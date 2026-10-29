from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from PIL import Image
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.estate_canva import EstateCanvaConnection, EstateCanvaDesign, EstateCanvaOAuthState, new_canva_return_key
from app.models.estate_foundation import Estate
from app.routers.estate_marketing import AD_FORMATS, AD_STYLES, _build_ctx, _ctx_plot, _render_estate_ad, _render_flyer, _render_plot_ad, _staff, _staff_view
from app.routers.plots import get_db
from app.services.estates import canva_connect, marketing_render
from app.services.estates.canva_editability import AUTOFILL_SCOPES, PDF_IMPORT_SCOPE, autofill_data, autofill_payload
from app.services.estates.documents import read_private_estate_file
from app.services.estates.audit import append_estate_audit_event
from app.utils.r2_objects import build_public_url, build_r2_settings, create_r2_client, delete_object_best_effort, normalize_object_key, upload_bytes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/estates", tags=["estate-canva"])


class CanvaEditRequest(BaseModel):
    format: str = Field(default="post", pattern="^(status|post|landscape|poster)$")
    style: str = Field(default="promo", max_length=32)
    plot_id: int | None = None
    campaign_id: int | None = None


class CanvaImportRequest(BaseModel):
    format: str = Field(default="png", pattern="^(png|jpg)$")


class CanvaPdfImportRequest(BaseModel):
    style: str = Field(default="luxury", max_length=32)
    campaign_id: int | None = None


class CanvaAutofillRequest(BaseModel):
    campaign_id: int | None = None


def _hash(value: str) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connection(db: Session, organization_id: int) -> EstateCanvaConnection:
    row = (
        db.query(EstateCanvaConnection)
        .filter(EstateCanvaConnection.organization_id == organization_id, EstateCanvaConnection.status != "revoked")
        .order_by(EstateCanvaConnection.updated_at.desc(), EstateCanvaConnection.id.desc())
        .first()
    )
    if row is None:
        raise HTTPException(409, "Connect a Canva account before editing a flyer.")
    return row


def _connection_payload(row: EstateCanvaConnection | None) -> dict:
    if row is None:
        return {"connected": False}
    return {
        "connected": row.status == "active",
        "id": row.id,
        "status": row.status,
        "canva_user_id": row.canva_user_id,
        "canva_team_id": row.canva_team_id,
        "connected_at": row.created_at,
        "last_error": row.last_error if row.status != "active" else None,
    }


def _design_payload(row: EstateCanvaDesign) -> dict:
    return {
        "id": row.id,
        "estate_id": row.estate_id,
        "title": row.title,
        "canva_design_id": row.canva_design_id,
        "source_format": row.source_format,
        "source_style": row.source_style,
        "r2_content_type": row.r2_content_type,
        "status": row.status,
        "imported": bool(row.r2_object_key and row.status == "imported"),
        "created_at": row.created_at,
        "imported_at": row.imported_at,
        "last_error": row.last_error,
    }


def _frontend_redirect(path: str, **params: str) -> RedirectResponse:
    base = canva_connect.web_url()
    separator = "&" if "?" in path else "?"
    clean = {key: value for key, value in params.items() if value}
    return RedirectResponse(f"{base}{path}{separator}{urlencode(clean)}" if clean else f"{base}{path}", status_code=302)


def _persist_created_design(
    db: Session,
    *,
    estate: Estate,
    access,
    connection: EstateCanvaConnection,
    design: dict,
    source_format: str,
    source_style: str,
    title_fallback: str,
) -> tuple[EstateCanvaDesign, str]:
    design_id = str(design.get("id") or "")
    edit_url = str((design.get("urls") or {}).get("edit_url") or "")
    if not design_id or not edit_url.startswith("https://www.canva.com/"):
        raise canva_connect.CanvaApiError("Canva returned an incomplete or unsafe design link.")
    return_key = new_canva_return_key()
    row = EstateCanvaDesign(
        organization_id=estate.organization_id,
        estate_id=estate.id,
        connection_id=connection.id,
        canva_design_id=design_id,
        canva_user_id=connection.canva_user_id,
        canva_team_id=connection.canva_team_id,
        title=str(design.get("title") or title_fallback)[:255],
        source_format=source_format,
        source_style=source_style,
        return_key_hash=_hash(return_key),
        status="created",
        created_by_subject_type=access.principal.subject_type,
        created_by_subject_id=str(access.principal.subject_id),
    )
    db.add(row)
    db.commit()
    return row, canva_connect.prepare_edit_url(edit_url, return_key)


