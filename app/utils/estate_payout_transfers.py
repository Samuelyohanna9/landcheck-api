from __future__ import annotations

"""Flutterwave Transfer API client for Estates' admin-triggered "immediate settlement" payouts -
LandCheck advancing a developer their money immediately from LandCheck's own Flutterwave balance,
ahead of (and separate from) Flutterwave's own automatic subaccount settlement, which still
follows on its normal schedule and is reconciled independently (see settlement_tracking.py).

Deliberately a SEPARATE client from app/utils/estate_flutterwave.py (which only ever handles
buyer-checkout/subaccount creation, never outbound transfers) and from LandCheck Green's own
private transfer helpers in app/routers/green.py. Two independent live-money integrations should
never share code - a bug or credential issue in one must never silently affect the other. Reads
the same platform-level secret key convention Green's payouts already use live (FLW_SECRET_KEY /
FLUTTERWAVE_SECRET_KEY) since it is the same Flutterwave merchant account either way."""

import logging
import os

import requests

logger = logging.getLogger(__name__)

API_BASE = str(os.getenv("FLUTTERWAVE_API_BASE_URL") or "https://api.flutterwave.com/v3").strip().rstrip("/")
TIMEOUT = 30


def _secret_key() -> str:
    return str(os.getenv("FLW_SECRET_KEY") or os.getenv("FLUTTERWAVE_SECRET_KEY") or "").strip()


def configured() -> bool:
    return bool(_secret_key())


class TransferError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _headers(idempotency_key: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_secret_key()}", "Content-Type": "application/json"}
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    return headers


def _request(method: str, path: str, *, json: dict | None = None, idempotency_key: str | None = None) -> dict:
    if not configured():
        raise TransferError("LandCheck's Flutterwave payout key is not configured on this server.")
    try:
        response = requests.request(method, f"{API_BASE}{path}", json=json, headers=_headers(idempotency_key), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise TransferError(f"Could not reach Flutterwave: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or data.get("status") != "success":
        message = str(data.get("message") or f"Flutterwave returned HTTP {response.status_code}")
        logger.error("Flutterwave payout-transfer API error (%s %s): %s", method, path, message)
        raise TransferError(message, code=str(data.get("code") or "") or None)
    return data.get("data") or {}


def resolve_account_name(bank_code: str, account_number: str) -> dict:
    """Preview-only lookup - callers already have a verified EstateOrganizationPayoutAccount
    (resolved once at setup time, see app/services/estates/payout_accounts.py), so this is only
    used if a caller wants to re-confirm an account name right before transferring to it."""
    return _request("POST", "/accounts/resolve", json={"account_bank": bank_code, "account_number": account_number})


def create_transfer(*, bank_code: str, account_number: str, amount, beneficiary_name: str, reference: str, narration: str) -> dict:
    """Idempotent on `reference` (passed as both the Flutterwave `reference` field and the
    X-Idempotency-Key header) - callers should pass something stable per logical settlement (e.g.
    f"estate-settle-{charge_attempt_id}") so a retried request never double-transfers."""
    payload = {
        "account_bank": bank_code,
        "account_number": account_number,
        "amount": float(amount),
        "currency": "NGN",
        "beneficiary_name": beneficiary_name,
        "reference": reference,
        "narration": narration,
    }
    return _request("POST", "/transfers", json=payload, idempotency_key=reference)


def get_transfer_status(transfer_id: str) -> dict:
    return _request("GET", f"/transfers/{transfer_id}")
