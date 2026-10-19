from __future__ import annotations

"""Lets an estate developer org link the bank account their buyers' online plot payments settle
into (see app/services/estates/payout_accounts.py for the subaccount-creation mechanics). Kept as
its own small router, separate from the much larger estates.py, since this is a real-money-
settlement surface that's easier to review in isolation."""

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.routers.plots import get_db
from app.services.estates import estate_email
from app.services.estates.authorization import require_estate_owner
from app.services.estates.payout_accounts import get_payout_account, resolve_bank_account, set_up_payout_account
from app.utils import estate_flutterwave as flw
from app.utils import estate_paystack as pst
from app.models.estate_foundation import EstateOrganization

router = APIRouter(prefix="/estates/payout-accounts", tags=["estate-payout-accounts"])


class BankAccountResolveRequest(BaseModel):
    bank_code: str = Field(min_length=1)
    account_number: str = Field(min_length=1)


class BankAccountSaveRequest(BaseModel):
    bank_code: str = Field(min_length=1)
    bank_name: str | None = None
    account_number: str = Field(min_length=1)


def _mask_account_number(account_number: str) -> str:
    value = str(account_number or "")
    return f"***{value[-4:]}" if len(value) >= 4 else "***"


@router.get("/banks")
def list_banks(provider: str = "flutterwave"):
    provider = str(provider or "").strip().lower()
    if provider == "paystack":
        if not pst.configured():
            raise HTTPException(503, "Paystack is not configured on this server")
        response = pst.call_paystack_api("GET", "/bank", params={"country": "nigeria"})
        rows = response.get("data") if isinstance(response.get("data"), list) else []
        banks = [{"code": str(row.get("code") or ""), "name": str(row.get("name") or "")} for row in rows if isinstance(row, dict) and row.get("code") and row.get("name")]
    else:
        if not flw.configured():
            raise HTTPException(503, "Flutterwave is not configured on this server")
        response = flw.call_flutterwave_api("GET", "/banks/NG")
        rows = response.get("data") if isinstance(response.get("data"), list) else []
        banks = [{"code": str(row.get("code") or ""), "name": str(row.get("name") or "")} for row in rows if isinstance(row, dict) and row.get("code") and row.get("name")]
    banks.sort(key=lambda item: item["name"].lower())
    return {"banks": banks}


@router.post("/{organization_id}/resolve")
def resolve_payout_account(organization_id: int, payload: BankAccountResolveRequest, request: Request, db: Session = Depends(get_db)):
    # Owner-only, not just billing.manage: this is the account buyer money settles into, so no
    # custom staff checklist - however billing-permissioned - can reach it. See
    # app/services/estates/authorization.py::require_estate_owner.
    require_estate_owner(db, request, organization_id, require_subscription=False)
    try:
        resolved = resolve_bank_account("flutterwave" if flw.configured() else "paystack", payload.bank_code, payload.account_number)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Could not verify this account right now. Please try again.")
    if not resolved.get("account_name"):
        raise HTTPException(422, "Could not verify this account. Double-check the bank and account number.")
    return resolved


@router.put("/{organization_id}")
def save_payout_account(organization_id: int, payload: BankAccountSaveRequest, request: Request, db: Session = Depends(get_db)):
    access = require_estate_owner(db, request, organization_id, require_subscription=False)
    organization = db.get(EstateOrganization, organization_id)
    if not organization:
        raise HTTPException(404, "Organization not found")
    try:
        account = set_up_payout_account(db, organization=organization, bank_code=payload.bank_code, bank_name=payload.bank_name, account_number=payload.account_number, actor=access.principal)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    estate_email.send_payout_account_changed_email(
        organization=organization,
        account_name=account.account_name,
        bank_name=account.bank_name,
        masked_account_number=_mask_account_number(account.account_number),
        changed_by_display_name=access.principal.display_name,
    )
    return {
        "bank_code": account.bank_code,
        "bank_name": account.bank_name,
        "account_number": _mask_account_number(account.account_number),
        "account_name": account.account_name,
        "paystack_linked": bool(account.paystack_subaccount_code),
        "flutterwave_linked": bool(account.flutterwave_subaccount_id),
    }


@router.get("/{organization_id}")
def get_payout_account_status(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_owner(db, request, organization_id, require_subscription=False)
    account = get_payout_account(db, organization_id)
    if not account:
        return {"configured": False}
    return {
        "configured": True,
        "bank_code": account.bank_code,
        "bank_name": account.bank_name,
        "account_number": _mask_account_number(account.account_number),
        "account_name": account.account_name,
        "paystack_linked": bool(account.paystack_subaccount_code),
        "flutterwave_linked": bool(account.flutterwave_subaccount_id),
    }
