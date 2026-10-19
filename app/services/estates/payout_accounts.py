from __future__ import annotations

"""Developer payout accounts for buyer online plot payments - see app/models/estate_foundation.py
EstateOrganizationPayoutAccount and app/services/estates/allocation_charges.py for how a payment
actually settles there. Setting one up creates a Paystack and/or Flutterwave subaccount (whichever
gateway is configured server-wide) so a future buyer checkout can split-settle directly to the
developer's own bank account instead of LandCheck's."""

import logging

from sqlalchemy.orm import Session

from app.models.estate_foundation import EstateOrganization, EstateOrganizationPayoutAccount
from app.services.estates.authorization import EstatePrincipal
from app.services.estates.audit import append_estate_audit_event
from app.utils import estate_flutterwave as flw
from app.utils import estate_paystack as pst

logger = logging.getLogger(__name__)


def resolve_bank_account(provider: str, bank_code: str, account_number: str) -> dict:
    """Preview-only account-name lookup, for a staff member to visually confirm the right
    account before saving it. Always resolves via Flutterwave when available - it's the one
    gateway already proven against a real account-resolve call elsewhere in this codebase
    (LandCheck Green's sponsor-payout flow) - falling back to Paystack's own resolve endpoint
    only if Flutterwave isn't configured server-wide."""
    if str(provider or "").strip().lower() == "paystack" or not flw.configured():
        return pst.resolve_account_name(bank_code, account_number)
    return flw.resolve_account_name(bank_code, account_number)


def get_payout_account(db: Session, organization_id: int) -> EstateOrganizationPayoutAccount | None:
    return db.query(EstateOrganizationPayoutAccount).filter(EstateOrganizationPayoutAccount.organization_id == int(organization_id)).one_or_none()


def set_up_payout_account(
    db: Session,
    *,
    organization: EstateOrganization,
    bank_code: str,
    bank_name: str | None,
    account_number: str,
    actor: EstatePrincipal,
) -> EstateOrganizationPayoutAccount:
    """Resolves the account name once, then creates a subaccount on every gateway configured
    server-wide, storing whichever succeed. Raises if neither gateway could create one - there is
    no safe fallback (see allocation_charges.initiate_allocation_payment, which refuses buyer
    checkout entirely when no payout account exists, rather than ever settling to LandCheck's own
    account by accident)."""
    resolved = resolve_bank_account("flutterwave" if flw.configured() else "paystack", bank_code, account_number)
    account_name = str(resolved.get("account_name") or "").strip()
    if not account_name:
        raise ValueError("Could not verify this account. Double-check the bank and account number.")

    # Each gateway is attempted independently and non-fatally - a Paystack account that's
    # configured (a secret key is set) but not yet fully verified for live payments must not
    # block Flutterwave from linking successfully, and vice versa. Whichever gateway(s) actually
    # succeed are what initiate_allocation_payment has to work with later.
    paystack_subaccount_code: str | None = None
    flutterwave_subaccount_id: str | None = None
    if pst.configured():
        try:
            paystack_subaccount_code = pst.create_subaccount(business_name=organization.name, bank_code=bank_code, account_number=account_number)["subaccount_code"]
        except Exception:
            logger.exception("Paystack subaccount creation failed for org %s - continuing without it", organization.id)
    if flw.configured():
        try:
            flutterwave_subaccount_id = flw.create_subaccount(account_bank=bank_code, account_number=account_number, business_name=organization.name)["subaccount_id"]
        except Exception:
            logger.exception("Flutterwave subaccount creation failed for org %s - continuing without it", organization.id)
    if not paystack_subaccount_code and not flutterwave_subaccount_id:
        raise ValueError("Could not link this account to any payment gateway right now. Please try again shortly.")

    account = get_payout_account(db, organization.id)
    before_data = None
    if account is not None:
        before_data = {"bank_code": account.bank_code, "account_name": account.account_name, "account_number_last4": account.account_number[-4:] if len(account.account_number) >= 4 else account.account_number}
    if account is None:
        account = EstateOrganizationPayoutAccount(organization_id=organization.id, created_by_subject_type=actor.subject_type, created_by_subject_id=actor.subject_id)
        db.add(account)
    account.bank_code = bank_code
    account.bank_name = bank_name
    account.account_number = account_number
    account.account_name = account_name
    account.paystack_subaccount_code = paystack_subaccount_code
    account.flutterwave_subaccount_id = flutterwave_subaccount_id
    db.flush()
    append_estate_audit_event(
        db,
        organization_id=organization.id,
        actor=actor,
        action="organization.payout_account_changed" if before_data else "organization.payout_account_configured",
        entity_type="estate_organization_payout_account",
        entity_id=account.id,
        before_data=before_data,
        after_data={"bank_code": bank_code, "account_name": account_name, "account_number_last4": account_number[-4:] if len(account_number) >= 4 else account_number},
    )
    return account
