from __future__ import annotations

"""A small, independent Flutterwave client for LandCheck Estates subscription billing.

Deliberately NOT sharing code with app/routers/green.py's Flutterwave integration (used for
LandCheck Green's tree-sponsorship checkout) - that is a live, real-money system, and importing
its private (underscore-prefixed) helpers or refactoring it to share code with a brand-new billing
feature is a needless risk to something already working. The handful of generic pieces (auth
headers, the API-call wrapper, webhook signature verification) are duplicated here instead - a
small amount of duplication is the safer trade.
"""

import base64
import hashlib
import hmac
import logging
import os
from decimal import Decimal
from typing import Any

import requests
from fastapi import HTTPException

logger = logging.getLogger(__name__)

FLUTTERWAVE_API_BASE_URL = str(os.getenv("FLUTTERWAVE_API_BASE_URL") or "https://api.flutterwave.com/v3").strip().rstrip("/")


def _flutterwave_secret_key() -> str | None:
    value = str(os.getenv("FLW_SECRET_KEY") or os.getenv("FLUTTERWAVE_SECRET_KEY") or "").strip()
    return value or None


def _flutterwave_secret_hash() -> str | None:
    value = str(os.getenv("FLW_SECRET_HASH") or os.getenv("FLUTTERWAVE_SECRET_HASH") or "").strip()
    return value or None


def _flutterwave_headers() -> dict[str, str]:
    secret_key = _flutterwave_secret_key()
    if not secret_key:
        raise HTTPException(status_code=503, detail="Flutterwave payment gateway is not configured on this server")
    return {"Authorization": f"Bearer {secret_key}", "Content-Type": "application/json"}


def call_flutterwave_api(method: str, path: str, *, payload: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{FLUTTERWAVE_API_BASE_URL}{path}"
    try:
        response = requests.request(method, url, headers=_flutterwave_headers(), json=payload, params=params, timeout=30)
    except requests.RequestException as exc:
        logger.exception("Could not reach Flutterwave (%s %s)", method, path)
        raise HTTPException(status_code=502, detail="Could not reach Flutterwave. Please try again.") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or str(body.get("status") or "").lower() == "error":
        detail = str(body.get("message") or body.get("error") or "Flutterwave rejected the request.")
        raise HTTPException(status_code=502, detail=detail)
    return body


def initiate_checkout(
    *,
    tx_ref: str,
    amount: Decimal,
    currency: str,
    email: str,
    name: str,
    redirect_url: str,
    title: str,
    description: str,
    meta: dict[str, Any] | None = None,
    payment_options: str | None = None,
    bank_transfer_expiry: int | None = None,
) -> str:
    """Creates a hosted-checkout charge and returns the link the customer is redirected to."""
    payload: dict[str, Any] = {
        "tx_ref": tx_ref,
        "amount": f"{Decimal(amount):.2f}",
        "currency": currency,
        "redirect_url": redirect_url,
        "customer": {"email": email, "name": name},
        "customizations": {"title": title, "description": description},
        "meta": meta or {},
        "configurations": {"session_duration": 30, "max_retry_attempt": 3},
    }
    if payment_options:
        payload["payment_options"] = payment_options
    if bank_transfer_expiry is not None:
        payload["bank_transfer_options"] = {"expires": int(bank_transfer_expiry)}
    response = call_flutterwave_api(
        "POST",
        "/payments",
        payload=payload,
    )
    link = str((response.get("data") or {}).get("link") or "")
    if not link:
        raise HTTPException(status_code=502, detail="Flutterwave did not return a checkout link.")
    return link


def verify_transaction(transaction_id: str) -> dict[str, Any]:
    """Server-side re-verification - never trust a webhook/redirect payload's own claimed status."""
    response = call_flutterwave_api("GET", f"/transactions/{transaction_id}/verify")
    return dict(response.get("data") or {})


def verify_transaction_by_reference(tx_ref: str) -> dict[str, Any]:
    response = call_flutterwave_api("GET", "/transactions/verify_by_reference", params={"tx_ref": tx_ref})
    return dict(response.get("data") or {})


def charge_token(
    *,
    token: str,
    amount: Decimal,
    currency: str,
    email: str,
    tx_ref: str,
    redirect_url: str | None = None,
) -> dict[str, Any]:
    """Charges a previously-captured card token with no customer interaction - the mechanism
    behind trial-conversion and renewal charges. Genuinely new capability for this codebase; there
    is no existing tokenized-charge usage anywhere else to model this on."""
    payload: dict[str, Any] = {
        "token": token,
        "currency": currency,
        "amount": f"{Decimal(amount):.2f}",
        "email": email,
        "tx_ref": tx_ref,
        # Flutterwave requires the billing country for tokenized charges. Without this field,
        # recurring charges can be rejected even when the stored token and amount are valid.
        "country": "NG",
    }
    if redirect_url:
        payload["redirect_url"] = redirect_url
    response = call_flutterwave_api(
        "POST",
        "/tokenized-charges",
        payload=payload,
    )
    return dict(response.get("data") or {})


def refund_transaction(transaction_id: str, *, amount: Decimal | None = None) -> None:
    payload: dict[str, Any] = {}
    if amount is not None:
        payload["amount"] = f"{Decimal(amount):.2f}"
    call_flutterwave_api("POST", f"/transactions/{transaction_id}/refund", payload=payload)


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    secret_hash = _flutterwave_secret_hash()
    if not secret_hash or not signature:
        return False
    expected = hmac.new(secret_hash.encode(), raw_body, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(expected).decode()
    return hmac.compare_digest(expected_b64, signature.strip())


def extract_card_token(verify_data: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Returns (token, last4, brand) from a verify-transaction response's card block, or
    (None, None, None) if this particular card wasn't tokenizable - callers must handle that case
    (no auto-renewal possible; the customer needs the manual pay-link fallback)."""
    card = dict(verify_data.get("card") or {})
    token = str(card.get("token") or "").strip() or None
    last4 = str(card.get("last_4digits") or "").strip() or None
    brand = str(card.get("type") or card.get("issuer") or "").strip() or None
    return token, last4, brand