@router.get("/{estate_id}/marketing/canva")
def canva_status(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff_view(db, request, estate_id, permission="estate.read")
    connection = (
        db.query(EstateCanvaConnection)
        .filter(EstateCanvaConnection.organization_id == estate.organization_id, EstateCanvaConnection.status != "revoked")
        .order_by(EstateCanvaConnection.updated_at.desc(), EstateCanvaConnection.id.desc())
        .first()
    )
    designs = db.query(EstateCanvaDesign).filter(EstateCanvaDesign.estate_id == estate.id).order_by(EstateCanvaDesign.created_at.desc()).limit(20).all()
    return {
        "configured": canva_connect.configured(),
        "autofill_configured": canva_connect.autofill_configured(),
        "autofill_ready": bool(connection and connection.status == "active" and canva_connect.connection_has_scopes(connection, set(AUTOFILL_SCOPES)) and canva_connect.autofill_configured()),
        "connection": _connection_payload(connection),
        "designs": [_design_payload(item) for item in designs],
    }


@router.post("/{estate_id}/marketing/canva/connect")
def canva_connect_start(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission="marketing.manage")
    try:
        canva_connect.require_configured()
    except canva_connect.CanvaNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    state = secrets.token_urlsafe(32)
    verifier, challenge = canva_connect.pkce_pair()
    db.add(EstateCanvaOAuthState(
        organization_id=estate.organization_id,
        estate_id=estate.id,
        state_hash=_hash(state),
        code_verifier_enc=canva_connect.encrypt_token(verifier),
        return_path=f"/estates/{estate.id}/marketing?tab=materials",
        created_by_subject_type=access.principal.subject_type,
        created_by_subject_id=str(access.principal.subject_id),
        expires_at=_now() + timedelta(minutes=10),
    ))
    db.commit()
    return {"url": canva_connect.authorization_url(state=state, challenge=challenge)}


@router.get("/marketing/canva/oauth/callback")
def canva_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    if error or not code or not state:
        return _frontend_redirect("/estates", canva_error="Canva connection was cancelled.")
    row = db.query(EstateCanvaOAuthState).filter(EstateCanvaOAuthState.state_hash == _hash(state)).with_for_update().one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= _now():
        return _frontend_redirect("/estates", canva_error="The Canva connection request expired. Please try again.")
    row.used_at = _now()
    db.commit()
    try:
        tokens = canva_connect.exchange_code(code, canva_connect.decrypt_token(row.code_verifier_enc))
        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not access_token or not refresh_token:
            raise canva_connect.CanvaApiError("Canva did not return the required connection tokens.")
        temporary = EstateCanvaConnection(
            organization_id=row.organization_id,
            canva_user_id="pending",
            canva_team_id="pending",
            access_token_enc=canva_connect.encrypt_token(access_token),
            refresh_token_enc=canva_connect.encrypt_token(refresh_token),
            access_token_expires_at=_now() + timedelta(seconds=int(tokens.get("expires_in") or 3600)),
            scopes=str(tokens.get("scope") or canva_connect.scopes()),
            status="active",
        )
        db.add(temporary)
        db.flush()
        user_id, team_id = canva_connect.current_user(db, temporary)
        existing = db.query(EstateCanvaConnection).filter(EstateCanvaConnection.organization_id == row.organization_id, EstateCanvaConnection.canva_user_id == user_id, EstateCanvaConnection.canva_team_id == team_id).one_or_none()
        connection = existing or temporary
        if existing:
            existing.access_token_enc = temporary.access_token_enc
            existing.refresh_token_enc = temporary.refresh_token_enc
            existing.access_token_expires_at = temporary.access_token_expires_at
            existing.scopes = temporary.scopes
            existing.status = "active"
            existing.last_error = None
            db.delete(temporary)
        connection.canva_user_id = user_id
        connection.canva_team_id = team_id
        connection.connected_by_subject_type = row.created_by_subject_type
        connection.connected_by_subject_id = row.created_by_subject_id
        db.commit()
        return _frontend_redirect(row.return_path, canva_connected="1")
    except Exception:
        db.rollback()
        logger.exception("Canva OAuth callback failed for organization %s", row.organization_id)
        return _frontend_redirect(row.return_path, canva_error="Canva could not be connected. Please try again.")


@router.delete("/marketing/canva/connections/{connection_id}")
def canva_disconnect(connection_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(EstateCanvaConnection, connection_id)
    if row is None:
        raise HTTPException(404, "Canva connection not found")
    estate = db.query(Estate).filter(Estate.organization_id == row.organization_id, Estate.archived_at.is_(None)).order_by(Estate.id.asc()).first()
    if estate is None:
        raise HTTPException(404, "Estate not found")
    access = _staff(db, request, estate.id, permission="marketing.manage")[1]
    try:
        canva_connect.revoke_token(canva_connect.decrypt_token(row.refresh_token_enc))
    except Exception:
        # Local revocation remains the safety boundary for LandCheck even if Canva is unavailable.
        logger.warning("Canva remote token revocation failed for connection %s", connection_id, exc_info=True)
    row.status = "revoked"
    row.last_error = "Disconnected by LandCheck user"
    row.access_token_enc = canva_connect.encrypt_token("revoked")
    row.refresh_token_enc = canva_connect.encrypt_token("revoked")
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="canva.disconnected", entity_type="estate_canva_connection", entity_id=row.id)
    db.commit()
    return {"status": "disconnected"}


@router.post("/{estate_id}/marketing/canva/designs")
def canva_create_design(estate_id: int, payload: CanvaEditRequest, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission="marketing.manage")
    if payload.format not in AD_FORMATS:
        raise HTTPException(422, "Choose status, post, landscape or poster")
    if payload.style not in AD_STYLES:
        raise HTTPException(422, "Choose a valid design style")
    if payload.format == "poster" and payload.style == "luxury":
        raise HTTPException(422, "The print poster is not available in the classic dark style")
    connection = _connection(db, estate.organization_id)
    campaign = None
    if payload.campaign_id:
        from app.routers.estate_marketing import _campaign_for_staff
        campaign = _campaign_for_staff(db, estate, payload.campaign_id)
    ctx = _build_ctx(db, estate, campaign.code if campaign else None)
    plot = _ctx_plot(ctx, payload.plot_id) if payload.plot_id else None
    data = _render_plot_ad(ctx, plot, payload.format, style=payload.style) if plot else _render_estate_ad(ctx, payload.format, style=payload.style)
    try:
        image = Image.open(BytesIO(data))
        width, height = image.size
    except Exception as exc:
        raise HTTPException(409, "The generated flyer could not be prepared for Canva.") from exc
    try:
        asset_id = canva_connect.upload_image(db, connection, data, f"LandCheck-{estate.name}-{payload.format}.png")
        design = canva_connect.create_design(db, connection, asset_id=asset_id, title=f"{estate.name} · LandCheck flyer", width=width, height=height)
        design_id = str(design.get("id") or "")
        edit_url = str((design.get("urls") or {}).get("edit_url") or "")
        owner = design.get("owner") or {}
        if not design_id or not edit_url or owner.get("user_id") != connection.canva_user_id or owner.get("team_id") != connection.canva_team_id:
            raise canva_connect.CanvaApiError("Canva returned an incomplete or mismatched design.")
        return_key = new_canva_return_key()
        row = EstateCanvaDesign(
            organization_id=estate.organization_id, estate_id=estate.id, connection_id=connection.id,
            canva_design_id=design_id, canva_user_id=connection.canva_user_id, canva_team_id=connection.canva_team_id,
            title=str(design.get("title") or f"{estate.name} flyer")[:255], source_format=payload.format, source_style=payload.style,
            source_plot_id=plot.id if plot else None, return_key_hash=_hash(return_key), status="created",
            created_by_subject_type=access.principal.subject_type, created_by_subject_id=str(access.principal.subject_id),
        )
        db.add(row)
        db.commit()
        edit_url = canva_connect.prepare_edit_url(edit_url, return_key)
        return {"id": row.id, "design_id": design_id, "edit_url": edit_url, "title": row.title}
    except canva_connect.CanvaApiError as exc:
        db.rollback()
        raise HTTPException(exc.status or 502, str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.exception("Canva design creation failed for estate %s", estate_id)
        raise HTTPException(502, "Canva could not open this flyer for editing.") from exc


@router.post("/{estate_id}/marketing/canva/pdf-import")
def canva_import_pdf_design(estate_id: int, payload: CanvaPdfImportRequest, request: Request, db: Session = Depends(get_db)):
    """Open the existing luxury PDF as a Canva design-import job.

    The other flyer families are deliberately kept on the existing image path:
    their source PDFs are full-page raster posters and cannot provide useful
    element-level editing.
    """
    estate, access = _staff(db, request, estate_id, permission="marketing.manage")
    if payload.style not in AD_STYLES:
        raise HTTPException(422, "Choose a valid design style")
    if payload.style != "luxury":
        raise HTTPException(422, "Element-level PDF import is currently available for the Luxury flyer only.")
    connection = _connection(db, estate.organization_id)
    if not canva_connect.connection_has_scopes(connection, {PDF_IMPORT_SCOPE}):
        raise HTTPException(409, "Reconnect Canva to grant the design:content:write permission required for PDF import.")
    campaign = None
    if payload.campaign_id:
        from app.routers.estate_marketing import _campaign_for_staff
        campaign = _campaign_for_staff(db, estate, payload.campaign_id)
    ctx = _build_ctx(db, estate, campaign.code if campaign else None)
    try:
        pdf = _render_flyer(ctx, "luxury")
        design = canva_connect.import_pdf(db, connection, pdf, f"{estate.name} · LandCheck editable flyer")
        row, edit_url = _persist_created_design(
            db,
            estate=estate,
            access=access,
            connection=connection,
            design=design,
            source_format="pdf",
            source_style="luxury",
            title_fallback=f"{estate.name} · LandCheck editable flyer",
        )
        return {"id": row.id, "design_id": row.canva_design_id, "edit_url": edit_url, "title": row.title, "mode": "pdf-import"}
    except canva_connect.CanvaApiError as exc:
        db.rollback()
        raise HTTPException(exc.status or 502, str(exc)) from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("Canva PDF import failed for estate %s", estate_id)
        raise HTTPException(502, "The flyer PDF could not be opened in Canva.") from exc


@router.post("/{estate_id}/marketing/canva/autofill")
def canva_autofill_design(estate_id: int, payload: CanvaAutofillRequest, request: Request, db: Session = Depends(get_db)):
    """Create an editable design from the configured LandCheck Canva template."""
    estate, access = _staff(db, request, estate_id, permission="marketing.manage")
    if not canva_connect.autofill_configured():
        raise HTTPException(503, "Autofill is not configured. Add CANVA_AUTOFILL_BRAND_TEMPLATE_ID first.")
    connection = _connection(db, estate.organization_id)
    if not canva_connect.connection_has_scopes(connection, set(AUTOFILL_SCOPES)):
        raise HTTPException(409, "Reconnect Canva to grant the brand-template and Autofill permissions.")
    campaign = None
    if payload.campaign_id:
        from app.routers.estate_marketing import _campaign_for_staff
        campaign = _campaign_for_staff(db, estate, payload.campaign_id)
    ctx = _build_ctx(db, estate, campaign.code if campaign else None)
    try:
        map_bytes = marketing_render.layout_png_for_documents(ctx, width=1500, height=1000)
        cover_bytes = None
        if getattr(ctx.estate, "public_cover_object_key", None):
            try:
                cover_bytes, _mime = read_private_estate_file(ctx.estate.public_cover_object_key)
            except Exception:
                logger.info("Estate cover could not be loaded for Canva Autofill; using layout map.", exc_info=True)
        hero_bytes = cover_bytes or map_bytes
        hero_asset_id = canva_connect.upload_image(db, connection, hero_bytes, f"{estate.name}-hero.png") if hero_bytes else None
        map_asset_id = canva_connect.upload_image(db, connection, map_bytes, f"{estate.name}-layout.png") if map_bytes else None
        logo_asset_id = canva_connect.upload_image(db, connection, ctx.logo_bytes, f"{estate.name}-logo.png") if ctx.logo_bytes else None
        areas = [float(plot.area_sqm) for plot in ctx.available_plots if plot.area_sqm is not None]
        if areas:
            low, high = min(areas), max(areas)
            size_text = marketing_render.area_text(low) if low == high else f"{marketing_render.area_text(low)} – {marketing_render.area_text(high)}"
        else:
            size_text = "Size on request"
        data = autofill_data(
            estate_name=estate.name,
            location=ctx.location or "Location available on request",
            price_text=marketing_render.naira_short(ctx.min_price) if ctx.min_price is not None else "Price on request",
            size_text=size_text,
            headline=estate.public_tagline or f"{estate.name} · available plots",
            description=estate.public_description or "Explore the published estate layout and available plots.",
            contact_name=ctx.agent_name or ctx.organization_name,
            contact_phone=ctx.contact_phone or "",
            contact_email=ctx.contact_email or "",
            hero_asset_id=hero_asset_id,
            logo_asset_id=logo_asset_id,
            map_asset_id=map_asset_id,
        )
        design = canva_connect.autofill_design(db, connection, autofill_payload(canva_connect.autofill_template_id(), data))
        row, edit_url = _persist_created_design(
            db,
            estate=estate,
            access=access,
            connection=connection,
            design=design,
            source_format="autofill",
            source_style="template",
            title_fallback=f"{estate.name} · LandCheck template flyer",
        )
        return {"id": row.id, "design_id": row.canva_design_id, "edit_url": edit_url, "title": row.title, "mode": "autofill"}
    except canva_connect.CanvaApiError as exc:
        db.rollback()
        raise HTTPException(exc.status or 502, str(exc)) from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("Canva Autofill failed for estate %s", estate_id)
        raise HTTPException(502, "The Canva template could not be prepared.") from exc


@router.get("/{estate_id}/marketing/canva/designs")
def canva_list_designs(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff_view(db, request, estate_id, permission="estate.read")
    rows = db.query(EstateCanvaDesign).filter(EstateCanvaDesign.estate_id == estate.id).order_by(EstateCanvaDesign.created_at.desc()).limit(20).all()
    return {"items": [_design_payload(row) for row in rows]}


@router.get("/{estate_id}/marketing/canva/designs/{design_id}/image")
def canva_design_image(estate_id: int, design_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff_view(db, request, estate_id, permission="estate.read")
    row = db.query(EstateCanvaDesign).filter(EstateCanvaDesign.id == design_id, EstateCanvaDesign.estate_id == estate.id, EstateCanvaDesign.organization_id == estate.organization_id, EstateCanvaDesign.status == "imported").one_or_none()
    if row is None or not row.r2_object_key:
        raise HTTPException(404, "Canva image not found")
    settings = build_r2_settings(prefix="R2")
    if settings is None:
        raise HTTPException(503, "File storage is not configured on this server.")
    try:
        object_key = normalize_object_key(row.r2_object_key, settings["bucket"])
        item = create_r2_client(settings).get_object(Bucket=settings["bucket"], Key=object_key)
        data = item["Body"].read()
    except Exception as exc:
        logger.exception("Canva image read failed for design %s", design_id)
        raise HTTPException(502, "The imported Canva image could not be loaded.") from exc
    return Response(data, media_type=row.r2_content_type or "image/png", headers={"Cache-Control": "private, no-store"})


@router.get("/marketing/canva/return")
def canva_return(correlation_jwt: str | None = Query(default=None), db: Session = Depends(get_db)):
    if not correlation_jwt:
        return _frontend_redirect("/estates", canva_error="Canva did not provide a return token.")
    try:
        claims = canva_connect.verify_correlation_jwt(correlation_jwt)
        key_hash = _hash(str(claims.get("correlation_state") or ""))
        row = db.query(EstateCanvaDesign).filter(EstateCanvaDesign.return_key_hash == key_hash).one_or_none()
        if row is None or row.canva_design_id != str(claims.get("design_id") or "") or row.canva_user_id != str(claims.get("sub") or "") or row.canva_team_id != str(claims.get("team_id") or ""):
            raise canva_connect.CanvaApiError("The Canva design return does not match this LandCheck workspace.", 400, "invalid_return")
        row.last_return_at = _now()
        db.commit()
        return _frontend_redirect(f"/estates/{row.estate_id}/marketing?tab=materials", canva_return="1", canva_design=str(row.id))
    except Exception:
        db.rollback()
        logger.warning("Rejected Canva return navigation", exc_info=True)
        return _frontend_redirect("/estates", canva_error="Canva returned an invalid or expired design link.")


@router.post("/{estate_id}/marketing/canva/designs/{design_id}/import")
def canva_import_design(estate_id: int, design_id: int, payload: CanvaImportRequest, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission="marketing.manage")
    row = db.query(EstateCanvaDesign).filter(EstateCanvaDesign.id == design_id, EstateCanvaDesign.estate_id == estate.id, EstateCanvaDesign.organization_id == estate.organization_id).one_or_none()
    if row is None:
        raise HTTPException(404, "Canva design not found")
    connection = db.query(EstateCanvaConnection).filter(EstateCanvaConnection.id == row.connection_id, EstateCanvaConnection.organization_id == estate.organization_id, EstateCanvaConnection.status == "active").one_or_none()
    if connection is None or connection.canva_user_id != row.canva_user_id or connection.canva_team_id != row.canva_team_id:
        raise HTTPException(409, "Reconnect the Canva account that created this design.")
    content_type = "image/jpeg" if payload.format == "jpg" else "image/png"
    if row.r2_object_key and row.r2_content_type == content_type and row.status == "imported":
        return {"design": _design_payload(row), "image_url": build_public_url(build_r2_settings(prefix="R2") or {}, row.r2_object_key) if build_r2_settings(prefix="R2") else None}
    had_imported_image = row.status == "imported" and bool(row.r2_object_key)
    uploaded_key: str | None = None
    try:
        data = canva_connect.export_design(db, connection, row.canva_design_id, payload.format)
        if payload.format == "png" and not data.startswith(b"\x89PNG"):
            raise canva_connect.CanvaApiError("Canva returned an invalid PNG export.")
        if payload.format == "jpg" and not data.startswith(b"\xff\xd8\xff"):
            raise canva_connect.CanvaApiError("Canva returned an invalid JPG export.")
        settings = build_r2_settings(prefix="R2")
        if settings is None:
            raise HTTPException(503, "File storage is not configured on this server.")
        object_key = f"estates/{estate.id}/canva/{uuid4().hex}.{payload.format}"
        uploaded_key = object_key
        upload_bytes(settings, object_key, data, content_type=content_type, cache_control="private, max-age=0, no-store")
        row.r2_object_key = object_key
        row.r2_content_type = content_type
        row.status = "imported"
        row.imported_at = _now()
        row.last_error = None
        append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="canva.design_imported", entity_type="estate_canva_design", entity_id=row.id, after_data={"format": payload.format})
        db.commit()
        return {"design": _design_payload(row), "image_url": build_public_url(settings, object_key)}
    except HTTPException:
        db.rollback()
        raise
    except canva_connect.CanvaApiError as exc:
        row.status = "imported" if had_imported_image else "import_failed"
        row.last_error = str(exc)[:500]
        db.commit()
        raise HTTPException(exc.status or 502, str(exc)) from exc
    except Exception as exc:
        if uploaded_key and (settings := build_r2_settings(prefix="R2")) is not None:
            try:
                delete_object_best_effort(settings, uploaded_key)
            except Exception:
                logger.warning("Canva import cleanup failed for %s", uploaded_key, exc_info=True)
        db.rollback()
        logger.exception("Canva import failed for design %s", design_id)
        raise HTTPException(502, "The Canva design could not be imported into LandCheck.") from exc
