from __future__ import annotations

"""WhatsApp Business (Cloud API) template messages to buyers who opted in.

Configuration (environment):
  WHATSAPP_CLOUD_TOKEN       - permanent system-user token from Meta Business
  WHATSAPP_PHONE_NUMBER_ID   - the sending number's id
  WHATSAPP_API_VERSION       - default v23.0
  WHATSAPP_VERIFY_TOKEN      - any string; used when Meta verifies the webhook URL
  WHATSAPP_APP_SECRET        - (optional) to verify webhook signatures; falls back to META_APP_SECRET

Only pre-approved templates can start a conversation, so staff pick a preset here whose text must exist
(with the same name) in WhatsApp Manager. See PRESETS."""

import hashlib
import hmac
import os
import re
from typing import Any

import requests

TIMEOUT = 30
MAX_BROADCAST = 250

# preset key -> (default template name, label, sample text to create in WhatsApp Manager, params builder keys)
# Body variables are filled in this order: {{1}} first name, {{2}} estate name, {{3}} price or detail, {{4}} link.
PRESETS = {
    "new_plots": {
        "label": "New plots available",
        "env": "WA_TEMPLATE_NEW_PLOTS",
        "default_name": "estate_new_plots",
        "sample": "Hello {{1}}, new plots are now available at {{2}}, from {{3}}. View the live map and prices: {{4}}. Reply STOP to stop updates.",
    },
    "price_update": {
        "label": "Price or payment-plan update",
        "env": "WA_TEMPLATE_PRICE_UPDATE",
        "default_name": "estate_price_update",
        "sample": "Hello {{1}}, there is an update at {{2}}: {{3}}. Details: {{4}}. Reply STOP to stop updates.",
    },
    "inspection_invite": {
        "label": "Site inspection invitation",
        "env": "WA_TEMPLATE_INSPECTION",
        "default_name": "estate_inspection_invite",
        "sample": "Hello {{1}}, you are invited to a site inspection at {{2}}: {{3}}. Book your place: {{4}}. Reply STOP to stop updates.",
    },
}


def api_version() -> str:
    return str(os.getenv("WHATSAPP_API_VERSION") or "v23.0").strip()


def token() -> str:
    return str(os.getenv("WHATSAPP_CLOUD_TOKEN") or "").strip()


