from __future__ import annotations

"""A small, independent Paystack client for LandCheck Estates subscription billing - the
second payment gateway alongside app/utils/estate_flutterwave.py. Deliberately mirrors that
module's exact public function signatures (initiate_checkout, verify_transaction,
verify_transaction_by_reference, charge_token, refund_transaction, verify_webhook_signature,
extract_card_token) so every call site in subscriptions.py/estate_billing.py can dispatch
between the two with a one-line `module_for(provider).xyz(...)` instead of branching
provider-specific logic throughout. See app/utils/estate_payment_provider.py for that
dispatch.

Two gotchas Paystack has that Flutterwave doesn't, both handled entirely inside this module
so nothing above it needs to know:
- Amounts are in kobo (NGN x 100), not naira - converted at the edges (in/out of this file).
- Verification is reference-only - there's no separate numeric-transaction-id lookup the way
  Flutterwave's /transactions/{id}/verify exists, so verify_transaction(transaction_id) just
  treats whatever it's given as the reference (the caller already generates and tracks its
  own tx_ref/reference for every payment, same as the Flutterwave flow does).

_normalize_verify_response() translates Paystack's native response shape into the exact
dict keys/values Flutterwave's own responses already carry (status "successful" not
"success", amount in naira not kobo, meta not metadata, a `card` block instead of
`authorization`, payment_type instead of channel) - so extract_card_token() and every
caller in estate_billing.py/subscriptions.py read the same keys regardless of which
gateway actually processed the payment.
"""

import hashlib
import hmac
import logging
import os
from decimal import Decimal
from typing import Any

import requests
from fastapi import HTTPException

logger = logging.getLogger(__name__)

PAYSTACK_API_BASE_URL = str(os.getenv("PAYSTACK_API_BASE_URL") or "https://api.paystack.co").strip().rstrip("/")


def _paystack_secret_key() -> str | None:
    value = str(os.getenv("PAYSTACK_SECRET_KEY") or "").strip()
    return value or None


def configured() -> bool:
    return bool(_paystack_secret_key())


def _paystack_headers() -> dict[str, str]:
    secret_key = _paystack_secret_key()
    if not secret_key:
        raise HTTPException(status_code=503, detail="Paystack payment gateway is not configured on this server")
    return {"Authorization": f"Bearer {secret_key}", "Content-Type": "application/json"}


def call_paystack_api(method: str, path: str, *, payload: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{PAYSTACK_API_BASE_URL}{path}"
    try:
        response = requests.request(method, url, headers=_paystack_headers(), json=payload, params=params, timeout=30)
    except requests.RequestException as exc:
        logger.exception("Could not reach Paystack (%s %s)", method, path)
        raise HTTPException(status_code=502, detail="Could not reach Paystack. Please try again.") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or body.get("status") is False:
        detail = str(body.get("message") or "Paystack rejected the request.")
        raise HTTPException(status_code=502, detail=detail)
    return body


def _to_kobo(amount: Decimal) -> int:
    return int((Decimal(amount) * 100).quantize(Decimal("1")))


def _from_kobo(amount_kobo: Any) -> Decimal:
    try:
        return (Decimal(str(amount_kobo)) / 100).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0")


def _normalize_verify_response(data: dict[str, Any]) -> dict[str, Any]:
    authorization = dict(data.get("authorization") or {})
    status = str(data.get("status") or "").strip().lower()
    return {
        "id": data.get("id"),
        "tx_ref": data.get("reference"),
        "status": "successful" if status == "success" else status,
        "amount": _from_kobo(data.get("amount")),
        "charged_amount": _from_kobo(data.get("amount")),
        "currency": str(data.get("currency") or "NGN").upper(),
        "meta": dict(data.get("metadata") or {}),
        "customer": dict(data.get("customer") or {}),
        "payment_type": str(data.get("channel") or "").strip().lower(),
        "processor_response": data.get("gateway_response"),
        "card": {
            "token": authorization.get("authorization_code"),
            "last_4digits": authorization.get("last4"),
            "type": authorization.get("card_type") or authorization.get("brand"),
        },
    }


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
    subaccount_code: str | None = None,
) -> str:
    """Creates a hosted-checkout transaction and returns the link the customer is redirected
    to. `title`/`description`/`bank_transfer_expiry` are Flutterwave-only concepts with no
    Paystack equivalent - accepted for interface compatibility with estate_flutterwave.py,
    silently unused here. `payment_options` ("card", "banktransfer", "card,banktransfer")
    maps to Paystack's `channels` list. `subaccount_code` (see create_subaccount()) routes the
    settlement split to a buyer-plot-payment payout account instead of the main account - leave
    unset for subscription billing, which must always settle to the main account."""
    channels: list[str] = []
    options = str(payment_options or "").lower()
    if "card" in options:
        channels.append("card")
    if "bank" in options or "transfer" in options:
        channels.append("bank_transfer")
    payload: dict[str, Any] = {
        "reference": tx_ref,
        "amount": _to_kobo(amount),
        "currency": currency,
        "email": email,
        "callback_url": redirect_url,
        "metadata": {**(meta or {}), "customer_name": name},
    }
    if channels:
        payload["channels"] = channels
    if subaccount_code:
        payload["subaccount"] = subaccount_code
    response = call_paystack_api("POST", "/transaction/initialize", payload=payload)
    link = str((response.get("data") or {}).get("authorization_url") or "")
    if not link:
        raise HTTPException(status_code=502, detail="Paystack did not return a checkout link.")
    return link


