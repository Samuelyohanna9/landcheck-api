from __future__ import annotations

"""LandCheck-internal Estates accounting admin - every organization's captured Flutterwave
payments, their settlement status, and the settlement requests developers have filed, visible
platform-wide (not scoped to one estate/org the way every other router in app/routers/estates.py
is). Gated by the SAME "Super Admin" session LandCheck Green's own admin payout tooling already
uses (require_super_admin_request) - one platform login, not a new Estates-specific admin account.

Mirrors app/services/estates/settlement_tracking.py's own rule: a row only ever becomes "settled"
on a REAL outcome (here: a successful Flutterwave transfer, not a guess)."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.estate_foundation import (
    Estate,
    EstateAllocation,
    EstateAllocationChargeAttempt,
    EstateCustomer,
    EstateOrganization,
    EstateOrganizationPayoutAccount,
    EstatePlot,
    EstateSettlementRequest,
)
from app.routers.plots import get_db
from app.services.estates import estate_email
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import EstatePrincipal
from app.utils import estate_payout_transfers as flw_transfers
from app.utils.auth_security import require_super_admin_request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/estate-admin/accounting", tags=["estate-admin-accounting"])


def _admin_principal(session) -> EstatePrincipal:
    return EstatePrincipal("landcheck_admin", str(session.user_id or session.subject_id or "admin"), session.display_name)


@router.get("/payments")
def list_all_captured_payments(request: Request, settlement_status: str | None = None, db: Session = Depends(get_db)):
    """Every Flutterwave charge attempt that actually captured money, across every organization -
    the platform-wide ledger of what's been received and what's still awaiting settlement."""
    require_super_admin_request(db, request)
    query = db.query(EstateAllocationChargeAttempt).filter(
        EstateAllocationChargeAttempt.payment_provider == "flutterwave",
        EstateAllocationChargeAttempt.status == "success",
    )
    if settlement_status in ("pending", "settled"):
        query = query.filter(EstateAllocationChargeAttempt.settlement_status == settlement_status)
    attempts = query.order_by(EstateAllocationChargeAttempt.completed_at.desc()).limit(500).all()

    org_ids = {a.organization_id for a in attempts}
    organizations = {o.id: o for o in db.query(EstateOrganization).filter(EstateOrganization.id.in_(org_ids)).all()} if org_ids else {}
    payout_accounts = {p.organization_id: p for p in db.query(EstateOrganizationPayoutAccount).filter(EstateOrganizationPayoutAccount.organization_id.in_(org_ids)).all()} if org_ids else {}
    allocation_ids = {a.allocation_id for a in attempts}
    allocations = {a.id: a for a in db.query(EstateAllocation).filter(EstateAllocation.id.in_(allocation_ids)).all()} if allocation_ids else {}
    estate_ids = {a.estate_id for a in allocations.values()}
    estates = {e.id: e for e in db.query(Estate).filter(Estate.id.in_(estate_ids)).all()} if estate_ids else {}
    plot_ids = {a.plot_id for a in allocations.values()}
    plots = {p.id: p for p in db.query(EstatePlot).filter(EstatePlot.id.in_(plot_ids)).all()} if plot_ids else {}
    customer_ids = {a.customer_id for a in attempts}
    customers = {c.id: c for c in db.query(EstateCustomer).filter(EstateCustomer.id.in_(customer_ids)).all()} if customer_ids else {}

    rows = []
    for attempt in attempts:
        allocation = allocations.get(attempt.allocation_id)
        estate = estates.get(allocation.estate_id) if allocation else None
        plot = plots.get(allocation.plot_id) if allocation else None
        organization = organizations.get(attempt.organization_id)
        payout_account = payout_accounts.get(attempt.organization_id)
        customer = customers.get(attempt.customer_id)
        rows.append({
            "id": attempt.id,
            "organization": {"id": organization.id, "name": organization.name} if organization else None,
            "estate": {"id": estate.id, "name": estate.name} if estate else None,
            "plot_number": plot.plot_number if plot else None,
            "customer_name": customer.full_name if customer else None,
            "amount": str(attempt.amount),
            "currency": attempt.currency,
            "tx_ref": attempt.tx_ref,
            "captured_at": attempt.completed_at,
            "settlement_status": attempt.settlement_status,
            "settlement_channel": attempt.settlement_channel,
            "settled_at": attempt.settled_at,
            "manual_transfer_status": attempt.manual_transfer_status,
            "manual_transfer_error": attempt.manual_transfer_error,
            "payout_account_ready": bool(payout_account and payout_account.bank_code and payout_account.account_number),
            "payout_account": {"bank_name": payout_account.bank_name, "account_name": payout_account.account_name, "account_number": payout_account.account_number} if payout_account else None,
        })
    return {"rows": rows}


@router.get("/settlement-requests")
def list_all_settlement_requests(request: Request, status: str | None = None, db: Session = Depends(get_db)):
    require_super_admin_request(db, request)
    query = db.query(EstateSettlementRequest)
    if status in ("requested", "in_review", "settled", "declined"):
        query = query.filter(EstateSettlementRequest.status == status)
    rows = query.order_by(EstateSettlementRequest.created_at.desc()).limit(200).all()
    org_ids = {r.organization_id for r in rows}
    organizations = {o.id: o for o in db.query(EstateOrganization).filter(EstateOrganization.id.in_(org_ids)).all()} if org_ids else {}
    return {"rows": [
        {
            "id": r.id,
            "organization": {"id": r.organization_id, "name": organizations[r.organization_id].name} if r.organization_id in organizations else None,
            "charge_attempt_id": r.charge_attempt_id,
            "amount": str(r.amount_snapshot) if r.amount_snapshot is not None else None,
            "note": r.note,
            "status": r.status,
            "created_at": r.created_at,
            "settled_at": r.settled_at,
        }
        for r in rows
    ]}


class SettlementRequestReview(BaseModel):
    status: str = Field(pattern="^(in_review|declined)$")


@router.patch("/settlement-requests/{request_id}")
def review_settlement_request(request_id: int, payload: SettlementRequestReview, request: Request, db: Session = Depends(get_db)):
    """Triage only (in_review/declined) - "settled" is only ever reached by actually transferring
    the money, via POST .../charge-attempts/{id}/settle below."""
    session = require_super_admin_request(db, request)
    settlement_request = db.get(EstateSettlementRequest, request_id)
    if not settlement_request:
        raise HTTPException(404, "Settlement request not found")
    settlement_request.status = payload.status
    settlement_request.reviewed_at = datetime.now(timezone.utc)
    settlement_request.reviewed_by_subject_type = "landcheck_admin"
    settlement_request.reviewed_by_subject_id = str(session.user_id or session.subject_id or "admin")
    db.commit()
    return {"id": settlement_request.id, "status": settlement_request.status}


@router.post("/charge-attempts/{attempt_id}/settle")
def settle_charge_attempt(attempt_id: int, request: Request, db: Session = Depends(get_db)):
    """Transfers the captured amount to the organization's verified payout account via
    Flutterwave's Transfer API, then marks the charge attempt (and any linked settlement request)
    settled. Only ever flips to "settled" after the transfer call itself succeeds."""
    session = require_super_admin_request(db, request)
    attempt = db.get(EstateAllocationChargeAttempt, attempt_id)
    if not attempt:
        raise HTTPException(404, "Payment not found")
    if attempt.payment_provider != "flutterwave" or attempt.status != "success":
        raise HTTPException(409, "Only a captured Flutterwave payment can be settled")
    if attempt.settlement_status == "settled":
        raise HTTPException(409, "This payment has already settled")
    payout_account = db.query(EstateOrganizationPayoutAccount).filter(EstateOrganizationPayoutAccount.organization_id == attempt.organization_id).one_or_none()
    if not payout_account or not payout_account.bank_code or not payout_account.account_number:
        raise HTTPException(422, "This organization has no verified payout account on file")
    organization = db.get(EstateOrganization, attempt.organization_id)
    if not organization:
        raise HTTPException(404, "Organization not found")

    reference = f"estate-settle-{attempt.id}"
    try:
        transfer = flw_transfers.create_transfer(
            bank_code=payout_account.bank_code,
            account_number=payout_account.account_number,
            amount=attempt.amount,
            beneficiary_name=payout_account.account_name,
            reference=reference,
            narration=f"LandCheck Estates settlement - {organization.name} - {attempt.tx_ref}",
        )
    except flw_transfers.TransferError as exc:
        attempt.manual_transfer_status = "failed"
        attempt.manual_transfer_error = str(exc)
        attempt.manual_settled_by_subject_type = "landcheck_admin"
        attempt.manual_settled_by_subject_id = str(session.user_id or session.subject_id or "admin")
        db.commit()
        raise HTTPException(502, f"Transfer could not be initiated: {exc}") from exc

    now = datetime.now(timezone.utc)
    attempt.settlement_status = "settled"
    attempt.settlement_channel = "manual_transfer"
    attempt.settled_at = now
    attempt.manual_transfer_id = str(transfer.get("id") or "")
    attempt.manual_transfer_reference = reference
    attempt.manual_transfer_status = str(transfer.get("status") or "initiated")
    attempt.manual_transfer_error = None
    attempt.manual_settled_by_subject_type = "landcheck_admin"
    attempt.manual_settled_by_subject_id = str(session.user_id or session.subject_id or "admin")

    linked_request = db.query(EstateSettlementRequest).filter(EstateSettlementRequest.charge_attempt_id == attempt.id, EstateSettlementRequest.status.in_(("requested", "in_review"))).one_or_none()
    if linked_request:
        linked_request.status = "settled"
        linked_request.settled_at = now
        linked_request.reviewed_at = linked_request.reviewed_at or now
        linked_request.reviewed_by_subject_type = "landcheck_admin"
        linked_request.reviewed_by_subject_id = str(session.user_id or session.subject_id or "admin")

    append_estate_audit_event(
        db, organization_id=attempt.organization_id, actor=_admin_principal(session), action="charge_attempt.manually_settled",
        entity_type="estate_allocation_charge_attempt", entity_id=attempt.id,
        after_data={"amount": str(attempt.amount), "transfer_id": attempt.manual_transfer_id, "reference": reference},
    )
    db.commit()
    estate_email.send_settlement_completed_email(organization=organization, amount=f"NGN {attempt.amount}", reference=reference)
    return {"id": attempt.id, "settlement_status": attempt.settlement_status, "manual_transfer_id": attempt.manual_transfer_id, "manual_transfer_status": attempt.manual_transfer_status}
