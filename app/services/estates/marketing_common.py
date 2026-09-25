from __future__ import annotations

"""Small shared helpers for the Estate marketing features: public URLs, phone/WhatsApp links,
per-IP throttling for anonymous endpoints, and campaign/agent resolution from a source code."""

import os
import re
import time
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.models.estate_foundation import Estate, EstateOrganizationMember, EstatePlot, EstateQrCampaign


def web_url() -> str:
    return str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").rstrip("/")


def api_url() -> str:
    return str(os.getenv("LANDCHECK_API_PUBLIC_URL") or "https://api.landcheck.online").rstrip("/")


def public_page_url(estate: Estate, *, source: str | None = None, plot_id: int | None = None) -> str:
    query = []
    if plot_id:
        query.append(f"plot={int(plot_id)}")
    if source:
        query.append(f"source={quote(str(source), safe='')}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return f"{web_url()}/estates/public/{estate.public_slug}{suffix}"


def share_page_url(estate: Estate, *, source: str | None = None, plot_id: int | None = None) -> str:
    """URL that serves Open Graph tags (so WhatsApp/Facebook show a rich card) and then redirects
    the visitor to the real public page."""
    base = str(os.getenv("LANDCHECK_SHARE_BASE_URL") or api_url()).rstrip("/")
    path = f"/estates/public/{estate.public_slug}/share"
    if plot_id:
        path += f"/plots/{int(plot_id)}"
    return f"{base}{path}" + (f"?source={quote(str(source), safe='')}" if source else "")


def normalize_phone_digits(raw: str | None) -> str | None:
    """Digits-only international form suitable for wa.me links. Nigerian local numbers
    (0803...) become 234803...; anything else is returned as its digits."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]
    if len(digits) < 8 or len(digits) > 15:
        return None
    return digits


def whatsapp_link(phone: str | None, text: str | None = None) -> str | None:
    digits = normalize_phone_digits(phone)
    if not digits:
        return None
    return f"https://wa.me/{digits}" + (f"?text={quote(text)}" if text else "")


_THROTTLE: dict[tuple[str, str], list[float]] = {}


def throttled(client: str, bucket: str, *, limit: int, window_seconds: int) -> bool:
    """True when this client has exceeded `limit` calls to `bucket` within the window. Per-process
    and best-effort - it only exists to stop casual abuse of anonymous write endpoints."""
    now = time.monotonic()
    key = (client or "unknown", bucket)
    recent = [stamp for stamp in _THROTTLE.get(key, []) if now - stamp < window_seconds]
    if len(recent) >= limit:
        _THROTTLE[key] = recent
        return True
    recent.append(now)
    _THROTTLE[key] = recent
    if len(_THROTTLE) > 5000:
        for stale_key in [item for item, stamps in _THROTTLE.items() if not stamps or now - stamps[-1] > window_seconds]:
            _THROTTLE.pop(stale_key, None)
    return False


def client_ip(request) -> str:
    forwarded = str(request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def resolve_campaign(db: Session, estate_id: int, source: str | None) -> EstateQrCampaign | None:
    code = str(source or "").strip()
    if not code:
        return None
    return db.query(EstateQrCampaign).filter(
        EstateQrCampaign.estate_id == estate_id,
        EstateQrCampaign.code == code,
        EstateQrCampaign.is_active.is_(True),
    ).one_or_none()


def agent_member_for(db: Session, campaign: EstateQrCampaign | None) -> EstateOrganizationMember | None:
    if not campaign or not campaign.assigned_agent_subject_type or not campaign.assigned_agent_subject_id:
        return None
    return db.query(EstateOrganizationMember).filter(
        EstateOrganizationMember.organization_id == campaign.organization_id,
        EstateOrganizationMember.subject_type == campaign.assigned_agent_subject_type,
        EstateOrganizationMember.subject_id == campaign.assigned_agent_subject_id,
        EstateOrganizationMember.is_active.is_(True),
    ).one_or_none()


def ensure_agent_campaign(db: Session, *, estate: Estate, member: EstateOrganizationMember) -> EstateQrCampaign:
    """Every agent gets one stable personal link per estate, created on first use."""
    existing = db.query(EstateQrCampaign).filter(
        EstateQrCampaign.estate_id == estate.id,
        EstateQrCampaign.assigned_agent_subject_type == member.subject_type,
        EstateQrCampaign.assigned_agent_subject_id == member.subject_id,
        EstateQrCampaign.channel == "agent_link",
    ).order_by(EstateQrCampaign.id.asc()).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            db.flush()
        return existing
    base = re.sub(r"[^a-z0-9]+", "-", str(member.subject_id).lower()).strip("-")[:60] or "agent"
    code = f"agent-{base}"
    suffix = 2
    while db.query(EstateQrCampaign.id).filter(EstateQrCampaign.estate_id == estate.id, EstateQrCampaign.code == code).first():
        code = f"agent-{base}-{suffix}"
        suffix += 1
    row = EstateQrCampaign(
        organization_id=estate.organization_id,
        estate_id=estate.id,
        code=code,
        name=f"{member.subject_id} - personal link",
        channel="agent_link",
        assigned_agent_subject_type=member.subject_type,
        assigned_agent_subject_id=member.subject_id,
        created_by_subject_type=member.subject_type,
        created_by_subject_id=member.subject_id,
    )
    db.add(row)
    db.flush()
    return row


def plot_centroid(plot: EstatePlot):
    from geoalchemy2.shape import to_shape

    return to_shape(plot.geometry).centroid
