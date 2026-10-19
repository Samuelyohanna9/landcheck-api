from __future__ import annotations

"""Checks whether successful online plot payments have actually settled into the developer's
bank account yet - see app/utils/estate_flutterwave.py's list_settlements()/get_settlement_detail()
docstrings for why this is explicitly marked unverified against a real settled transaction.
Never speculates: a row only ever becomes "settled" on a positive match against Flutterwave's own
settlement records; anything else (no match, an API error, an unexpected response shape) leaves
it "pending" and simply updates settlement_checked_at so the UI can show when it was last asked."""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.estate_foundation import EstateAllocationChargeAttempt
from app.services.estates.payout_accounts import get_payout_account
from app.utils import estate_flutterwave as flw

logger = logging.getLogger(__name__)


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _find_settlement_match(detail: dict, provider_transaction_id: str) -> dict | None:
    transactions = detail.get("transactions")
    if not isinstance(transactions, list):
        return None
    for tx in transactions:
        if not isinstance(tx, dict):
            continue
        tx_id = str(tx.get("id") or tx.get("transaction_id") or "").strip()
        if tx_id and tx_id == provider_transaction_id:
            return tx
    return None


def refresh_settlement_status(db: Session, *, organization_id: int) -> int:
    """Returns how many rows were updated (matched as settled). Only ever called on demand (a
    staff-triggered "Refresh" button), not a background poll, since this integration path is
    new and unverified - see the module docstring."""
    if not flw.configured():
        return 0
    payout_account = get_payout_account(db, organization_id)
    if not payout_account or not payout_account.flutterwave_subaccount_id:
        return 0

    pending = (
        db.query(EstateAllocationChargeAttempt)
        .filter(
            EstateAllocationChargeAttempt.organization_id == organization_id,
            EstateAllocationChargeAttempt.payment_provider == "flutterwave",
            EstateAllocationChargeAttempt.status == "success",
            EstateAllocationChargeAttempt.settlement_status == "pending",
        )
        .all()
    )
    if not pending:
        return 0

    try:
        batches = flw.list_settlements(subaccount_id=payout_account.flutterwave_subaccount_id)
    except Exception:
        logger.exception("Could not list Flutterwave settlements for org %s", organization_id)
        return 0

    now = datetime.now(timezone.utc)
    settled_count = 0
    pending_by_tx_id = {str(attempt.provider_transaction_id or "").strip(): attempt for attempt in pending if attempt.provider_transaction_id}

    for batch in batches:
        if not isinstance(batch, dict) or not pending_by_tx_id:
            break
        batch_id = str(batch.get("id") or batch.get("settlement_id") or "").strip()
        if not batch_id:
            continue
        try:
            detail = flw.get_settlement_detail(batch_id)
        except Exception:
            logger.exception("Could not fetch Flutterwave settlement %s for org %s", batch_id, organization_id)
            continue
        batch_status = str(detail.get("status") or batch.get("status") or "").strip().upper()
        settled_when = _parse_timestamp(detail.get("settled_at") or detail.get("created_at") or batch.get("created_at")) or now
        for tx_id, attempt in list(pending_by_tx_id.items()):
            match = _find_settlement_match(detail, tx_id)
            if not match:
                continue
            subaccount_settlement = Decimal(str(match.get("subaccount_settlement") or 0))
            if batch_status == "SUCCESSFUL" and subaccount_settlement > 0:
                attempt.settlement_status = "settled"
                attempt.settled_at = settled_when
                settled_count += 1
                pending_by_tx_id.pop(tx_id, None)

    for attempt in pending:
        attempt.settlement_checked_at = now
    db.flush()
    return settled_count