def phone_number_id() -> str:
    return str(os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()


def configured() -> bool:
    return bool(token() and phone_number_id())


def template_images_enabled() -> bool:
    """Only turn this on once every template above has an approved IMAGE header in WhatsApp Manager -
    see docs/SOCIAL_POSTING_SETUP.md. A template whose approved structure has no header will be
    rejected by Meta if we send one, so this stays off until the templates are actually updated."""
    return str(os.getenv("WHATSAPP_TEMPLATE_IMAGES") or "").strip().lower() in {"1", "true", "yes"}


def template_name(preset: str) -> str:
    spec = PRESETS[preset]
    return str(os.getenv(spec["env"]) or spec["default_name"]).strip()


def presets_payload() -> list[dict[str, str]]:
    return [{"key": key, "label": spec["label"], "template_name": template_name(key), "sample": spec["sample"]} for key, spec in PRESETS.items()]


def _clean_param(value: Any) -> str:
    # Template variables cannot contain newlines/tabs or long runs of spaces.
    return re.sub(r"\s{2,}", " ", re.sub(r"[\r\n\t]+", " ", str(value or ""))).strip()[:200] or "-"


class WhatsAppError(RuntimeError):
    pass


def send_template(to_digits: str, name: str, params: list[str], *, language: str = "en", header_image_url: str | None = None) -> str:
    """Sends one approved template message. Returns the WhatsApp message id.

    header_image_url only has an effect once the named template was itself approved with an IMAGE
    header component - see template_images_enabled()."""
    if not configured():
        raise WhatsAppError("WhatsApp messaging is not configured on this server")
    components = []
    if header_image_url:
        components.append({"type": "header", "parameters": [{"type": "image", "image": {"link": header_image_url}}]})
    components.append({"type": "body", "parameters": [{"type": "text", "text": _clean_param(value)} for value in params]})
    body = {
        "messaging_product": "whatsapp",
        "to": to_digits,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": language},
            "components": components,
        },
    }
    response = requests.post(
        f"https://graph.facebook.com/{api_version()}/{phone_number_id()}/messages",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        json=body,
        timeout=TIMEOUT,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or "error" in data:
        error = data.get("error") or {}
        raise WhatsAppError(str(error.get("error_user_msg") or error.get("message") or f"WhatsApp returned HTTP {response.status_code}"))
    messages = data.get("messages") or []
    return str(messages[0].get("id")) if messages else ""


def send_text(to_digits: str, body: str) -> str:
    """A free-form reply - only deliverable within 24 hours of the customer's last message (Meta's
    customer service window). Outside that window Meta rejects it and a template must be used
    instead - callers check the window themselves before calling this."""
    if not configured():
        raise WhatsAppError("WhatsApp messaging is not configured on this server")
    response = requests.post(
        f"https://graph.facebook.com/{api_version()}/{phone_number_id()}/messages",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to_digits, "type": "text", "text": {"body": str(body or "")[:4096]}},
        timeout=TIMEOUT,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or "error" in data:
        error = data.get("error") or {}
        raise WhatsAppError(str(error.get("error_user_msg") or error.get("message") or f"WhatsApp returned HTTP {response.status_code}"))
    messages = data.get("messages") or []
    return str(messages[0].get("id")) if messages else ""


def get_business_profile() -> dict[str, Any]:
    response = requests.get(
        f"https://graph.facebook.com/{api_version()}/{phone_number_id()}/whatsapp_business_profile",
        headers={"Authorization": f"Bearer {token()}"},
        params={"fields": "about,address,description,email,profile_picture_url,websites,vertical"},
        timeout=TIMEOUT,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or "error" in data:
        error = data.get("error") or {}
        raise WhatsAppError(str(error.get("error_user_msg") or error.get("message") or f"WhatsApp returned HTTP {response.status_code}"))
    rows = data.get("data") or []
    return rows[0] if rows else {}


def update_business_profile(**fields: Any) -> None:
    """about/address/description/email/vertical are plain strings; websites is a list of up to 2 URLs.
    Changing this changes what every LandCheck Estates customer sees on this shared number, across
    every estate - callers must restrict who can do this."""
    clean = {key: value for key, value in fields.items() if value is not None}
    if not clean:
        return
    response = requests.post(
        f"https://graph.facebook.com/{api_version()}/{phone_number_id()}/whatsapp_business_profile",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", **clean},
        timeout=TIMEOUT,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or "error" in data:
        error = data.get("error") or {}
        raise WhatsAppError(str(error.get("error_user_msg") or error.get("message") or f"WhatsApp returned HTTP {response.status_code}"))


def download_media(media_id: str) -> tuple[bytes, str]:
    """Media URLs Meta gives out are short-lived and themselves require the bearer token to fetch, so
    this always does both steps fresh rather than caching a URL."""
    lookup = requests.get(
        f"https://graph.facebook.com/{api_version()}/{media_id}",
        headers={"Authorization": f"Bearer {token()}"},
        timeout=TIMEOUT,
    )
    try:
        meta = lookup.json()
    except ValueError:
        meta = {}
    if lookup.status_code >= 400 or "error" in meta or not meta.get("url"):
        raise WhatsAppError("This media is no longer available from WhatsApp.")
    download = requests.get(str(meta["url"]), headers={"Authorization": f"Bearer {token()}"}, timeout=TIMEOUT)
    if download.status_code >= 400:
        raise WhatsAppError("This media is no longer available from WhatsApp.")
    return download.content, str(meta.get("mime_type") or "application/octet-stream")


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    secret = str(os.getenv("WHATSAPP_APP_SECRET") or os.getenv("META_APP_SECRET") or "").strip()
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])


STOP_WORDS = {"stop", "unsubscribe", "cancel", "end", "quit", "opt out", "optout"}


def is_stop_message(text: str | None) -> bool:
    return str(text or "").strip().lower() in STOP_WORDS