def resolve_account_name(bank_code: str, account_number: str) -> dict[str, Any]:
    """Looks up the account holder's name for a bank code + account number, so a staff member
    can visually confirm it's the right account before it's saved as a payout destination."""
    response = call_paystack_api("GET", "/bank/resolve", params={"account_number": str(account_number).strip(), "bank_code": str(bank_code).strip()})
    data = dict(response.get("data") or {})
    return {"account_name": str(data.get("account_name") or ""), "account_number": str(data.get("account_number") or account_number)}


def create_subaccount(*, business_name: str, bank_code: str, account_number: str) -> dict[str, Any]:
    """Creates a Paystack subaccount that future initiate_checkout(subaccount_code=...) calls can
    settle into directly. `percentage_charge=0` is intended to mean 0% retained by the main
    (LandCheck) account / 100% settled to the subaccount - THIS MUST BE CONFIRMED WITH A REAL
    TEST-MODE TRANSACTION before this is relied on for a real buyer payment; Paystack's own docs
    are ambiguous on which party this percentage benefits."""
    response = call_paystack_api("POST", "/subaccount", payload={
        "business_name": business_name,
        "settlement_bank": str(bank_code).strip(),
        "account_number": str(account_number).strip(),
        "percentage_charge": 0,
    })
    data = dict(response.get("data") or {})
    subaccount_code = str(data.get("subaccount_code") or "")
    if not subaccount_code:
        raise HTTPException(status_code=502, detail="Paystack did not return a subaccount code.")
    return {"subaccount_code": subaccount_code}


def verify_transaction(transaction_id: str) -> dict[str, Any]:
    """Paystack has no separate numeric-transaction-id verify endpoint - treats the given
    value as the reference, same as verify_transaction_by_reference."""
    return verify_transaction_by_reference(transaction_id)


def verify_transaction_by_reference(tx_ref: str) -> dict[str, Any]:
    response = call_paystack_api("GET", f"/transaction/verify/{tx_ref}")
    return _normalize_verify_response(dict(response.get("data") or {}))


def charge_token(
    *,
    token: str,
    amount: Decimal,
    currency: str,
    email: str,
    tx_ref: str,
    redirect_url: str | None = None,
) -> dict[str, Any]:
    """Charges a previously-captured authorization_code with no customer interaction - the
    mechanism behind trial-conversion and renewal charges, same role as
    estate_flutterwave.py's charge_token but via Paystack's /transaction/charge_authorization."""
    payload: dict[str, Any] = {
        "authorization_code": token,
        "email": email,
        "amount": _to_kobo(amount),
        "currency": currency,
        "reference": tx_ref,
    }
    response = call_paystack_api("POST", "/transaction/charge_authorization", payload=payload)
    return _normalize_verify_response(dict(response.get("data") or {}))


def refund_transaction(transaction_id: str, *, amount: Decimal | None = None) -> None:
    payload: dict[str, Any] = {"transaction": transaction_id}
    if amount is not None:
        payload["amount"] = _to_kobo(amount)
    call_paystack_api("POST", "/refund", payload=payload)


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    secret_key = _paystack_secret_key()
    if not secret_key or not signature:
        return False
    expected = hmac.new(secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def extract_card_token(verify_data: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Same (token, last4, brand) contract as estate_flutterwave.py's extract_card_token -
    verify_data here has already been normalized by _normalize_verify_response, so this reads
    the identical `card` block shape regardless of which gateway produced it."""
    card = dict(verify_data.get("card") or {})
    token = str(card.get("token") or "").strip() or None
    last4 = str(card.get("last_4digits") or "").strip() or None
    brand = str(card.get("type") or "").strip() or None
    return token, last4, brand
