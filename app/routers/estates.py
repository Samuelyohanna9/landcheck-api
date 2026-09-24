from __future__ import annotations

import copy
import csv
import io
import json
import re
import tempfile
import os
import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from geoalchemy2.shape import from_shape, to_shape
from pyproj import Transformer
import ezdxf
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform, unary_union
from sqlalchemy import and_, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.routers.plots import _metric_epsg_for_wgs84_polygon, _subdivide_polygon_equal_count, get_db
from app.services.estates.authorization import list_estate_access, resolve_estate_principal
from app.services.estates.identity import slugify
from app.services.estates.entitlements import ESTATE_FEATURES, get_estate_entitlement
from app.models.estate_foundation import Estate, EstateAgentPortalToken, EstateAllocation, EstateAuditEvent, EstateBlock, EstateCommissionPayout, EstateCommissionTier, EstateCustomer, EstateCustomerPortalToken, EstateDocument, EstateDocumentLink, EstateFieldInspection, EstateHazardAssessment, EstateImportReview, EstateLayoutProposal, EstateNotificationLog, EstateOrganization, EstateOrganizationMember, EstatePayment, EstatePaymentInbox, EstatePaymentRule, EstatePlot, EstatePublicReservationRequest, EstateQrCampaign, EstateSpatialFeature, EstateSurveyRequest, EstateStakingTask
from app.schemas.estates import AllocationAction, BlockCreate, BlockUpdate, CommissionPayoutCreate, CommissionTiersUpdate, CustomerCreate, DevelopmentForecastPublishUpdate, DevelopmentStatusUpdate, EstateCreate, EstateLayoutCriteria, EstateLayoutDecision, EstateLayoutFeatureAdd, EstateLayoutFeatureRemove, EstateLayoutProposalEdit, EstateSubdivisionCreate, EstateUpdate, FieldInspectionCreate, GeoreferenceSessionLink, ImportFromGeoreference, ImportReviewCreate, ImportReviewDecision, ImportReviewFromGeoreferenceSession, MemberCreate, MemberUpdate, PaymentInboxCreate, PaymentInboxMatch, PlotCreate, PlotAddressUpdate, PlotGeometryUpdate, PlotListingDefaultsUpdate, PlotPriceUpdate, PortalTokenCreate, PublicEstateSettingsUpdate, PublicReservationConvert, PublicReservationCreate, PublicReservationUpdate, PaymentCreate, QrCampaignCreate, SpatialFeatureCreate, SpatialFeatureUpdate, SurveyEligibilityUpdate, VoidAction
from app.services.estates.payments import confirm_payment, financial_summary, record_payment, void_payment
from app.services.estates.documents import read_private_estate_file, store_private_estate_file
from app.utils.r2_objects import delete_object_best_effort, build_r2_settings
from app.services.estates.permissions import has_permission
from sqlalchemy import func
from app.services.estates.allocations import release_allocation, reserve_or_allocate
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import EstatePrincipal, require_estate_access
from app.services.estates.subscriptions import get_subscription, has_hazard_access
from app.services.estates.survey_requests import transition
from app.services.estates.survey_adapter import materialize_estate_plot_for_survey
from app.schemas.estate_survey import SurveyorAssignment
from app.services.estates.survey_eligibility import survey_eligibility
from app.services.estates.qc import validate_polygon
from app.services.estates.layout_generation import generate_estate_layout
from app.services.survey.dgps import alpha_station, render_dgps_staking_csv
from app.utils.coordinate_converter import COORDINATE_SYSTEMS, resolve_coordinate_system_key, convert_coordinates
from app.models.estate_auth import EstateAccount
from app.utils.survey_auth_security import find_or_create_survey_user, issue_survey_session
from app.models.plot import Plot
from app.services.estates import estate_email
from app.services.estates.layout_export import render_estate_layout_pdf
from app.services.estates.report_export import render_customer_statement_pdf, render_estate_report_pdf
from app.services.estates import commissions
from app.services.estates.operations import DOCUMENT_REQUIREMENTS, ESTATE_DOCUMENT_TYPES, ESTATE_DOCUMENT_TYPE_CODES, PUBLIC_DOCUMENT_TYPES, advance_payment_schedule_after_payment, build_operations_summary, buyer_portal_payload, customer_portal_url, document_readiness, hash_portal_token, issue_agent_portal_token, issue_customer_portal_link, issue_customer_portal_token, linked_documents, record_notification_log
from app.db import SessionLocal
from app.utils.hazard_jobs import get_hazard_job, insert_hazard_job, make_progress_reporter, serialize_hazard_job, set_hazard_job_status


router = APIRouter(prefix="/estates", tags=["estates"])


def _normalize_idempotency_key(value: str | None) -> str | None:
    key = str(value or "").strip()
    if not key:
        return None
    if len(key) > 128:
        raise HTTPException(status_code=422, detail="Idempotency key must be 128 characters or fewer")
    return key


def _agent_portal_url(raw_token: str) -> str:
    base = str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").rstrip("/")
    return f"{base}/estates/agent-portal/{raw_token}"

def _survey_payload(db, row):
    plot=db.get(EstatePlot,row.plot_id); estate=db.get(Estate,row.estate_id)
    allocation = db.get(EstateAllocation, row.allocation_id) if row.allocation_id else None
    return {"id":row.id,"reference":row.request_uid,"status":row.status,"estate":{"id":estate.id,"name":estate.name},"plot":{"id":plot.id,"number":plot.plot_number},"assigned_surveyor":row.assigned_surveyor_subject_id,"survey_reference":row.survey_reference,"survey_working_plot_id":row.survey_working_plot_id,"materialized":bool(row.survey_working_plot_id),"eligibility":survey_eligibility(db, allocation),"created_at":row.created_at}


def _bulk_financial_summaries(db: Session, allocations) -> dict[int, dict[str, Decimal]]:
    """Calculate allocation totals with one grouped payment query instead of two queries per row."""
    allocation_list = list(allocations or [])
    allocation_ids = [allocation.id for allocation in allocation_list]
    payment_totals: dict[int, dict[str, Decimal]] = {}
    if allocation_ids:
        payment_rows = db.query(
            EstatePayment.allocation_id,
            EstatePayment.status,
            func.coalesce(func.sum(EstatePayment.amount), 0),
        ).filter(
            EstatePayment.allocation_id.in_(allocation_ids),
            EstatePayment.status.in_(("confirmed", "recorded", "pending_confirmation")),
        ).group_by(EstatePayment.allocation_id, EstatePayment.status).all()
        for allocation_id, status, total in payment_rows:
            payment_totals.setdefault(allocation_id, {})[status] = Decimal(str(total or 0))
    result = {}
    for allocation in allocation_list:
        price = Decimal(str(allocation.agreed_price or 0))
        totals = payment_totals.get(allocation.id, {})
        confirmed = totals.get("confirmed", Decimal(0))
        pending = totals.get("recorded", Decimal(0)) + totals.get("pending_confirmation", Decimal(0))
        outstanding = max(Decimal(0), price - confirmed)
        result[allocation.id] = {
            "agreed_price": price,
            "confirmed_paid": confirmed,
            "pending_paid": pending,
            "outstanding": outstanding,
            "percentage": confirmed / price * 100 if price else Decimal(0),
        }
    return result


def _bulk_survey_payloads(db: Session, rows) -> list[dict]:
    """Build the survey queue without a database round-trip for every request."""
    request_rows = list(rows or [])
    if not request_rows:
        return []
    plot_ids = {row.plot_id for row in request_rows}
    estate_ids = {row.estate_id for row in request_rows}
    allocation_ids = {row.allocation_id for row in request_rows if row.allocation_id}
    plots = {plot.id: plot for plot in db.query(EstatePlot).filter(EstatePlot.id.in_(plot_ids)).all()}
    estates = {estate.id: estate for estate in db.query(Estate).filter(Estate.id.in_(estate_ids)).all()}
    allocations = {allocation.id: allocation for allocation in db.query(EstateAllocation).filter(EstateAllocation.id.in_(allocation_ids or {-1})).all()}
    financials = _bulk_financial_summaries(db, allocations.values())
    rules = db.query(EstatePaymentRule).filter(
        EstatePaymentRule.organization_id.in_({allocation.organization_id for allocation in allocations.values()} or {-1}),
        EstatePaymentRule.rule_key == "survey_minimum_confirmed_percentage",
    ).all()
    required_by_org = {rule.organization_id: Decimal(rule.percentage or 0) if rule.is_enabled else Decimal(0) for rule in rules}
    return [
        {
            "id": row.id,
            "reference": row.request_uid,
            "status": row.status,
            "estate": {"id": estates[row.estate_id].id, "name": estates[row.estate_id].name},
            "plot": {"id": plots[row.plot_id].id, "number": plots[row.plot_id].plot_number},
            "assigned_surveyor": row.assigned_surveyor_subject_id,
            "survey_reference": row.survey_reference,
            "survey_working_plot_id": row.survey_working_plot_id,
            "materialized": bool(row.survey_working_plot_id),
            "eligibility": (
                {"eligible": True, "confirmed_percentage": "0", "required_percentage": "0", "reason": "No allocation is linked"}
                if not row.allocation_id or row.allocation_id not in allocations
                else {
                    "eligible": financials[row.allocation_id]["percentage"] >= required_by_org.get(allocations[row.allocation_id].organization_id, Decimal(0)),
                    "confirmed_percentage": str(financials[row.allocation_id]["percentage"]),
                    "required_percentage": str(required_by_org.get(allocations[row.allocation_id].organization_id, Decimal(0))),
                    "reason": "Eligible" if financials[row.allocation_id]["percentage"] >= required_by_org.get(allocations[row.allocation_id].organization_id, Decimal(0)) else "Confirmed payments are below the organization survey threshold",
                }
            ),
            "created_at": row.created_at,
        }
        for row in request_rows
    ]


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _estate_metadata_snapshot(estate: Estate) -> dict:
    return {
        "name": estate.name,
        "state": estate.state,
        "locality": estate.locality,
        "location_text": estate.location_text,
        "description": estate.description,
        "crs": estate.crs,
        "datum": estate.datum,
        "unit_system": estate.unit_system,
        "approximate_area_sqm": str(estate.approximate_area_sqm) if estate.approximate_area_sqm is not None else None,
        "project_reference": estate.project_reference,
        "project_owner": estate.project_owner,
        "ownership_details": estate.ownership_details,
    }


def _enabled(db: Session, org_id: int) -> None:
    if not get_estate_entitlement(db, org_id, "ESTATES_ENABLED").is_enabled:
        raise HTTPException(status_code=404, detail="LandCheck Estates is not enabled")


def _public_estate(db: Session, slug: str) -> Estate:
    estate = db.query(Estate).filter(Estate.public_slug == str(slug or "").strip().lower()).one_or_none()
    if not estate or estate.archived_at is not None or not estate.public_enabled or estate.status != "active":
        raise HTTPException(status_code=404, detail="This Estate is not available.")
    if not get_estate_entitlement(db, estate.organization_id, "ESTATE_PUBLIC_MAP").is_enabled:
        raise HTTPException(status_code=404, detail="This Estate is not available.")
    return estate


def _public_reservation_payload(db: Session, row: EstatePublicReservationRequest) -> dict:
    estate = db.get(Estate, row.estate_id)
    plot = db.get(EstatePlot, row.plot_id)
    attribution = _reservation_attribution(db, row)
    return {
        "id": row.id,
        "uid": row.request_uid,
        "estate_id": row.estate_id,
        "estate_name": estate.name if estate else None,
        "plot_id": row.plot_id,
        "plot_number": plot.plot_number if plot else None,
        "full_name": row.full_name,
        "phone": row.phone,
        "email": row.email,
        "message": row.message,
        "source_code": row.source_code,
        "source_channel": row.source_channel,
        "assigned_agent_subject_type": row.assigned_agent_subject_type,
        "assigned_agent_subject_id": row.assigned_agent_subject_id,
        "attribution": attribution,
        "assigned_agent_name": attribution.get("agent_name"),
        "status": row.status,
        "staff_notes": row.staff_notes,
        "created_at": row.created_at,
        "contacted_at": row.contacted_at,
        "resolved_at": row.resolved_at,
        "customer_id": row.customer_id,
        "allocation_id": row.allocation_id,
    }


def _public_estate_payload(db: Session, estate: Estate) -> dict:
    plots = (
        db.query(EstatePlot)
        .filter(EstatePlot.estate_id == estate.id, EstatePlot.geometry_status == "approved")
        .order_by(EstatePlot.plot_number_normalized.asc())
        .all()
    )
    blocks = {block.id: block.label for block in db.query(EstateBlock).filter(EstateBlock.estate_id == estate.id).all()}
    visible_statuses = {"available", "reserved", "allocated", "on_hold", "under_survey", "under_staking", "developed"}
    public_plots = [
        {
            "id": plot.id,
            "plot_number": plot.plot_number,
            "block": blocks.get(plot.block_id),
            "address": plot.public_address,
            "area_sqm": float(plot.area_sqm) if plot.area_sqm is not None else None,
            "status": plot.commercial_status if plot.commercial_status in visible_statuses else "on_hold",
            "price": str(plot.asking_price) if estate.public_show_prices and plot.asking_price is not None else None,
            "geometry": mapping(to_shape(plot.geometry)),
        }
        for plot in plots
        if plot.geometry and plot.commercial_status in visible_statuses
    ]
    counts = {status: sum(1 for plot in public_plots if plot["status"] == status) for status in sorted(visible_statuses)}
    organization = db.get(EstateOrganization, estate.organization_id)
    forecast = copy.deepcopy(estate.public_development_forecast) if isinstance(estate.public_development_forecast, dict) and estate.public_development_forecast.get("published") else None
    if forecast:
        headline = str((forecast.get("reach_estimate") or {}).get("headline") or "")
        forecast.setdefault("reach_estimate", {})["headline"] = headline.replace(
            "; this analysis does not treat that as a promise of future development", ""
        )
        forecast["public_disclaimer"] = "This is a location-screening scenario based on rigorous analysis from multiple reliable data sources."
    return {
        "id": estate.id,
        "name": estate.name,
        "slug": estate.public_slug,
        "organization_name": organization.name if organization else None,
        "organization_email": organization.contact_email if organization else None,
        "logo_url": f"/estates/public/{estate.public_slug}/logo" if estate.public_logo_object_key else None,
        "tagline": estate.public_tagline,
        "description": estate.public_description or estate.description,
        "location": estate.location_text or ", ".join(filter(None, [estate.locality, estate.state])) or None,
        "contact_phone": estate.public_contact_phone,
        "show_prices": bool(estate.public_show_prices),
        "payment_plan": estate.public_payment_plan or [],
        "boundary": mapping(to_shape(estate.boundary)) if estate.boundary else None,
        "plots": public_plots,
        "counts": counts,
        "development_forecast": forecast,
    }


def _reservation_attribution(db: Session, row: EstatePublicReservationRequest) -> dict:
    if row.assigned_agent_subject_type and row.assigned_agent_subject_id:
        display_name = _agent_display_name(
            db,
            organization_id=row.organization_id,
            subject_type=row.assigned_agent_subject_type,
            subject_id=row.assigned_agent_subject_id,
        )
        return {
            "type": "agent",
            "label": "Agent QR",
            "agent_name": display_name,
            "source_code": row.source_code,
            "source_channel": row.source_channel,
        }
    if row.source_code:
        campaign = db.query(EstateQrCampaign).filter(
            EstateQrCampaign.estate_id == row.estate_id,
            EstateQrCampaign.code == row.source_code,
        ).one_or_none()
        return {
            "type": "company_qr",
            "label": "Company QR",
            "campaign_name": campaign.name if campaign else row.source_code,
            "source_code": row.source_code,
            "source_channel": row.source_channel or (campaign.channel if campaign else None),
        }
    return {"type": "public_page", "label": "Public Estate page", "source_code": None, "source_channel": None}


def _agent_display_name(db: Session, *, organization_id: int, subject_type: str, subject_id: str) -> str:
    if subject_type == "estate_account" and str(subject_id).isdigit():
        account = db.get(EstateAccount, int(subject_id))
        if account:
            return account.full_name
    member = db.query(EstateOrganizationMember).filter(
        EstateOrganizationMember.organization_id == organization_id,
        EstateOrganizationMember.subject_type == subject_type,
        EstateOrganizationMember.subject_id == subject_id,
    ).one_or_none()
    if member:
        return member.subject_id
    return subject_id


def _allocation_attribution(db: Session, allocation: EstateAllocation) -> dict:
    if allocation.sales_agent_subject_type and allocation.sales_agent_subject_id:
        return {
            "type": "agent",
            "label": "Agent QR",
            "agent_name": _agent_display_name(
                db,
                organization_id=allocation.organization_id,
                subject_type=allocation.sales_agent_subject_type,
                subject_id=allocation.sales_agent_subject_id,
            ),
            "source_code": allocation.lead_source_code,
            "source_channel": allocation.lead_source_channel,
        }
    if allocation.lead_source_code:
        campaign = db.query(EstateQrCampaign).filter(
            EstateQrCampaign.estate_id == allocation.estate_id,
            EstateQrCampaign.code == allocation.lead_source_code,
        ).one_or_none()
        return {
            "type": "company_qr",
            "label": "Company QR",
            "campaign_name": campaign.name if campaign else allocation.lead_source_code,
            "source_code": allocation.lead_source_code,
            "source_channel": allocation.lead_source_channel or (campaign.channel if campaign else None),
        }
    return {"type": "direct", "label": "Direct company reservation", "source_code": None, "source_channel": None}


def _unique_public_slug(db: Session, estate: Estate) -> str:
    """Create a stable, URL-safe page address from the Estate name."""
    base = slugify(estate.name)
    candidate = base
    suffix = 2
    while db.query(Estate.id).filter(Estate.public_slug == candidate, Estate.id != estate.id).first():
        suffix_text = f"-{suffix}"
        candidate = f"{base[:140 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate


def _geojson_geometry(value: dict, *, allow_polygon: bool = True):
    try:
        geometry = shape(value)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Geometry is invalid GeoJSON") from exc
    allowed = {"Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point"}
    if not allow_polygon:
        allowed.discard("Polygon")
        allowed.discard("MultiPolygon")
    if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in allowed:
        raise HTTPException(status_code=422, detail="Geometry is empty, invalid, or unsupported")
    return geometry


@router.get("/public/{slug}")
def public_estate_showcase(slug: str, source: str | None = None, db: Session = Depends(get_db)):
    """Return only the approved, public-safe inventory for a published Estate."""
    estate = _public_estate(db, slug)
    if source:
        campaign = db.query(EstateQrCampaign).filter(EstateQrCampaign.estate_id == estate.id, EstateQrCampaign.code == source.strip(), EstateQrCampaign.is_active.is_(True)).one_or_none()
        if campaign:
            campaign.scan_count = int(campaign.scan_count or 0) + 1
            campaign.last_scanned_at = datetime.now(timezone.utc)
            db.commit()
    return _public_estate_payload(db, estate)


@router.get("/public/{slug}/logo")
def public_estate_logo(slug: str, db: Session = Depends(get_db)):
    """Serve the deliberately public company logo without exposing its storage object key."""
    estate = _public_estate(db, slug)
    if not estate.public_logo_object_key:
        raise HTTPException(status_code=404, detail="Logo not available")
    data, mime = read_private_estate_file(estate.public_logo_object_key)
    return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=3600"})


@router.post("/public/{slug}/plots/{plot_id}/reservation", status_code=201)
def create_public_reservation(slug: str, plot_id: int, payload: PublicReservationCreate, source: str | None = None, idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"), db: Session = Depends(get_db)):
    """Create a sales lead without creating a customer or changing plot ownership."""
    estate = _public_estate(db, slug)
    idempotency_key = _normalize_idempotency_key(idempotency_header)
    if idempotency_key:
        existing = db.query(EstatePublicReservationRequest).filter(EstatePublicReservationRequest.organization_id == estate.organization_id, EstatePublicReservationRequest.idempotency_key == idempotency_key).one_or_none()
        if existing:
            return {"status": "received", "request_id": existing.request_uid, "notification_sent": False, "welcome_email_sent": False, "already_processed": True}
    plot = (
        db.query(EstatePlot)
        .filter(EstatePlot.id == plot_id, EstatePlot.estate_id == estate.id, EstatePlot.geometry_status == "approved")
        .one_or_none()
    )
    if not plot:
        raise HTTPException(status_code=404, detail="Plot not found")
    if plot.commercial_status != "available":
        raise HTTPException(status_code=409, detail="This plot is no longer available")
    email = str(payload.email or "").strip().lower() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address or leave it blank")
    source_code = (source or payload.source or "").strip() or None
    campaign = db.query(EstateQrCampaign).filter(EstateQrCampaign.estate_id == estate.id, EstateQrCampaign.code == source_code, EstateQrCampaign.is_active.is_(True)).one_or_none() if source_code else None
    row = EstatePublicReservationRequest(
        organization_id=estate.organization_id,
        idempotency_key=idempotency_key,
        estate_id=estate.id,
        plot_id=plot.id,
        full_name=payload.full_name.strip(),
        phone=payload.phone.strip(),
        email=email,
        message=payload.message.strip() if payload.message else None,
        source_code=campaign.code if campaign else source_code,
        source_channel=campaign.channel if campaign else None,
        assigned_agent_subject_type=campaign.assigned_agent_subject_type if campaign else None,
        assigned_agent_subject_id=campaign.assigned_agent_subject_id if campaign else None,
        status="new",
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = db.query(EstatePublicReservationRequest).filter(EstatePublicReservationRequest.organization_id == estate.organization_id, EstatePublicReservationRequest.idempotency_key == idempotency_key).one_or_none()
            if existing:
                return {"status": "received", "request_id": existing.request_uid, "notification_sent": False, "welcome_email_sent": False, "already_processed": True}
        raise HTTPException(status_code=409, detail="This reservation request was already submitted")
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=None,
        action="public_reservation.created",
        entity_type="estate_public_reservation_request",
        entity_id=row.id,
        after_data={"estate_id": estate.id, "plot_id": plot.id, "plot_number": plot.plot_number, "full_name": row.full_name, "phone": row.phone, "email": row.email, "source_code": row.source_code},
        metadata={"source": "public_estate_showcase", "campaign": row.source_code},
    )
    db.commit()
    organization = db.get(EstateOrganization, estate.organization_id)
    notification_sent = estate_email.notify_reservation_request(
        to_email=organization.contact_email if organization else None,
        organization_name=organization.name if organization else "Estate team",
        estate_name=estate.name,
        plot_number=plot.plot_number,
        full_name=row.full_name,
        phone=row.phone,
        email=row.email,
        message=row.message,
    )
    welcome_email_sent = estate_email.send_public_reservation_welcome(
        to_email=row.email,
        full_name=row.full_name,
        organization_name=organization.name if organization else "Estate team",
        estate_name=estate.name,
        plot_number=plot.plot_number,
        plot_address=plot.public_address,
        area_sqm=plot.area_sqm,
        price=plot.asking_price if estate.public_show_prices else None,
        payment_plan=estate.public_payment_plan,
        contact_phone=estate.public_contact_phone,
        contact_email=organization.contact_email if organization else None,
        public_page_url=f"{str(os.getenv('LANDCHECK_WEB_URL') or 'https://landcheck.online').rstrip('/')}/estates/public/{estate.public_slug}",
    )
    record_notification_log(
        db,
        organization_id=estate.organization_id,
        estate_id=estate.id,
        customer_id=None,
        allocation_id=None,
        event_key="public_reservation_welcome",
        recipient_email=row.email,
        recipient_name=row.full_name,
        subject=f"Welcome to {estate.name} - Plot {plot.plot_number}",
        status="sent" if welcome_email_sent else ("failed" if row.email else "skipped"),
    )
    db.commit()
    return {"status": "received", "request_id": row.request_uid, "notification_sent": notification_sent, "welcome_email_sent": welcome_email_sent, "already_processed": False}


def _import_candidate(*, row_number: int, plot_number: str, geometry: dict) -> dict:
    try:
        _, issues = validate_polygon(geometry)
        errors = [issue.message for issue in issues if issue.severity == "error"]
    except Exception:
        errors = ["geometry must be a valid GeoJSON Polygon"]
    return {
        "row": row_number,
        "plot_number": plot_number.strip(),
        "geometry": geometry,
        "valid": not errors,
        "issues": errors,
    }


def _create_plots_from_candidates(db: Session, *, estate: Estate, candidates: list[dict], source_type: str, source_reference: str, actor) -> int:
    """Shared by the CSV/GIS/DXF import-review approval path and the georeference import path -
    both end up with the same shape of reviewed candidate rows (plot_number + geometry + valid),
    just produced by a different intake method."""
    created = 0
    for candidate in candidates or []:
        number = str(candidate.get("plot_number") or "").strip()
        geometry = candidate.get("geometry")
        if not candidate.get("valid") or not number or not geometry:
            continue
        normalized = _normalized(number)
        if db.query(EstatePlot).filter(EstatePlot.estate_id == estate.id, EstatePlot.plot_number_normalized == normalized).first():
            continue
        area, issues = validate_polygon(geometry)
        if any(issue.severity == "error" for issue in issues):
            continue
        plot = EstatePlot(
            estate_id=estate.id,
            plot_number=number,
            plot_number_normalized=normalized,
            geometry=from_shape(shape(geometry), srid=4326),
            area_sqm=area,
            geometry_status="approved",
            source_type=source_type,
            source_reference=source_reference,
            created_by_subject_type=actor.subject_type,
            created_by_subject_id=actor.subject_id,
        )
        db.add(plot)
        created += 1
    return created


def _layout_proposal_payload(row: EstateLayoutProposal) -> dict:
    return {
        "id": row.id,
        "uid": row.proposal_uid,
        "estate_id": row.estate_id,
        "status": row.status,
        "criteria": row.criteria or {},
        "diagnostics": row.diagnostics or {},
        "candidates": row.plot_candidates or [],
        "features": row.feature_candidates or [],
        "created_at": row.created_at,
        "reviewed_at": row.reviewed_at,
    }


def _layout_unallocated_areas(estate: Estate, plot_candidates: list[dict], feature_candidates: list[dict]) -> list[dict]:
    """Return measurable pieces of the Estate boundary not occupied by plots or features.

    Layout drafts need to show unused land explicitly. This is intentionally derived from the
    current draft every time it changes, rather than stored as another editable geometry that can
    drift away from the plots and roads it describes.
    """
    if not estate or not estate.boundary:
        return []
    try:
        boundary = to_shape(estate.boundary)
        metric_epsg = _metric_epsg_for_wgs84_polygon(boundary)
        forward = Transformer.from_crs("EPSG:4326", f"EPSG:{metric_epsg}", always_xy=True).transform
        backward = Transformer.from_crs(f"EPSG:{metric_epsg}", "EPSG:4326", always_xy=True).transform
        metric_boundary = shapely_transform(forward, boundary)
        if metric_boundary.is_empty:
            return []
    except Exception:
        return []

    occupied: list = []
    for candidate in plot_candidates or []:
        try:
            geometry = shapely_transform(forward, shape(candidate.get("geometry") or {}))
            if not geometry.is_empty:
                occupied.append(geometry.buffer(0) if not geometry.is_valid else geometry)
        except Exception:
            continue
    for feature in feature_candidates or []:
        try:
            geometry = shapely_transform(forward, shape(feature.get("geometry") or {}))
            if geometry.is_empty:
                continue
            if feature.get("feature_type") == "road":
                geometry = geometry.buffer(float(feature.get("width_m") or 9.0) / 2, cap_style=2)
            occupied.append(geometry.buffer(0) if not geometry.is_valid else geometry)
        except Exception:
            continue

    occupied_union = unary_union(occupied) if occupied else None
    remaining = metric_boundary if occupied_union is None else metric_boundary.difference(occupied_union)
    if remaining.is_empty:
        return []

    pieces = []
    if remaining.geom_type == "Polygon":
        pieces = [remaining]
    elif remaining.geom_type == "MultiPolygon":
        pieces = list(remaining.geoms)
    elif remaining.geom_type == "GeometryCollection":
        pieces = [item for item in remaining.geoms if item.geom_type == "Polygon"]

    result = []
    for index, piece in enumerate(sorted(pieces, key=lambda item: item.area, reverse=True), 1):
        if piece.is_empty or piece.area < 25:
            continue
        geometry = shapely_transform(backward, piece)
        result.append({
            "label": f"Unallocated area {index}",
            "area_sqm": round(float(piece.area), 2),
            "geometry": mapping(geometry),
        })
    return result


def _refresh_layout_diagnostics(estate: Estate, diagnostics: dict | None, plot_candidates: list[dict], feature_candidates: list[dict]) -> dict:
    refreshed = dict(diagnostics or {})
    unallocated = _layout_unallocated_areas(estate, plot_candidates, feature_candidates)
    refreshed["unallocated_areas"] = unallocated
    refreshed["unallocated_area_sqm"] = round(sum(float(item.get("area_sqm") or 0) for item in unallocated), 2)
    return refreshed


def _compact_generated_road_gap(
    estate: Estate,
    plot_candidates: list[dict],
    deleted_feature: dict,
    remaining_features: list[dict],
) -> tuple[list[dict], list[dict]] | None:
    """Close a deleted generated-road corridor by moving one layout side to the other side.

    Generated roads are centre lines between complete rows of plots. Keeping the released corridor
    in place creates a visual and geometric split through the middle of the estate. Translating one
    side by the road width preserves every plot's area and number while moving the same amount of
    unused land to that side's outer edge. Hand-drawn roads are excluded because their carved-plot
    history has a more precise restore operation.
    """
    if deleted_feature.get("feature_type") != "road" or deleted_feature.get("carved_plots"):
        return None
    try:
        boundary = to_shape(estate.boundary)
        road_shape = shape(deleted_feature.get("geometry") or {})
        if road_shape.geom_type not in {"LineString", "MultiLineString"}:
            return None
        metric_epsg = _metric_epsg_for_wgs84_polygon(boundary)
        forward = Transformer.from_crs("EPSG:4326", f"EPSG:{metric_epsg}", always_xy=True).transform
        backward = Transformer.from_crs(f"EPSG:{metric_epsg}", "EPSG:4326", always_xy=True).transform
        metric_boundary = shapely_transform(forward, boundary)
        boundary_check = metric_boundary.buffer(0.1)
        metric_road = shapely_transform(forward, road_shape)
        road_parts = [metric_road] if metric_road.geom_type == "LineString" else list(metric_road.geoms)
        road_line = max(road_parts, key=lambda item: item.length, default=None)
        if road_line is None or road_line.length < 2:
            return None
        start_x, start_y = road_line.coords[0]
        end_x, end_y = road_line.coords[-1]
        length = math.hypot(end_x - start_x, end_y - start_y)
        if length < 2:
            return None
        normal_x = -(end_y - start_y) / length
        normal_y = (end_x - start_x) / length
        road_center = road_line.centroid
        width_m = max(float(deleted_feature.get("width_m") or 9.0), 1.0)

        metric_plots = []
        for candidate in plot_candidates:
            geometry = shapely_transform(forward, shape(candidate.get("geometry") or {}))
            if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                return None
            metric_plots.append((candidate, geometry))

        metric_features = []
        for feature in remaining_features:
            try:
                geometry = shapely_transform(forward, shape(feature.get("geometry") or {}))
            except Exception:
                return None
            if geometry.is_empty:
                return None
            metric_features.append((feature, geometry))

        def shift_geometry(geometry, side: int):
            parts = list(geometry.geoms) if geometry.geom_type in {"MultiPolygon", "MultiLineString", "GeometryCollection"} else [geometry]
            shifted_parts = []
            moved = False
            for part in parts:
                if part.is_empty:
                    continue
                signed_distance = ((part.centroid.x - road_center.x) * normal_x) + ((part.centroid.y - road_center.y) * normal_y)
                should_move = signed_distance * side > max(width_m * 0.1, 0.5)
                if should_move:
                    part = shapely_transform(lambda x, y, z=None: (x - normal_x * width_m * side, y - normal_y * width_m * side), part)
                    moved = True
                shifted_parts.append(part)
            if not moved:
                return geometry, False
            return (unary_union(shifted_parts) if len(shifted_parts) > 1 else shifted_parts[0]), True

        def try_side(side: int):
            shifted_plots = []
            moved_plot_count = 0
            for candidate, geometry in metric_plots:
                shifted, moved = shift_geometry(geometry, side)
                shifted_plots.append((candidate, shifted))
                moved_plot_count += int(moved)
            if not moved_plot_count:
                return None

            shifted_features = []
            for feature, geometry in metric_features:
                # Drainage reserves are boundary infrastructure and must not be moved as part of
                # a road compaction. Roads/open-space components move with the selected layout side.
                shifted, moved = (geometry, False) if feature.get("feature_type") == "drainage" else shift_geometry(geometry, side)
                shifted_features.append((feature, shifted, moved))

            all_footprints = []
            for _, geometry in shifted_plots:
                all_footprints.append(geometry)
            for feature, geometry, _ in shifted_features:
                if feature.get("feature_type") == "road":
                    all_footprints.append(geometry.buffer(float(feature.get("width_m") or 9.0) / 2, cap_style=2).intersection(metric_boundary))
                elif geometry.geom_type in {"Polygon", "MultiPolygon"}:
                    all_footprints.append(geometry)
            if any(not boundary_check.covers(geometry) for _, geometry in shifted_plots if not geometry.is_empty):
                return None
            if any(not boundary_check.covers(geometry) for _, geometry, _ in shifted_features if geometry.geom_type in {"Polygon", "MultiPolygon"} and not geometry.is_empty):
                return None
            total_area = sum(float(geometry.area) for geometry in all_footprints)
            occupied_area = float(unary_union(all_footprints).area) if all_footprints else 0.0
            # A valid compaction may make boundaries touch, but it must not make plots overlap
            # remaining roads, reserves, or one another.
            if total_area - occupied_area > max(2.0, total_area * 1e-6):
                return None

            updated_plots = []
            for candidate, geometry in shifted_plots:
                geometry_wgs84 = shapely_transform(backward, geometry)
                area_sqm, issues = validate_polygon(mapping(geometry_wgs84))
                if any(issue.severity == "error" for issue in issues):
                    return None
                updated_plots.append({**candidate, "geometry": mapping(geometry_wgs84), "area_sqm": round(float(area_sqm), 2), "valid": True, "issues": []})
            updated_features = []
            for feature, geometry, moved in shifted_features:
                updated_features.append({**feature, "geometry": mapping(shapely_transform(backward, geometry))} if moved else feature)
            return updated_plots, updated_features

        # Try both directions. The first valid direction moves the side that can be compacted
        # without leaving the estate boundary or colliding with a fixed reserve.
        return try_side(1) or try_side(-1)
    except Exception:
        return None


def _require_layout_approval_access(db: Session, request: Request, organization_id: int):
    """Allow the existing plot managers or survey managers to approve a concept layout."""
    principal = resolve_estate_principal(db, request)
    access = next((item for item in list_estate_access(db, principal) if item.organization_id == int(organization_id)), None)
    if access is None:
        raise HTTPException(status_code=404, detail="Estate organization was not found")
    if not (has_permission(access.role_key, "plot.manage") or has_permission(access.role_key, "survey.manage")):
        raise HTTPException(status_code=403, detail="You do not have permission to approve this Estate layout")
    return access


@router.get("")
def list_estates(request: Request, db: Session = Depends(get_db)):
    principal = resolve_estate_principal(db, request)
    access = list_estate_access(db, principal)
    rows = []
    for item in access:
        if not get_estate_entitlement(db, item.organization_id, "ESTATES_ENABLED").is_enabled:
            continue
        rows.extend(db.query(Estate).filter(Estate.organization_id == item.organization_id, Estate.archived_at.is_(None)).all())
    return [{"id": row.id, "uid": row.estate_uid, "name": row.name, "status": row.status, "organization_id": row.organization_id, "location": row.location_text, "crs": row.crs, "unit_system": row.unit_system, "project_reference": row.project_reference, "project_owner": row.project_owner, "public_enabled": bool(row.public_enabled), "public_slug": row.public_slug} for row in rows]


@router.post("/organizations/{organization_id}")
def create_estate(organization_id: int, payload: EstateCreate, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="estate.manage")
    _enabled(db, organization_id)
    boundary = None
    if payload.boundary:
        _, issues = validate_polygon(payload.boundary)
        if any(issue.severity == "error" for issue in issues):
            raise HTTPException(status_code=422, detail=[issue.message for issue in issues])
        boundary = from_shape(shape(payload.boundary), srid=4326)
    estate = Estate(
        organization_id=organization_id,
        name=payload.name.strip(),
        description=payload.description,
        state=payload.state,
        locality=payload.locality,
        location_text=payload.location_text,
        crs=payload.crs.strip(),
        datum=payload.datum,
        unit_system=payload.unit_system,
        approximate_area_sqm=payload.approximate_area_sqm,
        project_reference=payload.project_reference,
        project_owner=payload.project_owner,
        ownership_details=payload.ownership_details,
        boundary=boundary,
        created_by_subject_type=access.principal.subject_type,
        created_by_subject_id=access.principal.subject_id,
    )
    db.add(estate); db.flush()
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="estate.created", entity_type="estate", entity_id=estate.id, after_data={"name": estate.name, "boundary_present": bool(boundary)})
    db.commit()
    return {
        "id": estate.id,
        "uid": estate.estate_uid,
        "name": estate.name,
        "status": estate.status,
        "crs": estate.crs,
        "unit_system": estate.unit_system,
        "project_reference": estate.project_reference,
    }


@router.patch("/{estate_id}")
def update_estate(estate_id: int, payload: EstateUpdate, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.manage")
    before = _estate_metadata_snapshot(estate)
    values = payload.model_dump(exclude_unset=True)
    if "boundary" in values:
        if values["boundary"] is None:
            estate.boundary = None
        else:
            _, issues = validate_polygon(values["boundary"])
            if any(issue.severity == "error" for issue in issues):
                raise HTTPException(422, detail=[issue.message for issue in issues])
            estate.boundary = from_shape(shape(values["boundary"]), srid=4326)
    values.pop("boundary", None)
    for key, value in values.items():
        setattr(estate, key, value.strip() if isinstance(value, str) else value)
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="estate.updated",
        entity_type="estate",
        entity_id=estate.id,
        before_data=before,
        after_data=_estate_metadata_snapshot(estate),
    )
    db.commit()
    return estate_detail(estate_id, request, db)


@router.delete("/{estate_id}")
def delete_estate(estate_id: int, request: Request, db: Session = Depends(get_db), force: bool = False):
    """Archives an Estate so it disappears from the picker and every listing. This does not hard-
    delete its plots, payments, customers or documents - a cascade across that much linked
    financial/customer history is too risky to offer from a single confirm dialog, and archiving
    (the same mechanism list_estates already filters on) is reversible from the database if this
    was a mistake. If any plot in the Estate carries a customer reservation or allocation, that is
    surfaced as a 409 with the count so the caller can warn the user - passing force=true proceeds
    anyway (the allocations/payments/customers themselves are untouched, only the Estate record is
    archived), for an operator who has confirmed they still want it gone."""
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.manage")
    if estate.archived_at is not None:
        raise HTTPException(409, "This Estate has already been deleted")
    allocated_count = db.query(EstateAllocation).join(EstatePlot, EstateAllocation.plot_id == EstatePlot.id).filter(EstatePlot.estate_id == estate_id).count()
    if allocated_count and not force:
        raise HTTPException(409, {"message": f"{allocated_count} plot(s) in this Estate have a customer reservation or allocation.", "allocated_count": allocated_count, "requires_force": True})
    estate.archived_at = datetime.now(timezone.utc)
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="estate.deleted", entity_type="estate", entity_id=estate.id, after_data={"name": estate.name, "forced": bool(force and allocated_count), "allocated_plot_count": allocated_count})
    db.commit()
    return {"id": estate.id, "deleted": True, "forced": bool(force and allocated_count)}


@router.post("/{estate_id}/approve-map")
def approve_estate_map(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.manage")
    quality = estate_quality_check(estate_id, request, db)
    if not quality["plot_count"]:
        raise HTTPException(409, "Add at least one approved plot before publishing the Estate map")
    if quality["review_required"]:
        raise HTTPException(409, "Resolve geometry issues before publishing the Estate map")
    previous = estate.status
    estate.status = "active"
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="estate.map_approved",
        entity_type="estate",
        entity_id=estate.id,
        before_data={"status": previous},
        after_data={"status": estate.status, "plot_count": quality["plot_count"]},
    )
    db.commit()
    return {"id": estate.id, "status": estate.status, "plot_count": quality["plot_count"], "published": True}


@router.get("/{estate_id}/dashboard")
def estate_dashboard(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate: raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="estate.read"); _enabled(db, estate.organization_id)
    status_rows = db.query(EstatePlot.commercial_status, func.count(EstatePlot.id)).filter(EstatePlot.estate_id == estate_id).group_by(EstatePlot.commercial_status).all()
    development_rows = db.query(EstatePlot.development_status, func.count(EstatePlot.id)).filter(EstatePlot.estate_id == estate_id).group_by(EstatePlot.development_status).all()
    counts = {key: int(dict(status_rows).get(key, 0)) for key in ("available", "reserved", "allocated", "on_hold")}
    development = {key: int(dict(development_rows).get(key, 0)) for key in ("not_started", "site_cleared", "foundation", "under_construction", "developed")}
    total_plots = int(db.query(func.count(EstatePlot.id)).filter(EstatePlot.estate_id == estate_id).scalar() or 0)
    mapped_area_sqm = Decimal(str(db.query(func.coalesce(func.sum(EstatePlot.area_sqm), 0)).filter(EstatePlot.estate_id == estate_id).scalar() or 0))
    allocations = db.query(EstateAllocation).filter(EstateAllocation.estate_id == estate_id).all()
    financials = _bulk_financial_summaries(db, allocations)
    contracted_sales = sum((summary["agreed_price"] for summary in financials.values()), Decimal(0))
    confirmed_collections = sum((summary["confirmed_paid"] for summary in financials.values()), Decimal(0))
    pending_collections = sum((summary["pending_paid"] for summary in financials.values()), Decimal(0))
    outstanding_balance = sum((summary["outstanding"] for summary in financials.values()), Decimal(0))
    staked_plot_ids = {task.plot_id for task in db.query(EstateStakingTask).filter(EstateStakingTask.estate_id == estate_id).all()}
    allocated_plot_ids = {plot_id for (plot_id,) in db.query(EstatePlot.id).filter(EstatePlot.estate_id == estate_id, EstatePlot.commercial_status == "allocated").all()}
    survey_rows = db.query(EstateSurveyRequest.plot_id, EstateSurveyRequest.status).filter(EstateSurveyRequest.estate_id == estate_id).all()
    eligible_survey_plot_ids = {plot_id for plot_id, status in survey_rows if plot_id in allocated_plot_ids and status in {"in_progress", "ready_for_review", "approved", "completed"}}
    completed_survey_plot_ids = {plot_id for plot_id, status in survey_rows if status in {"approved", "completed"}}
    return {
        "estate": {"id": estate.id, "name": estate.name, "status": estate.status},
        "total_plots": total_plots,
        "statuses": {**counts, "sold": counts["allocated"]},
        "development": development,
        "staked_plots": len(staked_plot_ids),
        "awaiting_survey": len(allocated_plot_ids - eligible_survey_plot_ids),
        "survey_completed": len(completed_survey_plot_ids),
        "awaiting_staking": len(allocated_plot_ids - staked_plot_ids),
        "geometry_issues": int(db.query(func.count(EstatePlot.id)).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry_status != "approved").scalar() or 0),
        "mapped_area_sqm": float(mapped_area_sqm),
        "financial": {
            "contracted_sales_value": str(contracted_sales),
            "confirmed_collections": str(confirmed_collections),
            "pending_collections": str(pending_collections),
            "outstanding_balance": str(outstanding_balance),
        },
    }


@router.get("/{estate_id}/public-settings")
def get_public_estate_settings(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="estate.read")
    _enabled(db, estate.organization_id)
    return {
        "estate_id": estate.id,
        "public_enabled": bool(estate.public_enabled),
        "public_slug": estate.public_slug,
        "public_url_path": f"/estates/public/{estate.public_slug}" if estate.public_slug else None,
        "public_description": estate.public_description,
        "public_tagline": estate.public_tagline,
        "public_contact_phone": estate.public_contact_phone,
        "public_logo_path": f"/estates/public/{estate.public_slug}/logo" if estate.public_logo_object_key else None,
        "public_show_prices": bool(estate.public_show_prices),
        "payment_plan": estate.public_payment_plan or [],
        "can_publish": estate.status == "active" and db.query(EstatePlot).filter(EstatePlot.estate_id == estate.id, EstatePlot.geometry_status == "approved").count() > 0,
    }


@router.patch("/{estate_id}/public-settings")
def update_public_estate_settings(estate_id: int, payload: PublicEstateSettingsUpdate, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.manage")
    _enabled(db, estate.organization_id)
    approved_plot_count = db.query(EstatePlot).filter(EstatePlot.estate_id == estate.id, EstatePlot.geometry_status == "approved").count()
    if payload.public_enabled and (estate.status != "active" or approved_plot_count == 0):
        raise HTTPException(status_code=409, detail="Approve the Estate map before publishing it publicly")
    requested_slug = estate.public_slug or _unique_public_slug(db, estate)
    before = {"public_enabled": estate.public_enabled, "public_slug": estate.public_slug, "public_show_prices": estate.public_show_prices, "payment_plan": estate.public_payment_plan}
    estate.public_enabled = payload.public_enabled
    estate.public_slug = requested_slug
    estate.public_description = payload.public_description.strip() if payload.public_description else None
    estate.public_tagline = payload.public_tagline.strip() if payload.public_tagline else None
    estate.public_contact_phone = payload.public_contact_phone.strip() if payload.public_contact_phone else None
    estate.public_show_prices = payload.public_show_prices
    estate.public_payment_plan = [{"label": item.label.strip(), "percentage": str(item.percentage)} for item in payload.payment_plan] if payload.payment_plan else None
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="estate.public_settings.updated",
        entity_type="estate",
        entity_id=estate.id,
        before_data=before,
        after_data={"public_enabled": estate.public_enabled, "public_slug": estate.public_slug, "public_tagline": estate.public_tagline, "public_show_prices": estate.public_show_prices, "payment_plan": estate.public_payment_plan},
    )
    db.commit()
    return get_public_estate_settings(estate_id, request, db)


def _estate_forecast_boundary(db: Session, estate: Estate) -> dict:
    if estate.boundary:
        return mapping(to_shape(estate.boundary))
    geometries = [to_shape(plot.geometry) for plot in db.query(EstatePlot).filter(
        EstatePlot.estate_id == estate.id,
        EstatePlot.geometry_status == "approved",
        EstatePlot.geometry.isnot(None),
    ).all()]
    if not geometries:
        raise HTTPException(status_code=422, detail="Add an Estate boundary or approve plot geometry before running a development forecast")
    return mapping(unary_union(geometries).convex_hull)


@router.get("/{estate_id}/development-forecast")
def get_estate_development_forecast(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="estate.read")
    _enabled(db, estate.organization_id)
    _require_plus_plan(
        db,
        estate.organization_id,
        "Development outlook (flood, erosion and land-cover growth analysis)",
    )
    return {"estate_id": estate.id, "forecast": estate.public_development_forecast}


def _run_estate_development_forecast_job(job_id: str) -> None:
    """Run the three geospatial inputs away from the request thread, then persist one reviewed result."""
    db = SessionLocal()
    try:
        job = get_hazard_job(db, job_id)
        if not job:
            return
        payload = job.get("request_payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        estate = db.get(Estate, int(payload["estate_id"]))
        if not estate:
            set_hazard_job_status(db, job_id, status="failed", stage="Failed", error_text="Estate not found", completed=True)
            return
        if not has_hazard_access(get_subscription(db, estate.organization_id)):
            set_hazard_job_status(
                db,
                job_id,
                status="failed",
                stage="Plus plan required",
                error_text="Development outlook is available on the Plus plan. Upgrade to unlock it.",
                completed=True,
            )
            return

        set_hazard_job_status(db, job_id, status="running", stage="Starting forecast...", progress_pct=1, started=True)
        report = make_progress_reporter(db, job_id)
        boundary = payload["boundary"]
        flood_result = None
        erosion_result = None
        from app.routers.hazards import erosion_preview, flood_preview

        report("Running flood screening...", 5)
        try:
            flood_result = flood_preview({"boundary": boundary, "show_raster": False}, db)
        except Exception:
            db.rollback()
        report("Running erosion screening...", 25)
        try:
            erosion_result = erosion_preview({"boundary": boundary, "show_raster": False}, db)
        except Exception:
            db.rollback()

        planned_roads_count = int(db.query(EstateSpatialFeature.id).filter(
            EstateSpatialFeature.estate_id == estate.id,
            EstateSpatialFeature.feature_type == "road",
            EstateSpatialFeature.status == "active",
        ).count())
        from app.services.estates.development_forecast import compute_development_forecast

        forecast = compute_development_forecast(
            boundary,
            flood_result=flood_result,
            erosion_result=erosion_result,
            planned_roads_count=planned_roads_count,
            progress_cb=report,
        )
        # A rerun always needs a fresh human review before it can replace the public story.
        forecast["published"] = False
        estate.public_development_forecast = forecast
        actor = SimpleNamespace(subject_type=payload.get("subject_type"), subject_id=payload.get("subject_id"))
        append_estate_audit_event(
            db,
            organization_id=estate.organization_id,
            actor=actor,
            action="estate.development_forecast.generated",
            entity_type="estate",
            entity_id=estate.id,
            after_data={"data_available": forecast.get("data_available"), "published": False},
        )
        db.commit()
        set_hazard_job_status(db, job_id, status="completed", stage="Complete", progress_pct=100, result_payload=forecast, completed=True)
    except Exception as exc:
        db.rollback()
        set_hazard_job_status(db, job_id, status="failed", stage="Failed", error_text=str(exc), completed=True)
    finally:
        db.close()


@router.post("/{estate_id}/development-forecast/run")
def run_estate_development_forecast(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.manage")
    _enabled(db, estate.organization_id)
    _require_plus_plan(
        db,
        estate.organization_id,
        "Development outlook (flood, erosion and land-cover growth analysis)",
    )
    boundary = _estate_forecast_boundary(db, estate)
    job = insert_hazard_job(
        db,
        hazard_type="estate_development_forecast",
        output_type="preview",
        request_payload={
            "estate_id": estate.id,
            "boundary": boundary,
            "subject_type": access.principal.subject_type,
            "subject_id": access.principal.subject_id,
        },
        worker=_run_estate_development_forecast_job,
    )
    return serialize_hazard_job(job)


@router.patch("/{estate_id}/development-forecast/public")
def publish_estate_development_forecast(estate_id: int, payload: DevelopmentForecastPublishUpdate, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.manage")
    _enabled(db, estate.organization_id)
    _require_plus_plan(
        db,
        estate.organization_id,
        "Development outlook (flood, erosion and land-cover growth analysis)",
    )
    forecast = dict(estate.public_development_forecast or {})
    if payload.public_enabled and not forecast.get("data_available"):
        raise HTTPException(status_code=409, detail="Run a complete development forecast before publishing it")
    before = bool(forecast.get("published"))
    forecast["published"] = bool(payload.public_enabled)
    estate.public_development_forecast = forecast
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="estate.development_forecast.visibility_updated",
        entity_type="estate",
        entity_id=estate.id,
        before_data={"published": before},
        after_data={"published": forecast["published"]},
    )
    db.commit()
    return {"estate_id": estate.id, "forecast": forecast}


@router.post("/{estate_id}/public-logo")
async def upload_public_estate_logo(estate_id: int, request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.manage")
    _enabled(db, estate.organization_id)
    if str(file.content_type or "").lower() not in {"image/png", "image/jpeg"}:
        raise HTTPException(422, "Upload a PNG or JPEG logo")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "Logo must be 5 MB or smaller")
    organization = db.get(EstateOrganization, estate.organization_id)
    if not organization:
        raise HTTPException(404, "Estate company not found")
    stored = store_private_estate_file(
        organization_uid=organization.organization_uid,
        category="public-assets",
        entity_uid=f"estate_{estate.estate_uid}",
        filename=file.filename or "estate-logo.png",
        content_type=file.content_type or "",
        data=data,
    )
    previous_key = estate.public_logo_object_key
    estate.public_logo_object_key = stored.object_key
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="estate.public_logo_updated",
        entity_type="estate",
        entity_id=estate.id,
        after_data={"filename": stored.filename},
    )
    db.commit()
    if previous_key:
        settings = build_r2_settings(prefix="R2")
        if settings:
            delete_object_best_effort(settings, previous_key)
    return {"logo_path": f"/estates/public/{estate.public_slug}/logo" if estate.public_slug else None}


@router.get("/{estate_id}/reservation-requests")
def list_public_reservation_requests(estate_id: int, request: Request, status: str | None = None, page: int | None = None, page_size: int = 8, search: str | None = None, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="allocation.read")
    query = db.query(EstatePublicReservationRequest).filter(EstatePublicReservationRequest.estate_id == estate.id)
    if status and status != "all":
        if status not in {"new", "contacted", "converted", "declined"}:
            raise HTTPException(status_code=422, detail="Unknown reservation request status")
        query = query.filter(EstatePublicReservationRequest.status == status)
    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(EstatePublicReservationRequest.full_name).like(term),
                func.lower(EstatePublicReservationRequest.phone).like(term),
                func.lower(EstatePublicReservationRequest.email).like(term),
            )
        )
    ordered = query.order_by(EstatePublicReservationRequest.created_at.desc())
    if page is None:
        rows = ordered.limit(200).all()
        return [_public_reservation_payload(db, row) for row in rows]
    safe_page = max(1, page)
    safe_page_size = min(max(page_size, 1), 50)
    total = ordered.count()
    rows = ordered.offset((safe_page - 1) * safe_page_size).limit(safe_page_size).all()
    new_count = query.filter(EstatePublicReservationRequest.status == "new").count()
    return {"items": [_public_reservation_payload(db, row) for row in rows], "page": safe_page, "page_size": safe_page_size, "total": total, "new_count": new_count}


@router.patch("/reservation-requests/{request_id}")
def update_public_reservation_request(request_id: int, payload: PublicReservationUpdate, request: Request, db: Session = Depends(get_db)):
    row = db.get(EstatePublicReservationRequest, request_id)
    if not row:
        raise HTTPException(404, "Reservation request not found")
    access = require_estate_access(db, request, row.organization_id, permission="allocation.manage")
    if payload.status == "converted" and not row.allocation_id:
        raise HTTPException(status_code=409, detail="Use Migrate to customer to create the customer and reserve this plot")
    previous = row.status
    row.status = payload.status
    if "staff_notes" in payload.model_fields_set:
        row.staff_notes = payload.staff_notes.strip() if payload.staff_notes else None
    now = datetime.now(timezone.utc)
    if payload.status == "contacted" and row.contacted_at is None:
        row.contacted_at = now
    if payload.status in {"converted", "declined"}:
        row.resolved_at = row.resolved_at or now
    elif payload.status in {"new", "contacted"}:
        row.resolved_at = None
    append_estate_audit_event(
        db,
        organization_id=row.organization_id,
        actor=access.principal,
        action="public_reservation.updated",
        entity_type="estate_public_reservation_request",
        entity_id=row.id,
        before_data={"status": previous},
        after_data={"status": row.status, "staff_notes": row.staff_notes},
    )
    db.commit()
    return _public_reservation_payload(db, row)


@router.post("/reservation-requests/{request_id}/convert")
def convert_public_reservation_request(
    request_id: int,
    payload: PublicReservationConvert,
    request: Request,
    idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"),
    db: Session = Depends(get_db),
):
    """Turn a public sales lead into the normal Estate customer and reservation workflow."""
    row = db.get(EstatePublicReservationRequest, request_id)
    if not row:
        raise HTTPException(404, "Reservation request not found")
    access = require_estate_access(db, request, row.organization_id, permission="allocation.manage")
    idempotency_key = _normalize_idempotency_key(idempotency_header)
    if row.allocation_id:
        return {**_public_reservation_payload(db, row), "customer_notified": False, "already_converted": True}
    if idempotency_key:
        existing = db.query(EstateAllocation).filter(EstateAllocation.organization_id == access.organization_id, EstateAllocation.idempotency_key == idempotency_key).one_or_none()
        if existing:
            row.allocation_id = existing.id
            row.customer_id = existing.customer_id
            row.status = "converted"
            db.commit()
            return {**_public_reservation_payload(db, row), "customer_notified": False, "already_converted": True}
    if row.status == "declined":
        raise HTTPException(409, detail="A declined reservation request cannot be migrated")
    plot = db.query(EstatePlot).filter(EstatePlot.id == row.plot_id, EstatePlot.estate_id == row.estate_id).one_or_none()
    if not plot:
        raise HTTPException(404, "The requested plot no longer exists")
    if plot.commercial_status != "available":
        raise HTTPException(409, detail="This plot is no longer available to reserve")
    estate = db.get(Estate, row.estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    if not payload.initial_payment_amount:
        raise HTTPException(422, "Record the customer's first payment before creating a proper reservation.")
    customer = EstateCustomer(
        organization_id=row.organization_id,
        full_name=row.full_name,
        full_name_normalized=_normalized(row.full_name),
        phone=row.phone,
        email=row.email,
        notes=row.message,
    )
    db.add(customer)
    db.flush()
    agreed_price = payload.agreed_price if payload.agreed_price is not None else plot.asking_price
    scheduled_payment_plan = None
    scheduled_next_due_at = payload.next_payment_due_at
    if payload.payment_schedule:
        scheduled_payment_plan, scheduled_next_due_at = _allocation_payment_fields(payload)
    configured_payment_plan = "; ".join(
        f"{item.get('label')}: {item.get('percentage')}%" for item in (estate.public_payment_plan or [])
    ) or None
    try:
        allocation = reserve_or_allocate(
            db,
            plot=plot,
            customer=customer,
            actor=access.principal,
            allocate=False,
            agreed_price=agreed_price,
            payment_plan=scheduled_payment_plan or payload.payment_plan or configured_payment_plan,
            next_payment_due_at=scheduled_next_due_at,
            notes=payload.notes or "Created from public reservation request",
        )
        allocation.idempotency_key = idempotency_key
        initial_payment = _apply_initial_payment(db, record=allocation, payload=payload, actor=access.principal, idempotency_key=idempotency_key)
    except IntegrityError:
        db.rollback()
        existing_row = db.get(EstatePublicReservationRequest, request_id)
        if existing_row and existing_row.allocation_id:
            return {**_public_reservation_payload(db, existing_row), "customer_notified": False, "already_converted": True}
        raise HTTPException(status_code=409, detail="This public reservation was already converted or the plot was reserved by another request")
    if row.assigned_agent_subject_type and row.assigned_agent_subject_id:
        allocation.sales_agent_subject_type = row.assigned_agent_subject_type
        allocation.sales_agent_subject_id = row.assigned_agent_subject_id
    allocation.lead_source_code = row.source_code
    allocation.lead_source_channel = row.source_channel
    row.customer_id = customer.id
    row.allocation_id = allocation.id
    row.status = "converted"
    row.contacted_at = row.contacted_at or datetime.now(timezone.utc)
    row.resolved_at = datetime.now(timezone.utc)
    append_estate_audit_event(
        db,
        organization_id=row.organization_id,
        actor=access.principal,
        action="public_reservation.converted",
        entity_type="estate_public_reservation_request",
        entity_id=row.id,
        after_data={"customer_id": customer.id, "allocation_id": allocation.id, "plot_id": plot.id},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing_row = db.get(EstatePublicReservationRequest, request_id)
        if existing_row and existing_row.allocation_id:
            return {**_public_reservation_payload(db, existing_row), "customer_notified": False, "already_converted": True}
        raise HTTPException(status_code=409, detail="This public reservation was already converted or the plot was reserved by another request")
    customer_notified = _notify_allocation_customer(
        db,
        allocation=allocation,
        org_name=access.organization_name,
        event="reserved",
    )
    return {**_public_reservation_payload(db, row), "customer_notified": customer_notified, "initial_payment_id": initial_payment.id if initial_payment else None, "already_converted": False}

@router.get("/{estate_id}/plots.geojson")
def estate_plots_geojson(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    require_estate_access(db,request,estate.organization_id,permission="plot.read")
    rows = db.query(
        EstatePlot.id,
        EstatePlot.plot_number,
        EstatePlot.commercial_status,
        EstatePlot.development_status,
        EstatePlot.geometry_status,
        EstatePlot.area_sqm,
        EstatePlot.public_address,
        EstatePlot.asking_price,
        EstatePlot.block_id,
        # Seven decimal places is centimetre-level for the map and avoids returning a large
        # amount of floating-point noise for every vertex in a large layout.
        func.ST_AsGeoJSON(EstatePlot.geometry, 7).label("geometry_json"),
    ).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry.isnot(None)).all()
    features = []
    for row in rows:
        try:
            geometry = json.loads(row.geometry_json) if isinstance(row.geometry_json, str) else row.geometry_json
        except (TypeError, ValueError):
            geometry = None
        if geometry is None:
            continue
        features.append({"type": "Feature", "id": row.id, "properties": {"id": row.id, "plot_number": row.plot_number, "commercial_status": row.commercial_status, "development_status": row.development_status, "geometry_status": row.geometry_status, "area_sqm": float(row.area_sqm or 0), "public_address": row.public_address, "asking_price": str(row.asking_price) if row.asking_price is not None else None, "block_id": row.block_id}, "geometry": geometry})
    return {"type": "FeatureCollection", "features": features}

@router.get("/{estate_id}/layers.geojson")
def estate_layers_geojson(estate_id:int, request:Request, db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    require_estate_access(db,request,estate.organization_id,permission="infrastructure.read")
    rows=db.query(EstateSpatialFeature).filter(EstateSpatialFeature.estate_id==estate_id, EstateSpatialFeature.status=="active").all()
    return {"type":"FeatureCollection","features":[{"type":"Feature","id":row.id,"properties":{"id":row.id,"type":row.feature_type,"name":row.name,"status":row.status},"geometry":mapping(to_shape(row.geometry))} for row in rows]}


@router.get("/{estate_id}/blocks")
def list_estate_blocks(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="plot.read")
    rows = db.query(EstateBlock).filter(EstateBlock.estate_id == estate_id).order_by(EstateBlock.label.asc()).all()
    return [{"id": row.id, "label": row.label, "name": row.name, "notes": row.notes, "geometry": mapping(to_shape(row.geometry)) if row.geometry else None} for row in rows]


@router.post("/{estate_id}/blocks")
def create_estate_block(estate_id: int, payload: BlockCreate, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="infrastructure.manage")
    if db.query(EstateBlock).filter(EstateBlock.estate_id == estate_id, EstateBlock.label == payload.label.strip()).first():
        raise HTTPException(409, "Block label already exists in this estate")
    geometry = _geojson_geometry(payload.geometry) if payload.geometry else None
    if geometry is not None and geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise HTTPException(422, "Block geometry must be a polygon")
    row = EstateBlock(estate_id=estate_id, label=payload.label.strip(), name=payload.name, notes=payload.notes, geometry=from_shape(geometry, srid=4326) if geometry else None)
    db.add(row)
    db.flush()
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="estate_block.created", entity_type="estate_block", entity_id=row.id, after_data={"label": row.label})
    db.commit()
    return {"id": row.id, "label": row.label, "name": row.name}


@router.patch("/{estate_id}/blocks/{block_id}")
def update_estate_block(estate_id: int, block_id: int, payload: BlockUpdate, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    row = db.get(EstateBlock, block_id)
    if not estate or not row or row.estate_id != estate_id:
        raise HTTPException(404, "Block not found")
    access = require_estate_access(db, request, estate.organization_id, permission="infrastructure.manage")
    before = {"label": row.label, "name": row.name, "notes": row.notes}
    values = payload.model_dump(exclude_unset=True)
    if "label" in values:
        duplicate = db.query(EstateBlock).filter(EstateBlock.estate_id == estate_id, EstateBlock.label == values["label"].strip(), EstateBlock.id != row.id).first()
        if duplicate:
            raise HTTPException(409, "Block label already exists in this estate")
        row.label = values["label"].strip()
    for key in ("name", "notes"):
        if key in values:
            setattr(row, key, values[key])
    if "geometry" in values and values["geometry"] is not None:
        geometry = _geojson_geometry(values["geometry"])
        if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            raise HTTPException(422, "Block geometry must be a polygon")
        row.geometry = from_shape(geometry, srid=4326)
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="estate_block.updated", entity_type="estate_block", entity_id=row.id, before_data=before, after_data={"label": row.label, "name": row.name, "notes": row.notes})
    db.commit()
    return {"id": row.id, "label": row.label, "name": row.name}

@router.post("/{estate_id}/import-reviews")
def create_import_review(estate_id:int,payload:ImportReviewCreate,request:Request,db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    access=require_estate_access(db,request,estate.organization_id,permission="plot.manage")
    row=EstateImportReview(organization_id=estate.organization_id,estate_id=estate.id,source_type=payload.source_type,survey_georeference_session_id=payload.survey_georeference_session_id,notes=payload.notes,created_by_subject_type=access.principal.subject_type,created_by_subject_id=access.principal.subject_id)
    db.add(row); db.flush(); append_estate_audit_event(db,organization_id=estate.organization_id,actor=access.principal,action="import_review.created",entity_type="estate_import_review",entity_id=row.id,after_data={"source_type":row.source_type}); db.commit()
    return {"id":row.id,"status":row.status}

def _normalize_csv_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").strip().lower())


def _normalize_csv_records(raw_records: list[dict]) -> list[dict]:
    """Real spreadsheet exports rarely use our exact lowercase column names (Excel/QGIS/AutoCAD
    commonly ship "Plot Number", "Longitude", "Easting (m)", etc.) - matching only on the raw
    header silently dropped every row and reported "no usable plots" with no indication why.
    Normalizing each row's keys once (lowercased, trimmed, non-alphanumerics stripped) lets the
    same handful of `record.get(...)` calls the caller uses match the header spelling people
    actually use."""
    return [{_normalize_csv_header(key): value for key, value in record.items()} for record in raw_records]


@router.post("/{estate_id}/import-reviews/csv")
async def import_estate_csv(estate_id: int, request: Request, source_crs: str | None = None, file: UploadFile = File(...), db: Session = Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    access=require_estate_access(db,request,estate.organization_id,permission="plot.manage")
    try: raw_records=list(csv.DictReader(io.StringIO((await file.read()).decode("utf-8-sig"))))
    except Exception as exc: raise HTTPException(422,"CSV could not be read") from exc
    records = _normalize_csv_records(raw_records)
    candidates = []
    has_geometry = any(str(record.get("geometry") or "").strip() for record in records)
    if has_geometry:
        for index, record in enumerate(records, 1):
            try:
                geometry = json.loads(record.get("geometry") or "")
                candidates.append(_import_candidate(row_number=index, plot_number=str(record.get("plotnumber") or ""), geometry=geometry))
            except Exception:
                candidates.append({"row": index, "plot_number": str(record.get("plotnumber") or "").strip(), "valid": False, "issues": ["geometry must be a GeoJSON Polygon"]})
    else:
        grouped: dict[str, list[list[float]]] = {}
        first_rows: dict[str, int] = {}
        try:
            coordinate_transformer = Transformer.from_crs((source_crs or "EPSG:4326").strip(), "EPSG:4326", always_xy=True)
        except Exception as exc:
            raise HTTPException(status_code=422, detail="CSV source_crs is invalid") from exc
        for index, record in enumerate(records, 1):
            number = str(record.get("plotnumber") or record.get("plot") or record.get("plotno") or record.get("plotid") or "").strip()
            x_value = record.get("longitude") or record.get("lng") or record.get("lon") or record.get("easting") or record.get("eastingm") or record.get("x")
            y_value = record.get("latitude") or record.get("lat") or record.get("northing") or record.get("northingm") or record.get("y")
            try:
                if not number or x_value in (None, "") or y_value in (None, ""):
                    raise ValueError
                x, y = coordinate_transformer.transform(float(x_value), float(y_value))
                grouped.setdefault(number, []).append([x, y])
                first_rows.setdefault(number, index)
            except (TypeError, ValueError):
                candidates.append({"row": index, "plot_number": number, "valid": False, "issues": ["CSV rows require plot_number, longitude/latitude (or x/y) coordinates"]})
        for number, points in grouped.items():
            if len(points) >= 3:
                ring = points if points[0] == points[-1] else points + [points[0]]
                candidates.append(_import_candidate(row_number=first_rows[number], plot_number=number, geometry={"type": "Polygon", "coordinates": [ring]}))
            else:
                candidates.append({"row": first_rows[number], "plot_number": number, "valid": False, "issues": ["A parcel requires at least three coordinate rows"]})
    if not candidates: raise HTTPException(422,"CSV contains no rows")
    row=EstateImportReview(organization_id=estate.organization_id,estate_id=estate.id,source_type="csv",notes=f"{len(candidates)} candidate parcel(s) imported from {file.filename or 'CSV'}",candidate_data=candidates,created_by_subject_type=access.principal.subject_type,created_by_subject_id=access.principal.subject_id)
    db.add(row); db.flush(); append_estate_audit_event(db,organization_id=estate.organization_id,actor=access.principal,action="import_review.csv_uploaded",entity_type="estate_import_review",entity_id=row.id,after_data={"candidate_count":len(candidates),"valid_count":sum(1 for candidate in candidates if candidate["valid"])}) ; db.commit()
    return {"id":row.id,"status":row.status,"candidate_count":len(candidates),"valid_count":sum(1 for candidate in candidates if candidate["valid"]),"source_crs":source_crs or "EPSG:4326"}

@router.post("/{estate_id}/import-reviews/geojson")
async def import_estate_geojson(estate_id:int,request:Request,file:UploadFile=File(...),db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    access=require_estate_access(db,request,estate.organization_id,permission="plot.manage")
    try: document=json.loads((await file.read()).decode("utf-8-sig")); features=document.get("features",[]) if document.get("type")=="FeatureCollection" else [document]
    except Exception as exc: raise HTTPException(422,"GeoJSON could not be read") from exc
    candidates=[]
    for index,feature in enumerate(features,1):
        geometry=feature.get("geometry") or {}; properties=feature.get("properties") or {}; number=str(properties.get("plot_number") or properties.get("name") or f"Imported-{index}").strip()
        try: _,issues=validate_polygon(geometry); errors=[issue.message for issue in issues if issue.severity=="error"]
        except Exception: errors=["geometry must be a GeoJSON Polygon"]
        candidates.append({"row":index,"plot_number":number,"geometry":geometry,"valid":not errors,"issues":errors})
    if not candidates: raise HTTPException(422,"GeoJSON contains no features")
    row=EstateImportReview(organization_id=estate.organization_id,estate_id=estate.id,source_type="gis",notes=f"{len(candidates)} candidate parcel(s) imported from {file.filename or 'GeoJSON'}",candidate_data=candidates,created_by_subject_type=access.principal.subject_type,created_by_subject_id=access.principal.subject_id)
    db.add(row); db.flush(); append_estate_audit_event(db,organization_id=estate.organization_id,actor=access.principal,action="import_review.geojson_uploaded",entity_type="estate_import_review",entity_id=row.id,after_data={"candidate_count":len(candidates)}); db.commit()
    return {"id":row.id,"status":row.status,"candidate_count":len(candidates),"valid_count":sum(1 for candidate in candidates if candidate["valid"])}

def _closed_ring_points(entity, raw_points: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """The ring's vertices (without a duplicated closing point) if this polyline traces a closed
    loop - either via its own DXF "closed" flag, or because its first and last vertices coincide.
    Many real-world DXF exporters leave that flag unset even though the polyline visually closes
    (first vertex re-drawn as the last), which previously made a perfectly good closed boundary
    get silently dropped and reported as "no closed polygon polylines". Returns None if the
    entity isn't closed either way."""
    closed_flag = getattr(entity, "is_closed", False)
    if callable(closed_flag):
        closed_flag = closed_flag()
    if bool(closed_flag):
        return raw_points
    if len(raw_points) < 3:
        return None
    first, last = raw_points[0], raw_points[-1]
    xs = [point[0] for point in raw_points]
    ys = [point[1] for point in raw_points]
    extent = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    distance = ((first[0] - last[0]) ** 2 + (first[1] - last[1]) ** 2) ** 0.5
    if distance <= max(extent * 0.001, 1e-6):
        return raw_points[:-1]
    return None


@router.post("/{estate_id}/import-reviews/dxf")
async def import_estate_dxf(estate_id:int,source_crs:str,request:Request,file:UploadFile=File(...),db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    access=require_estate_access(db,request,estate.organization_id,permission="plot.manage")
    handle=None
    try: src=source_crs.strip().upper(); transformer=Transformer.from_crs(src,"EPSG:4326",always_xy=True); content=await file.read(); handle=tempfile.NamedTemporaryFile(suffix=".dxf",delete=False); handle.write(content); handle.close(); document=ezdxf.readfile(handle.name)
    except Exception as exc: raise HTTPException(422,"DXF could not be read; provide a valid source CRS") from exc
    finally:
        try: os.unlink(handle.name if handle else "")
        except Exception: pass
    candidates=[]
    for index,entity in enumerate(document.modelspace().query("LWPOLYLINE POLYLINE"),1):
        try:
            if entity.dxftype() == "LWPOLYLINE":
                raw_points = list(entity.get_points())
            else:
                vertices = entity.vertices() if callable(getattr(entity, "vertices", None)) else entity.vertices
                raw_points = [(vertex.dxf.location.x, vertex.dxf.location.y) for vertex in vertices]
            ring_points = _closed_ring_points(entity, raw_points)
            if ring_points is None:
                continue
            points=[transformer.transform(float(point[0]),float(point[1])) for point in ring_points]
            if len(points)<3: continue
            geometry={"type":"Polygon","coordinates":[[list(point) for point in points+[points[0]]]]}
            _,issues=validate_polygon(geometry); errors=[issue.message for issue in issues if issue.severity=="error"]
            layer_name = str(entity.dxf.layer or "CAD").strip() or "CAD"
            candidates.append({"row":index,"plot_number":layer_name if index == 1 else f"{layer_name}-{index}","geometry":geometry,"valid":not errors,"issues":errors})
        except Exception: continue
    if not candidates: raise HTTPException(422,"DXF contains no closed polygon polylines")
    row=EstateImportReview(organization_id=estate.organization_id,estate_id=estate.id,source_type="cad",notes=f"{len(candidates)} candidate parcel(s) imported from {file.filename or 'DXF'} using {src}",candidate_data=candidates,created_by_subject_type=access.principal.subject_type,created_by_subject_id=access.principal.subject_id)
    db.add(row); db.flush(); append_estate_audit_event(db,organization_id=estate.organization_id,actor=access.principal,action="import_review.dxf_uploaded",entity_type="estate_import_review",entity_id=row.id,after_data={"candidate_count":len(candidates),"source_crs":src}); db.commit()
    return {"id":row.id,"status":row.status,"candidate_count":len(candidates),"valid_count":sum(1 for candidate in candidates if candidate["valid"]),"source_crs":src}


@router.post("/{estate_id}/import-reviews/scanned-layout")
async def import_scanned_layout(
    estate_id: int,
    request: Request,
    survey_georeference_session_id: str | None = Form(default=None),
    candidate_data: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Keep a private scanned plan in the review queue until geometry is verified.

    Optional candidate_data is a JSON array of the same polygon candidates produced by CSV/GIS
    intake. This lets a surveyor attach verified digitisation results to the scan without treating
    OCR or image recognition as authoritative parcel geometry.
    """
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    candidates = []
    if candidate_data:
        try:
            raw_candidates = json.loads(candidate_data)
            if not isinstance(raw_candidates, list):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(422, "candidate_data must be a JSON array") from exc
        for index, item in enumerate(raw_candidates, 1):
            if not isinstance(item, dict):
                candidates.append({"row": index, "plot_number": "", "valid": False, "issues": ["Candidate must be an object"]})
                continue
            geometry = item.get("geometry") or {}
            candidates.append(_import_candidate(row_number=index, plot_number=str(item.get("plot_number") or ""), geometry=geometry))
    review = EstateImportReview(
        organization_id=estate.organization_id,
        estate_id=estate.id,
        source_type="raster" if (file.content_type or "").lower().startswith("image/") else "pdf",
        survey_georeference_session_id=survey_georeference_session_id.strip() if survey_georeference_session_id else None,
        notes=f"Private scanned layout uploaded from {file.filename or 'layout'}; geometry requires surveyor review.",
        candidate_data=candidates,
        created_by_subject_type=access.principal.subject_type,
        created_by_subject_id=access.principal.subject_id,
    )
    db.add(review)
    db.flush()
    organization = db.get(EstateOrganization, estate.organization_id)
    stored = store_private_estate_file(
        organization_uid=organization.organization_uid,
        category="imports",
        entity_uid=f"import_review_{review.id}",
        filename=file.filename or "scanned-layout",
        content_type=file.content_type or "",
        data=await file.read(),
    )
    document = EstateDocument(
        organization_id=estate.organization_id,
        object_key=stored.object_key,
        original_filename=stored.filename,
        mime_type=stored.mime_type,
        size_bytes=stored.size_bytes,
        checksum=stored.checksum,
        document_type="layout_import",
        description="Private source scan for Estate geometry review",
        uploaded_by_subject_type=access.principal.subject_type,
        uploaded_by_subject_id=access.principal.subject_id,
    )
    db.add(document)
    db.flush()
    db.add(EstateDocumentLink(document_id=document.id, entity_type="import_review", entity_id=str(review.id)))
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="import_review.scanned_layout_uploaded",
        entity_type="estate_import_review",
        entity_id=review.id,
        after_data={"document_id": document.id, "filename": stored.filename, "candidate_count": len(candidates)},
    )
    db.commit()
    return {"id": review.id, "status": review.status, "document_id": document.id, "candidate_count": len(candidates)}


@router.get("/{estate_id}/import-reviews")
def list_import_reviews(estate_id:int,request:Request,db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    require_estate_access(db,request,estate.organization_id,permission="plot.read")
    rows=db.query(EstateImportReview).filter(EstateImportReview.estate_id==estate_id).order_by(EstateImportReview.created_at.desc()).all()
    result = []
    for row in rows:
        linked_document = db.query(EstateDocument).join(EstateDocumentLink, EstateDocumentLink.document_id == EstateDocument.id).filter(EstateDocumentLink.entity_type == "import_review", EstateDocumentLink.entity_id == str(row.id)).first()
        result.append({"id": row.id, "source_type": row.source_type, "session_id": row.survey_georeference_session_id, "status": row.status, "notes": row.notes, "candidate_count": len(row.candidate_data or []), "candidates": row.candidate_data or [], "document_id": linked_document.id if linked_document else None, "created_at": row.created_at})
    return result

@router.post("/import-reviews/{review_id}/decision")
def decide_import_review(review_id:int,payload:ImportReviewDecision,request:Request,db:Session=Depends(get_db)):
    row=db.get(EstateImportReview,review_id)
    if not row: raise HTTPException(404,"Import review not found")
    access=require_estate_access(db,request,row.organization_id,permission="survey.manage")
    if row.status != "review_required": raise HTTPException(409,"Import review has already been decided")
    if payload.candidate_data is not None:
        normalized_candidates = []
        for index, candidate in enumerate(payload.candidate_data, 1):
            normalized_candidates.append(_import_candidate(row_number=index, plot_number=str(candidate.get("plot_number") or ""), geometry=candidate.get("geometry") or {}))
        row.candidate_data = normalized_candidates
    created=0
    if payload.status == "approved":
        estate=db.get(Estate,row.estate_id)
        if payload.as_boundary:
            usable = [candidate for candidate in (row.candidate_data or []) if candidate.get("valid") and candidate.get("geometry")]
            if len(usable) != 1:
                raise HTTPException(422, "A boundary import needs exactly one usable shape - approve it as individual plots instead, or fix the file so it contains a single outline.")
            geometry = usable[0]["geometry"]
            _, issues = validate_polygon(geometry)
            if any(issue.severity == "error" for issue in issues):
                raise HTTPException(422, detail=[issue.message for issue in issues])
            estate.boundary = from_shape(shape(geometry), srid=4326)
            row.notes=(payload.notes or row.notes or "") + "; set as the Estate boundary"
        else:
            created=_create_plots_from_candidates(db,estate=estate,candidates=row.candidate_data or [],source_type=row.source_type,source_reference=str(row.id),actor=access.principal)
            row.notes=(payload.notes or row.notes or "") + f"; {created} operational plot(s) created"
    else: row.notes=payload.notes or row.notes
    row.status=payload.status
    append_estate_audit_event(db,organization_id=row.organization_id,actor=access.principal,action=f"import_review.{row.status}",entity_type="estate_import_review",entity_id=row.id,after_data={"status":row.status}); db.commit()
    return {"id":row.id,"status":row.status,"created_plots":created}


def _plot_number_from_georeference_feature(feature: dict, index: int, prefix: str) -> str:
    """A digitized feature's label is whatever the surveyor typed while digitizing (often left at
    the tool's generic default) - only promote it to a plot number when it looks intentional,
    otherwise fall back to a sequential prefix so two auto-generated plots never collide."""
    label = str(feature.get("label") or "").strip()
    generic_label = label.lower() in {"", "polygon", f"polygon {index}"}
    return f"{prefix}{index}" if generic_label else label


def _polygon_geometry_from_georeference_feature(feature: dict) -> dict:
    ring = feature.get("wgs84_coordinates") or []
    return {"type": "Polygon", "coordinates": [ring]}


def _layer_geometry_from_georeference_feature(feature: dict) -> dict:
    """A category-tagged (road/drainage/open_space/infrastructure) digitized feature keeps
    whatever shape it was actually drawn as - unlike a plot, which is always a polygon."""
    feature_type = str(feature.get("feature_type") or "")
    coordinates = feature.get("wgs84_coordinates") or []
    if feature_type == "polygon":
        return _polygon_geometry_from_georeference_feature(feature)
    if feature_type == "point":
        return {"type": "Point", "coordinates": coordinates[0] if coordinates else []}
    return {"type": "LineString", "coordinates": coordinates}


def _load_georeference_session_features(db: Session, session_id: str) -> list[dict]:
    """Reads straight from survey_georeference_sessions - the session carries only raster/pixel/
    geometry data (no estate, customer or payment data), so a cross-domain read here is safe; it
    mirrors the read-only cross-reference the Survey side already makes into estate_plots via
    estate_survey_requests.survey_working_plot_id."""
    row = db.execute(text("SELECT features_json FROM survey_georeference_sessions WHERE id = :id"), {"id": session_id}).mappings().first()
    if not row:
        raise HTTPException(404, "The linked georeference session no longer exists")
    features = row["features_json"] or []
    if isinstance(features, str):
        features = json.loads(features)
    return features


@router.post("/{estate_id}/import-reviews/from-georeference-session")
def create_import_review_from_georeference_session(estate_id: int, payload: ImportReviewFromGeoreferenceSession, request: Request, db: Session = Depends(get_db)):
    """Starts a review from a raster the surveyor already georeferenced in the Survey product's
    own tool (POST /survey-georeference/sessions, unauthenticated by session id like a plot draft)
    - no re-upload here, this just remembers which session belongs to which Estate."""
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    session_id = payload.survey_georeference_session_id.strip()
    exists = db.execute(text("SELECT 1 FROM survey_georeference_sessions WHERE id = :id"), {"id": session_id}).scalar()
    if not exists:
        raise HTTPException(404, "Georeference session not found")
    if db.query(EstateImportReview).filter(EstateImportReview.survey_georeference_session_id == session_id).first():
        raise HTTPException(409, "This georeference session is already linked to an import review")
    review = EstateImportReview(
        organization_id=estate.organization_id,
        estate_id=estate.id,
        source_type="raster",
        survey_georeference_session_id=session_id,
        notes=payload.notes or "Layout is being georeferenced and digitized before its plots are reviewed.",
        candidate_data=[],
        created_by_subject_type=access.principal.subject_type,
        created_by_subject_id=access.principal.subject_id,
    )
    db.add(review)
    db.flush()
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="import_review.georeference_started", entity_type="estate_import_review", entity_id=review.id, after_data={"session_id": session_id})
    db.commit()
    return {"id": review.id, "status": review.status, "survey_georeference_session_id": review.survey_georeference_session_id}


@router.post("/import-reviews/{review_id}/georeference-session")
def link_import_review_georeference_session(review_id: int, payload: GeoreferenceSessionLink, request: Request, db: Session = Depends(get_db)):
    """Attaches (or replaces) the georeference session behind an already-created review - for the
    case where the scan was uploaded through the older file-upload path first and georeferenced
    afterward."""
    row = db.get(EstateImportReview, review_id)
    if not row:
        raise HTTPException(404, "Import review not found")
    access = require_estate_access(db, request, row.organization_id, permission="plot.manage")
    if row.status != "review_required":
        raise HTTPException(409, "Import review has already been decided")
    session_id = payload.survey_georeference_session_id.strip()
    exists = db.execute(text("SELECT 1 FROM survey_georeference_sessions WHERE id = :id"), {"id": session_id}).scalar()
    if not exists:
        raise HTTPException(404, "Georeference session not found")
    conflict = db.query(EstateImportReview).filter(EstateImportReview.survey_georeference_session_id == session_id, EstateImportReview.id != review_id).first()
    if conflict:
        raise HTTPException(409, "This georeference session is already linked to another import review")
    row.survey_georeference_session_id = session_id
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="import_review.georeference_linked", entity_type="estate_import_review", entity_id=row.id, after_data={"session_id": session_id})
    db.commit()
    return {"id": row.id, "survey_georeference_session_id": row.survey_georeference_session_id}


@router.post("/import-reviews/{review_id}/import-from-georeference")
def import_plots_from_georeference(review_id: int, payload: ImportFromGeoreference, request: Request, db: Session = Depends(get_db)):
    """Approves a georeference-linked review: pulls the polygons the surveyor digitized and
    solved in the Survey georeference tool (already in WGS84 - see _feature_to_saved_payload in
    survey_georeference.py) straight into the Estate's operational plot register, the same way
    approving a CSV/GIS review does."""
    row = db.get(EstateImportReview, review_id)
    if not row:
        raise HTTPException(404, "Import review not found")
    access = require_estate_access(db, request, row.organization_id, permission="survey.manage")
    if row.status != "review_required":
        raise HTTPException(409, "Import review has already been decided")
    session_id = str(row.survey_georeference_session_id or "").strip()
    if not session_id:
        raise HTTPException(409, "Link a georeference session to this review first")
    features = _load_georeference_session_features(db, session_id)
    # A feature becomes a Plot only when it's a polygon with no assigned category - a category
    # (road/drainage/open_space/infrastructure) marks it as an Estate layout feature instead,
    # regardless of shape (see DigitizedFeatureInput.category in survey_georeference.py).
    polygons = [feature for feature in features if str(feature.get("feature_type") or "") == "polygon" and not feature.get("category")]
    layer_features = [feature for feature in features if feature.get("category")]
    if not polygons and not layer_features:
        raise HTTPException(422, "No digitized polygons or layout features were saved in this georeference session yet")
    prefix = (payload.plot_prefix or "P").strip() or "P"
    candidates = []
    for index, feature in enumerate(polygons, 1):
        geometry = _polygon_geometry_from_georeference_feature(feature)
        plot_number = _plot_number_from_georeference_feature(feature, index, prefix)
        candidates.append(_import_candidate(row_number=index, plot_number=plot_number, geometry=geometry))
    row.candidate_data = candidates
    estate = db.get(Estate, row.estate_id)
    created = _create_plots_from_candidates(db, estate=estate, candidates=candidates, source_type=row.source_type, source_reference=str(row.id), actor=access.principal) if candidates else 0
    created_features = 0
    for feature in layer_features:
        try:
            geometry = _geojson_geometry(_layer_geometry_from_georeference_feature(feature), allow_polygon=True)
        except HTTPException:
            continue
        db.add(EstateSpatialFeature(
            organization_id=row.organization_id,
            estate_id=row.estate_id,
            feature_type=str(feature.get("category")),
            name=str(feature.get("label") or "").strip() or None,
            geometry=from_shape(geometry, srid=4326),
            created_by_subject_type=access.principal.subject_type,
            created_by_subject_id=access.principal.subject_id,
        ))
        created_features += 1
    row.notes = (row.notes or "") + f"; {created} operational plot(s) and {created_features} layout feature(s) created from the georeferenced layout"
    row.status = "approved"
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="import_review.approved", entity_type="estate_import_review", entity_id=row.id, after_data={"status": row.status, "source": "georeference", "created_plots": created, "created_features": created_features})
    db.commit()
    return {"id": row.id, "status": row.status, "created_plots": created, "created_features": created_features}


@router.post("/{estate_id}/layout-proposals")
def generate_layout_proposal(
    estate_id: int,
    payload: EstateLayoutCriteria,
    request: Request,
    db: Session = Depends(get_db),
):
    """Generate a reviewable concept layout from the Estate boundary."""
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    _enabled(db, estate.organization_id)
    if not estate.boundary:
        raise HTTPException(409, "Add a valid Estate boundary before creating a layout")
    try:
        generated = generate_estate_layout(to_shape(estate.boundary), payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    generated["diagnostics"] = _refresh_layout_diagnostics(
        estate,
        generated.get("diagnostics"),
        generated.get("plot_candidates") or [],
        generated.get("feature_candidates") or [],
    )
    row = EstateLayoutProposal(
        organization_id=estate.organization_id,
        estate_id=estate.id,
        criteria=generated["criteria"],
        diagnostics=generated["diagnostics"],
        plot_candidates=generated["plot_candidates"],
        feature_candidates=generated["feature_candidates"],
        created_by_subject_type=access.principal.subject_type,
        created_by_subject_id=access.principal.subject_id,
    )
    db.add(row)
    db.flush()
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="layout_proposal.created",
        entity_type="estate_layout_proposal",
        entity_id=row.id,
        after_data={"proposal_uid": row.proposal_uid, "plot_count": len(row.plot_candidates), "feature_count": len(row.feature_candidates)},
    )
    db.commit()
    return _layout_proposal_payload(row)


@router.get("/{estate_id}/layout-proposals")
def list_layout_proposals(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="plot.read")
    return [_layout_proposal_payload(row) for row in db.query(EstateLayoutProposal).filter(EstateLayoutProposal.estate_id == estate_id).order_by(EstateLayoutProposal.created_at.desc()).all()]


@router.patch("/layout-proposals/{proposal_id}")
def edit_layout_proposal(proposal_id: int, payload: EstateLayoutProposalEdit, request: Request, db: Session = Depends(get_db)):
    """Lets a reviewer nudge vertices, delete a candidate plot, or edit a road/open-space shape on
    a draft layout - before it is approved into real plots and spatial features."""
    row = db.get(EstateLayoutProposal, proposal_id)
    if not row:
        raise HTTPException(404, "Layout proposal not found")
    access = _require_layout_approval_access(db, request, row.organization_id)
    if row.status != "review_required":
        raise HTTPException(409, "This layout has already been decided and can no longer be edited")
    if payload.plot_candidates is not None:
        cleaned_plots: list[dict] = []
        for index, candidate in enumerate(payload.plot_candidates, 1):
            geometry = candidate.get("geometry") or {}
            area, issues = validate_polygon(geometry)
            if any(issue.severity == "error" for issue in issues):
                raise HTTPException(422, f"Plot {candidate.get('plot_number') or index}: {issues[0].message if issues else 'invalid geometry'}")
            cleaned_plots.append({
                "plot_number": str(candidate.get("plot_number") or f"P-{index:03d}").strip(),
                "block_label": candidate.get("block_label"),
                "geometry": geometry,
                "area_sqm": round(float(area), 2),
                "valid": True,
                "issues": [],
            })
        row.plot_candidates = cleaned_plots
        diagnostics = dict(row.diagnostics or {})
        diagnostics["estimated_plot_count"] = len(cleaned_plots)
        diagnostics["total_plot_area_sqm"] = round(sum(float(c["area_sqm"]) for c in cleaned_plots), 2)
        row.diagnostics = diagnostics
    if payload.feature_candidates is not None:
        cleaned_features: list[dict] = []
        for candidate in payload.feature_candidates:
            geometry = candidate.get("geometry") or {}
            _geojson_geometry(geometry)
            cleaned_feature = {
                "feature_type": str(candidate.get("feature_type") or "infrastructure"),
                "name": str(candidate.get("name") or "Generated layout feature"),
                "geometry": geometry,
            }
            if candidate.get("width_m"):
                cleaned_feature["width_m"] = float(candidate["width_m"])
            if candidate.get("carved_plots"):
                cleaned_feature["carved_plots"] = candidate["carved_plots"]
            cleaned_features.append(cleaned_feature)
        row.feature_candidates = cleaned_features
    estate = db.get(Estate, row.estate_id)
    if estate:
        row.diagnostics = _refresh_layout_diagnostics(estate, row.diagnostics, row.plot_candidates or [], row.feature_candidates or [])
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="layout_proposal.edited", entity_type="estate_layout_proposal", entity_id=row.id, after_data={"plot_count": len(row.plot_candidates or []), "feature_count": len(row.feature_candidates or [])})
    db.commit()
    return _layout_proposal_payload(row)


def _largest_polygon_piece(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type == "Polygon":
        return geometry
    if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        pieces = [item for item in getattr(geometry, "geoms", []) if item.geom_type == "Polygon"]
        return max(pieces, key=lambda item: item.area, default=None)
    return None


@router.post("/layout-proposals/{proposal_id}/features")
def add_layout_proposal_feature(proposal_id: int, payload: EstateLayoutFeatureAdd, request: Request, db: Session = Depends(get_db)):
    """Adds a hand-drawn road or open-space shape to a draft layout, carving its footprint out of
    every plot candidate it overlaps (keeping only the larger remaining piece of a split plot, and
    dropping a plot outright if what's left is too small to be usable) so the draft reflects the
    new infrastructure instead of showing plots that overlap it."""
    row = db.get(EstateLayoutProposal, proposal_id)
    if not row:
        raise HTTPException(404, "Layout proposal not found")
    access = _require_layout_approval_access(db, request, row.organization_id)
    if row.status != "review_required":
        raise HTTPException(409, "This layout has already been decided and can no longer be edited")

    try:
        drawn = shape(payload.geometry)
    except Exception as exc:
        raise HTTPException(422, "Geometry is invalid GeoJSON") from exc
    if payload.feature_type == "road":
        if drawn.geom_type not in {"LineString", "MultiLineString"}:
            raise HTTPException(422, "A road must be drawn as a line")
        width_m = float(payload.width_m or 9.0)
    else:
        if drawn.geom_type not in {"Polygon", "MultiPolygon"}:
            raise HTTPException(422, "Open space must be drawn as a closed shape")
        width_m = None

    metric_epsg = _metric_epsg_for_wgs84_polygon(drawn)
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{metric_epsg}", always_xy=True).transform
    backward = Transformer.from_crs(f"EPSG:{metric_epsg}", "EPSG:4326", always_xy=True).transform
    drawn_metric = shapely_transform(forward, drawn)
    footprint_metric = drawn_metric.buffer(width_m / 2, cap_style=2) if width_m else drawn_metric
    if footprint_metric.is_empty:
        raise HTTPException(422, "This shape has no usable area")

    source_candidates = payload.plot_candidates if payload.plot_candidates is not None else (row.plot_candidates or [])
    target_area = float((row.criteria or {}).get("target_plot_area_sqm") or 100)
    min_area_sqm = max(target_area * 0.35, 25)
    updated_candidates: list[dict] = []
    # Every plot this feature actually touches gets snapshotted here in its pre-carve form, so that
    # deleting this road/open space later can restore exactly what it took - a plot it only
    # trimmed goes back to its original shape, and a plot it consumed entirely comes back too.
    # This is a per-feature undo record, not a general recompute: if a second feature later also
    # carves the same plot, deleting the first one restores this snapshot regardless of what the
    # second one did, so overlapping carves on the same plot don't compose perfectly - flagged
    # rather than silently assumed away.
    carved_plots: list[dict] = []
    removed = 0
    for candidate in source_candidates:
        try:
            plot_metric = shapely_transform(forward, shape(candidate.get("geometry") or {}))
        except Exception:
            updated_candidates.append(candidate)
            continue
        if not plot_metric.intersects(footprint_metric):
            updated_candidates.append(candidate)
            continue
        carved_plots.append(dict(candidate))
        remainder = _largest_polygon_piece(plot_metric.difference(footprint_metric))
        if remainder is None or remainder.is_empty or remainder.area < min_area_sqm:
            removed += 1
            continue
        remainder_wgs84 = shapely_transform(backward, remainder)
        area_sqm, issues = validate_polygon(mapping(remainder_wgs84))
        if any(issue.severity == "error" for issue in issues):
            removed += 1
            continue
        updated_candidates.append({**candidate, "geometry": mapping(remainder_wgs84), "area_sqm": round(float(area_sqm), 2), "valid": True, "issues": []})

    footprint_wgs84 = shapely_transform(backward, footprint_metric)
    existing_count = sum(1 for feature in (row.feature_candidates or []) if feature.get("feature_type") == payload.feature_type)
    default_name = f"Road {existing_count + 1}" if payload.feature_type == "road" else (f"Open space {existing_count + 1}" if existing_count else "Open space")
    new_feature = {"feature_type": payload.feature_type, "name": (payload.name or "").strip() or default_name, "geometry": mapping(footprint_wgs84), "carved_plots": carved_plots}
    if width_m:
        new_feature["width_m"] = width_m

    row.plot_candidates = updated_candidates
    row.feature_candidates = [*(row.feature_candidates or []), new_feature]
    diagnostics = dict(row.diagnostics or {})
    diagnostics["estimated_plot_count"] = len(updated_candidates)
    diagnostics["total_plot_area_sqm"] = round(sum(float(c["area_sqm"]) for c in updated_candidates), 2)
    estate = db.get(Estate, row.estate_id)
    row.diagnostics = _refresh_layout_diagnostics(estate, diagnostics, updated_candidates, row.feature_candidates or []) if estate else diagnostics

    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="layout_proposal.feature_added", entity_type="estate_layout_proposal", entity_id=row.id, after_data={"feature_type": payload.feature_type, "name": new_feature["name"], "plots_removed": removed, "plots_remaining": len(updated_candidates)})
    db.commit()
    return _layout_proposal_payload(row)


@router.post("/layout-proposals/{proposal_id}/remove-feature")
def remove_layout_proposal_feature(proposal_id: int, payload: EstateLayoutFeatureRemove, request: Request, db: Session = Depends(get_db)):
    """Remove a layout feature while keeping released road land visible for later subdivision.

    Open-space features retain their original restore behavior. Roads are different: deleting a
    road releases its corridor, so the current plot geometry is kept and the diagnostics layer
    exposes that corridor as unallocated land instead of silently absorbing it into neighboring
    plots.
    """
    row = db.get(EstateLayoutProposal, proposal_id)
    if not row:
        raise HTTPException(404, "Layout proposal not found")
    access = _require_layout_approval_access(db, request, row.organization_id)
    if row.status != "review_required":
        raise HTTPException(409, "This layout has already been decided and can no longer be edited")

    features = payload.feature_candidates if payload.feature_candidates is not None else (row.feature_candidates or [])
    if payload.feature_index >= len(features):
        raise HTTPException(404, "Feature not found on this layout")
    feature = features[payload.feature_index]

    source_candidates = payload.plot_candidates if payload.plot_candidates is not None else (row.plot_candidates or [])
    candidates_by_number = {str(candidate.get("plot_number")): dict(candidate) for candidate in source_candidates}
    remaining_features = [item for index, item in enumerate(features) if index != payload.feature_index]

    carved_plots = feature.get("carved_plots") or []
    road_gap_compacted = False
    if carved_plots:
        # This feature carved these plots when it was added - hand each one back exactly as it
        # was, regardless of what shape it holds right now.
        for entry in carved_plots:
            candidates_by_number[str(entry.get("plot_number"))] = dict(entry)
    elif feature.get("feature_type") == "road":
        compacted = _compact_generated_road_gap(estate=db.get(Estate, row.estate_id), plot_candidates=list(candidates_by_number.values()), deleted_feature=feature, remaining_features=remaining_features)
        if compacted:
            compacted_candidates, remaining_features = compacted
            candidates_by_number = {str(candidate.get("plot_number")): candidate for candidate in compacted_candidates}
            road_gap_compacted = True

    row.plot_candidates = list(candidates_by_number.values())
    row.feature_candidates = remaining_features
    diagnostics = dict(row.diagnostics or {})
    diagnostics["estimated_plot_count"] = len(row.plot_candidates)
    diagnostics["total_plot_area_sqm"] = round(sum(float(candidate.get("area_sqm") or 0) for candidate in row.plot_candidates), 2)
    diagnostics["road_gap_compacted"] = road_gap_compacted
    estate = db.get(Estate, row.estate_id)
    row.diagnostics = _refresh_layout_diagnostics(estate, diagnostics, row.plot_candidates, row.feature_candidates) if estate else diagnostics

    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="layout_proposal.feature_removed", entity_type="estate_layout_proposal", entity_id=row.id, after_data={"feature_type": feature.get("feature_type"), "name": feature.get("name"), "plots_remaining": len(row.plot_candidates)})
    db.commit()
    return _layout_proposal_payload(row)


@router.post("/layout-proposals/{proposal_id}/decision")
def decide_layout_proposal(
    proposal_id: int,
    payload: EstateLayoutDecision,
    request: Request,
    db: Session = Depends(get_db),
):
    """Approve generated candidates into the Estate register, or reject the proposal."""
    row = db.get(EstateLayoutProposal, proposal_id)
    if not row:
        raise HTTPException(404, "Layout proposal not found")
    access = _require_layout_approval_access(db, request, row.organization_id)
    _enabled(db, row.organization_id)
    if row.status != "review_required":
        raise HTTPException(409, "Layout proposal has already been decided")
    if payload.status == "rejected":
        row.status = "rejected"
        row.reviewed_by_subject_type = access.principal.subject_type
        row.reviewed_by_subject_id = access.principal.subject_id
        row.reviewed_at = datetime.now(timezone.utc)
        append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="layout_proposal.rejected", entity_type="estate_layout_proposal", entity_id=row.id, after_data={"status": row.status, "notes": payload.notes})
        db.commit()
        return {"id": row.id, "status": row.status, "created_plots": 0, "created_features": 0}

    estate = db.get(Estate, row.estate_id)
    candidates = row.plot_candidates or []
    if not candidates:
        raise HTTPException(409, "This layout contains no usable plot candidates")
    candidate_numbers = [_normalized(str(candidate.get("plot_number") or "")) for candidate in candidates]
    if any(not value for value in candidate_numbers) or len(candidate_numbers) != len(set(candidate_numbers)):
        raise HTTPException(409, "The generated layout contains duplicate or blank plot numbers")

    replaced_plot_count = 0
    replaced_feature_count = 0
    if payload.replace_existing:
        # The frontend only sets this after the user has explicitly confirmed replacing their
        # previously approved layout - still re-checked here rather than trusted blindly, since a
        # plot already reserved/allocated to a real customer must never be silently deleted.
        allocated_count = (
            db.query(EstateAllocation)
            .join(EstatePlot, EstateAllocation.plot_id == EstatePlot.id)
            .filter(EstatePlot.estate_id == row.estate_id, EstateAllocation.status.in_(("reserved", "allocated")))
            .count()
        )
        if allocated_count:
            raise HTTPException(409, f"{allocated_count} plot(s) in this Estate already have a customer reservation or allocation - remove those first before replacing the layout.")
        existing_plots = db.query(EstatePlot).filter(EstatePlot.estate_id == row.estate_id).all()
        replaced_plot_count = len(existing_plots)
        for plot in existing_plots:
            db.delete(plot)
        existing_features = db.query(EstateSpatialFeature).filter(EstateSpatialFeature.estate_id == row.estate_id).all()
        replaced_feature_count = len(existing_features)
        for feature in existing_features:
            db.delete(feature)
        db.flush()

    existing_numbers = {value for (value,) in db.query(EstatePlot.plot_number_normalized).filter(EstatePlot.estate_id == row.estate_id).all()}
    if existing_numbers.intersection(candidate_numbers):
        raise HTTPException(409, "Some generated plot numbers already exist. Reject this proposal or use a different prefix, or replace the existing layout.")

    block_ids: dict[str, int] = {}
    for candidate in candidates:
        label = str(candidate.get("block_label") or "").strip()
        if not label or label in block_ids:
            continue
        block = db.query(EstateBlock).filter(EstateBlock.estate_id == row.estate_id, EstateBlock.label == label).one_or_none()
        if block is None:
            block = EstateBlock(estate_id=row.estate_id, label=label, name=f"Block {label}")
            db.add(block)
            db.flush()
        block_ids[label] = block.id

    created_plots: list[EstatePlot] = []
    for candidate in candidates:
        geometry = candidate.get("geometry") or {}
        area, issues = validate_polygon(geometry)
        if any(issue.severity == "error" for issue in issues):
            raise HTTPException(409, "The generated layout contains an invalid plot candidate")
        label = str(candidate.get("block_label") or "").strip()
        plot = EstatePlot(
            estate_id=row.estate_id,
            block_id=block_ids.get(label),
            plot_number=str(candidate["plot_number"]).strip(),
            plot_number_normalized=_normalized(str(candidate["plot_number"])),
            geometry=from_shape(shape(geometry), srid=4326),
            area_sqm=area,
            land_use="residential",
            commercial_status="available",
            development_status="not_started",
            geometry_status="approved",
            source_type="generated",
            source_reference=row.proposal_uid,
            created_by_subject_type=access.principal.subject_type,
            created_by_subject_id=access.principal.subject_id,
        )
        db.add(plot)
        created_plots.append(plot)

    created_features: list[EstateSpatialFeature] = []
    for candidate in row.feature_candidates or []:
        geometry = _geojson_geometry(candidate.get("geometry") or {})
        feature = EstateSpatialFeature(
            organization_id=row.organization_id,
            estate_id=row.estate_id,
            feature_type=str(candidate.get("feature_type") or "infrastructure"),
            name=str(candidate.get("name") or "Generated layout feature"),
            geometry=from_shape(geometry, srid=4326),
            created_by_subject_type=access.principal.subject_type,
            created_by_subject_id=access.principal.subject_id,
        )
        db.add(feature)
        created_features.append(feature)

    row.status = "approved"
    row.reviewed_by_subject_type = access.principal.subject_type
    row.reviewed_by_subject_id = access.principal.subject_id
    row.reviewed_at = datetime.now(timezone.utc)
    db.flush()
    append_estate_audit_event(
        db,
        organization_id=row.organization_id,
        actor=access.principal,
        action="layout_proposal.approved",
        entity_type="estate_layout_proposal",
        entity_id=row.id,
        after_data={"status": row.status, "created_plot_ids": [plot.id for plot in created_plots], "created_feature_ids": [feature.id for feature in created_features], "replaced_plot_count": replaced_plot_count, "replaced_feature_count": replaced_feature_count},
    )
    db.commit()
    return {"id": row.id, "status": row.status, "created_plots": len(created_plots), "created_features": len(created_features), "replaced_plots": replaced_plot_count, "replaced_features": replaced_feature_count, "estate_id": estate.id}

_LAYER_TYPE_LABELS = {"road": "Road", "drainage": "Drainage", "open_space": "Open space", "infrastructure": "Infrastructure"}


@router.post("/{estate_id}/layers")
def create_estate_layer(estate_id:int,payload:SpatialFeatureCreate,request:Request,db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    access=require_estate_access(db,request,estate.organization_id,permission="infrastructure.manage")
    try: geometry=from_shape(shape(payload.geometry),srid=4326)
    except Exception: raise HTTPException(422,"Layer geometry is invalid")
    name=(payload.name or "").strip()
    if not name:
        # "Layer name" is an optional field in the UI - a manually added road left unnamed still
        # needs a real name so it actually shows a label on the map, not just get displayed as an
        # unmarked shape (the older behavior here, storing NULL, is what let that happen).
        existing_count = db.query(EstateSpatialFeature).filter(EstateSpatialFeature.estate_id == estate.id, EstateSpatialFeature.feature_type == payload.feature_type).count()
        name = f"{_LAYER_TYPE_LABELS.get(payload.feature_type, payload.feature_type.title())} {existing_count + 1}"
    row=EstateSpatialFeature(organization_id=estate.organization_id,estate_id=estate.id,feature_type=payload.feature_type,name=name,geometry=geometry,created_by_subject_type=access.principal.subject_type,created_by_subject_id=access.principal.subject_id)
    db.add(row); db.flush(); append_estate_audit_event(db,organization_id=estate.organization_id,actor=access.principal,action="spatial_layer.created",entity_type="estate_spatial_feature",entity_id=row.id,after_data={"type":row.feature_type,"name":row.name}); db.commit()
    return {"id":row.id,"type":row.feature_type,"name":row.name}


@router.patch("/{estate_id}/layers/{feature_id}")
def update_estate_layer(estate_id: int, feature_id: int, payload: SpatialFeatureUpdate, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    row = db.get(EstateSpatialFeature, feature_id)
    if not estate or not row or row.estate_id != estate_id:
        raise HTTPException(404, "Spatial layer was not found")
    access = require_estate_access(db, request, estate.organization_id, permission="infrastructure.manage")
    before = {"feature_type": row.feature_type, "name": row.name, "status": row.status}
    values = payload.model_dump(exclude_unset=True)
    if "feature_type" in values:
        row.feature_type = values["feature_type"]
    if "name" in values:
        row.name = (values["name"] or "").strip() or None
    if "status" in values:
        row.status = values["status"]
    if "geometry" in values and values["geometry"] is not None:
        row.geometry = from_shape(_geojson_geometry(values["geometry"]), srid=4326)
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="spatial_layer.updated", entity_type="estate_spatial_feature", entity_id=row.id, before_data=before, after_data={"feature_type": row.feature_type, "name": row.name, "status": row.status})
    db.commit()
    return {"id": row.id, "type": row.feature_type, "name": row.name, "status": row.status}


@router.delete("/{estate_id}/layers/{feature_id}")
def archive_estate_layer(estate_id: int, feature_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    row = db.get(EstateSpatialFeature, feature_id)
    if not estate or not row or row.estate_id != estate_id:
        raise HTTPException(404, "Spatial layer was not found")
    access = require_estate_access(db, request, estate.organization_id, permission="infrastructure.manage")
    if row.status != "archived":
        row.status = "archived"
        append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="spatial_layer.archived", entity_type="estate_spatial_feature", entity_id=row.id)
        db.commit()
    return {"id": row.id, "status": row.status}

@router.get("/{estate_id}/quality-check")
def estate_quality_check(estate_id:int, request:Request, db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    require_estate_access(db,request,estate.organization_id,permission="plot.read")
    plots=db.query(EstatePlot).filter(EstatePlot.estate_id==estate_id).all(); issues=[]
    seen={}
    plot_by_id = {}
    geometry_by_id = {}
    for plot in plots:
        plot_by_id[plot.id] = plot
        seen.setdefault(plot.plot_number_normalized,[]).append(plot.id)
        if not plot.geometry: issues.append({"severity":"error","plot_id":plot.id,"code":"missing_geometry","message":"Plot has no geometry"}); continue
        geometry = to_shape(plot.geometry)
        geometry_by_id[plot.id] = geometry
        _, qc=validate_polygon(mapping(geometry))
        issues.extend({"severity":item.severity,"plot_id":plot.id,"code":item.code,"message":item.message} for item in qc)
    for number, ids in seen.items():
        if len(ids)>1: issues.append({"severity":"error","plot_id":ids[0],"code":"duplicate_plot_number","message":f"Duplicate plot number {number}"})
    # Use the spatial index to find possible overlaps before doing exact Shapely checks.
    other_plot = aliased(EstatePlot)
    candidate_pairs = db.query(EstatePlot.id, other_plot.id).join(
        other_plot,
        and_(
            other_plot.estate_id == EstatePlot.estate_id,
            other_plot.id > EstatePlot.id,
            other_plot.geometry.isnot(None),
            func.ST_Intersects(EstatePlot.geometry, other_plot.geometry),
        ),
    ).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry.isnot(None)).all()
    for plot_id, other_id in candidate_pairs:
        geometry = geometry_by_id.get(plot_id)
        other_geometry = geometry_by_id.get(other_id)
        if geometry is None or other_geometry is None:
            continue
        try:
            overlap_area = geometry.intersection(other_geometry).area
        except Exception:
            continue
        if overlap_area > 1e-12:
            other = plot_by_id[other_id]
            issues.append({"severity":"error","plot_id":plot_id,"related_plot_id":other_id,"code":"overlap","message":f"Overlaps plot {other.plot_number}"})
    return {"estate_id":estate_id,"plot_count":len(plots),"issues":issues,"review_required":any(item["severity"]=="error" for item in issues)}

def _calculate_plot_hazards(geometry, db: Session) -> dict:
    from app.routers.hazards import erosion_preview, flood_preview
    payload = {"boundary": mapping(to_shape(geometry)), "show_raster": False}
    return {"flood": flood_preview(payload, db), "erosion": erosion_preview(payload, db)}


def _hazard_row_payload(row: EstateHazardAssessment) -> dict:
    return {"id": row.id, "plot_id": row.plot_id, "estate_id": row.estate_id, "hazard_type": row.hazard_type, "status": row.status, "risk_class": row.risk_class, "risk_score": float(row.risk_score) if row.risk_score is not None else None, "assessed_at": row.assessed_at, "result": row.result_payload}


def _persist_hazard_results(db: Session, *, estate: Estate, plot_id: int | None, results: dict, access) -> list[EstateHazardAssessment]:
    rows = []
    for hazard_type, result in results.items():
        if hazard_type == "flood":
            risk_class = str((result.get("summary") or {}).get("floodplain_class") or "unavailable")
            raw_score = (result.get("floodplain") or {}).get("risk_score")
        else:
            risk_class = str(result.get("risk_class") or "unavailable")
            raw_score = result.get("risk_score")
        try:
            score = float(raw_score) if raw_score is not None else None
        except (TypeError, ValueError):
            score = None
        row = EstateHazardAssessment(
            organization_id=estate.organization_id,
            estate_id=estate.id,
            plot_id=plot_id,
            hazard_type=hazard_type,
            risk_class=risk_class,
            risk_score=score,
            result_payload=result,
            assessed_by_subject_type=access.principal.subject_type,
            assessed_by_subject_id=access.principal.subject_id,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="hazard.assessment_completed", entity_type="estate_plot" if plot_id else "estate", entity_id=plot_id or estate.id, after_data={"hazard_types": list(results), "assessment_ids": [row.id for row in rows]})
    return rows


def _require_plus_plan(db: Session, organization_id: int, feature_name: str) -> None:
    """Keep Plus-only geospatial features behind one server-side entitlement check."""
    if not has_hazard_access(get_subscription(db, organization_id)):
        raise HTTPException(
            status_code=402,
            detail={
                "code": "upgrade_required",
                "message": f"{feature_name} is available on the Plus plan. Upgrade to unlock it.",
            },
        )


def _require_hazard_plan(db: Session, organization_id: int) -> None:
    """Flood/erosion hazard analysis is a Plus-plan feature."""
    _require_plus_plan(db, organization_id, "Hazard analysis (flood and erosion)")


@router.get("/plots/{plot_id}/hazards")
def plot_hazards(plot_id: int, request: Request, db: Session = Depends(get_db)):
    """Return the latest stored result, with a read-only calculation for legacy records."""
    plot = db.get(EstatePlot, plot_id)
    if not plot:
        raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, plot.estate_id)
    require_estate_access(db, request, estate.organization_id, permission="plot.read")
    _require_hazard_plan(db, estate.organization_id)
    if plot.geometry_status != "approved":
        raise HTTPException(409, "Only approved plot geometry can be screened")
    latest = {}
    for row in db.query(EstateHazardAssessment).filter(EstateHazardAssessment.plot_id == plot.id).order_by(EstateHazardAssessment.assessed_at.desc()).all():
        latest.setdefault(row.hazard_type, row)
    if {"flood", "erosion"}.issubset(latest):
        return {"plot_id": plot.id, "persisted": True, "flood": latest["flood"].result_payload, "erosion": latest["erosion"].result_payload}
    return {"plot_id": plot.id, "persisted": False, **_calculate_plot_hazards(plot.geometry, db)}


@router.post("/plots/{plot_id}/hazards/assess")
def assess_plot_hazards(plot_id: int, request: Request, db: Session = Depends(get_db)):
    plot = db.get(EstatePlot, plot_id)
    if not plot:
        raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, plot.estate_id)
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    _require_hazard_plan(db, estate.organization_id)
    if plot.geometry_status != "approved":
        raise HTTPException(409, "Only approved plot geometry can be screened")
    results = _calculate_plot_hazards(plot.geometry, db)
    rows = _persist_hazard_results(db, estate=estate, plot_id=plot.id, results=results, access=access)
    db.commit()
    return {"plot_id": plot.id, "persisted": True, "assessments": [_hazard_row_payload(row) for row in rows], **results}


def _run_estate_hazard_assessment_job(job_id: str) -> None:
    """Background worker for the whole-layout "assess-all" button. Flood/erosion screening makes
    slow external calls per plot (see the ASYNC JOBS note in hazards.py, which is why single-plot
    screening already runs this way) - running that in a loop synchronously inside the HTTP
    request, as this endpoint originally did, ties up one request thread and its DB connection for
    the whole estate's runtime. Under concurrent use (or a client retrying a slow request) that was
    enough to exhaust the connection pool for the entire API. Moving the loop into its own
    short-lived daemon thread with its own session fixes that: the HTTP endpoint now only enqueues
    the job and returns immediately."""
    db = SessionLocal()
    try:
        job = get_hazard_job(db, job_id)
        if not job:
            return
        payload = job.get("request_payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        estate_id = int(payload["estate_id"])
        access_principal = SimpleNamespace(subject_type=payload.get("subject_type"), subject_id=payload.get("subject_id"))
        access = SimpleNamespace(principal=access_principal)

        set_hazard_job_status(db, job_id, status="running", stage="Starting analysis...", progress_pct=1, started=True)
        estate = db.get(Estate, estate_id)
        if not estate:
            set_hazard_job_status(db, job_id, status="failed", stage="Failed", error_text="Estate not found", completed=True)
            return
        plots = db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry_status == "approved").all()
        plots = [plot for plot in plots if plot.geometry]
        if not plots:
            set_hazard_job_status(db, job_id, status="failed", stage="Failed", error_text="This Estate has no approved plot geometry to screen yet", completed=True)
            return

        for index, plot in enumerate(plots):
            set_hazard_job_status(db, job_id, status="running", stage=f"Screening plot {index + 1} of {len(plots)}...", progress_pct=int(5 + 90 * index / len(plots)))
            results = _calculate_plot_hazards(plot.geometry, db)
            _persist_hazard_results(db, estate=estate, plot_id=plot.id, results=results, access=access)
            db.commit()

        append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="hazard.estate_assessment_completed", entity_type="estate", entity_id=estate.id, after_data={"plots_screened": len(plots)})
        db.commit()
        result_payload = _estate_hazard_dashboard_payload(db, estate)
        set_hazard_job_status(db, job_id, status="completed", stage="Complete", progress_pct=100, result_payload=result_payload, completed=True)
    except Exception as exc:
        db.rollback()
        set_hazard_job_status(db, job_id, status="failed", stage="Failed", error_text=str(exc), completed=True)
    finally:
        db.close()


@router.post("/{estate_id}/hazards/assess-all")
def assess_estate_hazards(estate_id: int, request: Request, db: Session = Depends(get_db)):
    """Kicks off flood + erosion screening for every approved plot in this Estate as a background
    job and returns immediately - the whole-layout equivalent of running it one plot at a time from
    each plot's drawer, without blocking a request thread/DB connection for the whole estate's
    runtime. Poll GET /hazards/jobs/{job_id} for progress; its result payload matches GET
    .../hazards so the caller can apply it directly once complete."""
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    _require_hazard_plan(db, estate.organization_id)
    has_screenable_plot = db.query(EstatePlot.id).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry_status == "approved").first()
    if not has_screenable_plot:
        raise HTTPException(422, "This Estate has no approved plot geometry to screen yet")
    job = insert_hazard_job(
        db,
        hazard_type="estate_all",
        output_type="preview",
        request_payload={"estate_id": estate_id, "subject_type": access.principal.subject_type, "subject_id": access.principal.subject_id},
        worker=_run_estate_hazard_assessment_job,
    )
    return serialize_hazard_job(job)


def _estate_hazard_dashboard_payload(db: Session, estate: Estate) -> dict:
    latest = {}
    rows = db.query(EstateHazardAssessment).filter(EstateHazardAssessment.estate_id == estate.id).order_by(EstateHazardAssessment.assessed_at.desc()).all()
    for row in rows:
        latest.setdefault((row.plot_id, row.hazard_type), row)
    plot_results = {}
    for (plot_id, hazard_type), row in latest.items():
        plot_results.setdefault(str(plot_id or "estate"), {"plot_id": plot_id, "hazards": {}})["hazards"][hazard_type] = _hazard_row_payload(row)
    summaries = {}
    for row in latest.values():
        bucket = summaries.setdefault(row.hazard_type, {"assessed": 0, "classes": {}})
        bucket["assessed"] += 1
        if row.risk_class:
            bucket["classes"][row.risk_class] = bucket["classes"].get(row.risk_class, 0) + 1
    return {"estate": {"id": estate.id, "name": estate.name}, "assessments": list(plot_results.values()), "summary": summaries, "assessment_count": len(rows)}


@router.get("/{estate_id}/hazards")
def estate_hazard_dashboard(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="plot.read")
    _require_hazard_plan(db, estate.organization_id)
    return _estate_hazard_dashboard_payload(db, estate)

@router.get("/{estate_id}/activity")
def estate_activity(estate_id:int, request:Request, limit:int=100, db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    require_estate_access(db,request,estate.organization_id,permission="audit.read")
    rows=db.query(EstateAuditEvent).filter(EstateAuditEvent.organization_id==estate.organization_id).order_by(EstateAuditEvent.created_at.desc()).limit(min(max(limit,1),200)).all()
    return [{"id":row.id,"action":row.action,"entity_type":row.entity_type,"entity_id":row.entity_id,"actor":row.actor_subject_id,"created_at":row.created_at,"details":row.after_data} for row in rows]


@router.get("/plots/{plot_id}/timeline")
def plot_timeline(plot_id: int, request: Request, db: Session = Depends(get_db)):
    """A single plot's own history, assembled across every entity type an audit event can be
    logged against for it - the plot record itself (created/geometry edited/subdivided/...), its
    reservation/allocation, its payments, its Survey request and its Staking task - since none of
    those share one common entity_id, a plain `entity_id == plot_id` filter would only ever surface
    the plot's own direct events and miss everything else that happened to it."""
    plot = db.get(EstatePlot, plot_id)
    if not plot:
        raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, plot.estate_id)
    require_estate_access(db, request, estate.organization_id, permission="audit.read")

    allocation_ids = [str(row.id) for row in db.query(EstateAllocation.id).filter(EstateAllocation.plot_id == plot_id).all()]
    payment_ids = [str(row.id) for row in db.query(EstatePayment.id).filter(EstatePayment.plot_id == plot_id).all()]
    survey_ids = [str(row.id) for row in db.query(EstateSurveyRequest.id).filter(EstateSurveyRequest.plot_id == plot_id).all()]
    staking_ids = [str(row.id) for row in db.query(EstateStakingTask.id).filter(EstateStakingTask.plot_id == plot_id).all()]

    conditions = [and_(EstateAuditEvent.entity_type == "estate_plot", EstateAuditEvent.entity_id == str(plot_id))]
    for entity_type, ids in (("estate_allocation", allocation_ids), ("estate_payment", payment_ids), ("estate_survey_request", survey_ids), ("estate_staking_task", staking_ids)):
        if ids:
            conditions.append(and_(EstateAuditEvent.entity_type == entity_type, EstateAuditEvent.entity_id.in_(ids)))

    rows = db.query(EstateAuditEvent).filter(EstateAuditEvent.organization_id == estate.organization_id, or_(*conditions)).order_by(EstateAuditEvent.created_at.desc()).limit(100).all()
    return [{"id": row.id, "action": row.action, "entity_type": row.entity_type, "actor": row.actor_subject_id, "created_at": row.created_at, "details": row.after_data} for row in rows]


@router.post("/{estate_id}/plots")
def create_plot(estate_id: int, payload: PlotCreate, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate: raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage"); _enabled(db, estate.organization_id)
    area, issues = validate_polygon(payload.geometry)
    if any(issue.severity == "error" for issue in issues): raise HTTPException(422, detail=[issue.message for issue in issues])
    normalized = _normalized(payload.plot_number)
    if db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id, EstatePlot.plot_number_normalized == normalized).first(): raise HTTPException(409, "Plot number already exists in this estate")
    if payload.block_id and not db.query(EstateBlock).filter(EstateBlock.id == payload.block_id, EstateBlock.estate_id == estate_id).first(): raise HTTPException(422, "Block does not belong to this estate")
    plot = EstatePlot(estate_id=estate_id, block_id=payload.block_id, plot_number=payload.plot_number.strip(), plot_number_normalized=normalized, geometry=from_shape(shape(payload.geometry), srid=4326), area_sqm=area, public_address=payload.public_address.strip() if payload.public_address else None, asking_price=payload.asking_price, land_use=payload.land_use, geometry_status=payload.geometry_status, created_by_subject_type=access.principal.subject_type, created_by_subject_id=access.principal.subject_id)
    db.add(plot); db.flush(); append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="plot.created", entity_type="estate_plot", entity_id=plot.id, after_data={"plot_number": plot.plot_number, "area_sqm": area, "geometry_status": plot.geometry_status})
    db.commit(); return {"id": plot.id, "uid": plot.plot_uid, "area_sqm": area, "qc": [{"severity": issue.severity, "code": issue.code, "message": issue.message} for issue in issues]}


@router.patch("/plots/{plot_id}/public-price")
def update_plot_public_price(plot_id: int, payload: PlotPriceUpdate, request: Request, db: Session = Depends(get_db)):
    plot = db.get(EstatePlot, plot_id)
    if not plot:
        raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, plot.estate_id)
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    previous = str(plot.asking_price) if plot.asking_price is not None else None
    plot.asking_price = payload.asking_price
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="plot.public_price_updated",
        entity_type="estate_plot",
        entity_id=plot.id,
        before_data={"asking_price": previous},
        after_data={"asking_price": str(plot.asking_price) if plot.asking_price is not None else None},
    )
    db.commit()
    return {"id": plot.id, "asking_price": str(plot.asking_price) if plot.asking_price is not None else None}


@router.patch("/plots/{plot_id}/public-address")
def update_plot_public_address(plot_id: int, payload: PlotAddressUpdate, request: Request, db: Session = Depends(get_db)):
    plot = db.get(EstatePlot, plot_id)
    if not plot:
        raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, plot.estate_id)
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    previous = plot.public_address
    plot.public_address = payload.public_address.strip() if payload.public_address else None
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="plot.public_address_updated",
        entity_type="estate_plot",
        entity_id=plot.id,
        before_data={"public_address": previous},
        after_data={"public_address": plot.public_address},
    )
    db.commit()
    return {"id": plot.id, "public_address": plot.public_address}


@router.patch("/{estate_id}/plot-listing-defaults")
def update_plot_listing_defaults(estate_id: int, payload: PlotListingDefaultsUpdate, request: Request, db: Session = Depends(get_db)):
    """Apply shared public address and/or price to every plot in an Estate."""
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    _enabled(db, estate.organization_id)
    if not payload.apply_address and not payload.apply_price:
        raise HTTPException(422, "Choose an address or price to apply")

    plots_query = db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id)
    updated_count = plots_query.count()
    address_value = payload.public_address.strip() if payload.public_address else None
    price_value = payload.asking_price
    changes = {}
    if payload.apply_address:
        changes["public_address"] = address_value
    if payload.apply_price:
        changes["asking_price"] = price_value
    plots_query.update(changes, synchronize_session=False)
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="estate.plot_listing_defaults_updated",
        entity_type="estate",
        entity_id=estate.id,
        after_data={
            "updated_plots": updated_count,
            "public_address": address_value if payload.apply_address else None,
            "asking_price": str(price_value) if payload.apply_price and price_value is not None else None,
        },
    )
    db.commit()
    return {
        "estate_id": estate.id,
        "updated_plots": updated_count,
        "public_address": address_value if payload.apply_address else None,
        "asking_price": str(price_value) if payload.apply_price and price_value is not None else None,
    }


@router.patch("/{estate_id}/plots/{plot_id}/geometry")
def update_plot_geometry(estate_id: int, plot_id: int, payload: PlotGeometryUpdate, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate: raise HTTPException(404, "Estate not found")
    plot = db.get(EstatePlot, plot_id)
    if not plot or plot.estate_id != estate_id: raise HTTPException(404, "Plot not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    if plot.commercial_status != "available":
        raise HTTPException(409, "Only available plots can have their boundary edited - reserved, allocated or developed plots are locked.")
    area, issues = validate_polygon(payload.geometry)
    if any(issue.severity == "error" for issue in issues):
        raise HTTPException(422, detail=[issue.message for issue in issues])
    before_area = float(plot.area_sqm)
    plot.geometry = from_shape(shape(payload.geometry), srid=4326)
    plot.area_sqm = area
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="plot.geometry_updated", entity_type="estate_plot", entity_id=plot.id, before_data={"area_sqm": before_area}, after_data={"area_sqm": area})
    db.commit()
    return {"id": plot.id, "area_sqm": area, "qc": [{"severity": issue.severity, "code": issue.code, "message": issue.message} for issue in issues]}


@router.delete("/{estate_id}/plots/{plot_id}")
def delete_estate_plot(estate_id: int, plot_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate: raise HTTPException(404, "Estate not found")
    plot = db.get(EstatePlot, plot_id)
    if not plot or plot.estate_id != estate_id: raise HTTPException(404, "Plot not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    if db.query(EstateAllocation).filter(EstateAllocation.plot_id == plot_id).first():
        raise HTTPException(409, "This plot has a customer reservation or allocation on record - remove that first before deleting the plot.")
    plot_number = plot.plot_number
    db.delete(plot)
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="plot.deleted", entity_type="estate_plot", entity_id=plot_id, before_data={"plot_number": plot_number})
    db.commit()
    return {"deleted": True, "id": plot_id}


@router.delete("/{estate_id}/layout")
def reset_estate_layout(estate_id: int, request: Request, db: Session = Depends(get_db)):
    """Deletes every plot AND every road/open-space/drainage layer in this Estate, clears its
    boundary, and discards any layout draft still awaiting review - a full "start the layout over
    from scratch". Blocked if any plot already carries a customer reservation or allocation.
    Blocks (labels like "Block A") are left alone since they're a reusable naming convention, not
    part of the generated geometry, and a pending draft is marked rejected rather than hard-deleted
    so it still shows up in its own history."""
    estate = db.get(Estate, estate_id)
    if not estate: raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.manage")
    plots = db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id).all()
    allocated_count = db.query(EstateAllocation).join(EstatePlot, EstateAllocation.plot_id == EstatePlot.id).filter(EstatePlot.estate_id == estate_id).count()
    if allocated_count:
        raise HTTPException(409, f"{allocated_count} plot(s) in this Estate already have a customer reservation or allocation - remove those first before resetting the layout.")
    plot_count = len(plots)
    for plot in plots:
        db.delete(plot)
    features = db.query(EstateSpatialFeature).filter(EstateSpatialFeature.estate_id == estate_id).all()
    feature_count = len(features)
    for feature in features:
        db.delete(feature)
    pending_proposals = db.query(EstateLayoutProposal).filter(EstateLayoutProposal.estate_id == estate_id, EstateLayoutProposal.status == "review_required").all()
    for proposal in pending_proposals:
        proposal.status = "rejected"
        proposal.reviewed_by_subject_type = access.principal.subject_type
        proposal.reviewed_by_subject_id = access.principal.subject_id
        proposal.reviewed_at = datetime.now(timezone.utc)
    estate.boundary = None
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="layout.reset", entity_type="estate", entity_id=estate.id, after_data={"plots_deleted": plot_count, "features_deleted": feature_count, "proposals_discarded": len(pending_proposals)})
    db.commit()
    return {"deleted_plots": plot_count, "deleted_features": feature_count}


@router.post("/{estate_id}/plots/{plot_id}/subdivide")
def subdivide_estate_plot(
    estate_id: int,
    plot_id: int,
    payload: EstateSubdivisionCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Replace an available Estate plot with approved, allocatable child plots.

    Estate subdivision is intentionally separate from Survey subdivision: the resulting records
    remain EstatePlot rows and can immediately enter the Estate reservation/allocation workflow.
    """
    estate = db.get(Estate, estate_id)
    plot = db.get(EstatePlot, plot_id)
    if not estate or not plot or plot.estate_id != estate_id:
        raise HTTPException(404, "Plot not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    _enabled(db, estate.organization_id)
    if plot.geometry_status != "approved":
        raise HTTPException(409, "Only an approved plot can be subdivided")
    if plot.commercial_status != "available":
        raise HTTPException(409, "Only an available plot can be subdivided")

    parent = to_shape(plot.geometry)
    if parent is None or parent.is_empty or not parent.is_valid:
        raise HTTPException(409, "This plot has invalid geometry and cannot be subdivided")
    metric_epsg = _metric_epsg_for_wgs84_polygon(parent)
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{metric_epsg}", always_xy=True).transform
    backward = Transformer.from_crs(f"EPSG:{metric_epsg}", "EPSG:4326", always_xy=True).transform
    metric_parent = shapely_transform(forward, parent)
    orientation = 0.0
    if len(metric_parent.exterior.coords) > 2:
        first = metric_parent.exterior.coords[0]
        second = metric_parent.exterior.coords[1]
        orientation = math.degrees(math.atan2(second[1] - first[1], second[0] - first[0]))
    pieces = _subdivide_polygon_equal_count(metric_parent, payload.split_count, orientation)

    child_numbers = [f"{plot.plot_number}-{index:02d}" for index in range(1, len(pieces) + 1)]
    existing_numbers = {
        value
        for (value,) in db.query(EstatePlot.plot_number_normalized)
        .filter(EstatePlot.estate_id == estate_id, EstatePlot.plot_number_normalized.in_([_normalized(number) for number in child_numbers]))
        .all()
    }
    if existing_numbers:
        raise HTTPException(409, "Some child plot numbers already exist. Choose a different source plot number.")

    created: list[EstatePlot] = []
    for index, (number, piece) in enumerate(zip(child_numbers, pieces), 1):
        child = shapely_transform(backward, piece)
        area, issues = validate_polygon(mapping(child))
        if any(issue.severity == "error" for issue in issues):
            raise HTTPException(400, "Subdivision produced an invalid child plot")
        row = EstatePlot(
            estate_id=estate_id,
            block_id=plot.block_id,
            plot_number=number,
            plot_number_normalized=_normalized(number),
            geometry=from_shape(child, srid=4326),
            area_sqm=area,
            land_use=plot.land_use,
            commercial_status="available",
            development_status="not_started",
            geometry_status="approved",
            source_type="subdivision",
            source_reference=plot.plot_uid,
            created_by_subject_type=access.principal.subject_type,
            created_by_subject_id=access.principal.subject_id,
        )
        db.add(row)
        created.append(row)

    plot.commercial_status = "on_hold"
    db.flush()
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=access.principal,
        action="plot.subdivided",
        entity_type="estate_plot",
        entity_id=plot.id,
        before_data={"plot_number": plot.plot_number, "commercial_status": "available"},
        after_data={"commercial_status": "on_hold", "child_plot_ids": [child.id for child in created], "split_count": len(created)},
    )
    db.commit()
    return {
        "parent": {"id": plot.id, "plot_number": plot.plot_number, "commercial_status": plot.commercial_status},
        "created_count": len(created),
        "plots": [{"id": child.id, "plot_number": child.plot_number, "area_sqm": str(child.area_sqm), "commercial_status": child.commercial_status} for child in created],
    }

@router.patch("/plots/{plot_id}/development-status")
def update_development_status(plot_id:int, payload:DevelopmentStatusUpdate, request:Request, db:Session=Depends(get_db)):
    plot=db.get(EstatePlot,plot_id)
    if not plot: raise HTTPException(404,"Plot not found")
    estate=db.get(Estate,plot.estate_id); access=require_estate_access(db,request,estate.organization_id,permission="plot.manage")
    previous=plot.development_status; plot.development_status=payload.status
    append_estate_audit_event(db,organization_id=estate.organization_id,actor=access.principal,action="plot.development_status_changed",entity_type="estate_plot",entity_id=plot.id,before_data={"development_status":previous},after_data={"development_status":plot.development_status})
    db.commit()
    customer_notified = False
    if plot.development_status == "developed" and previous != "developed":
        active_allocation = db.query(EstateAllocation).filter(EstateAllocation.plot_id == plot.id, EstateAllocation.status.in_(("reserved", "allocated"))).one_or_none()
        if active_allocation:
            customer_notified = _notify_allocation_customer(db, allocation=active_allocation, org_name=access.organization_name, event="land_developed")
    return {"id":plot.id,"development_status":plot.development_status,"customer_notified":customer_notified}

@router.post("/plots/{plot_id}/inspections")
def create_field_inspection(plot_id:int, payload:FieldInspectionCreate, request:Request, db:Session=Depends(get_db)):
    plot=db.get(EstatePlot,plot_id)
    if not plot: raise HTTPException(404,"Plot not found")
    estate=db.get(Estate,plot.estate_id); access=require_estate_access(db,request,estate.organization_id,permission="field.manage")
    inspection=EstateFieldInspection(organization_id=estate.organization_id,estate_id=estate.id,plot_id=plot.id,inspection_type=payload.inspection_type.strip(),outcome=payload.outcome,notes=payload.notes,inspected_by_subject_type=access.principal.subject_type,inspected_by_subject_id=access.principal.subject_id)
    db.add(inspection); db.flush(); append_estate_audit_event(db,organization_id=estate.organization_id,actor=access.principal,action="field_inspection.recorded",entity_type="estate_field_inspection",entity_id=inspection.id,after_data={"plot_id":plot.id,"outcome":inspection.outcome}); db.commit()
    return {"id":inspection.id,"plot_id":plot.id,"outcome":inspection.outcome,"inspected_at":inspection.inspected_at}

@router.get("/plots/{plot_id}/inspections")
def list_field_inspections(plot_id:int, request:Request, db:Session=Depends(get_db)):
    plot=db.get(EstatePlot,plot_id)
    if not plot: raise HTTPException(404,"Plot not found")
    estate=db.get(Estate,plot.estate_id); require_estate_access(db,request,estate.organization_id,permission="field.read")
    rows=db.query(EstateFieldInspection).filter(EstateFieldInspection.plot_id==plot.id).order_by(EstateFieldInspection.inspected_at.desc()).all()
    return [{"id":row.id,"type":row.inspection_type,"outcome":row.outcome,"notes":row.notes,"inspected_at":row.inspected_at,"inspected_by":row.inspected_by_subject_id} for row in rows]

@router.post("/plots/{plot_id}/survey-requests")
def create_survey_request(plot_id:int, request:Request, db:Session=Depends(get_db)):
    plot=db.get(EstatePlot,plot_id)
    if not plot: raise HTTPException(404,"Plot not found")
    estate=db.get(Estate,plot.estate_id); access=require_estate_access(db,request,estate.organization_id,permission="survey.manage")
    if plot.geometry_status != "approved": raise HTTPException(409,"Only approved plot geometry can be sent to Survey")
    _,issues=validate_polygon(to_shape(plot.geometry).__geo_interface__)
    if any(i.severity=="error" for i in issues): raise HTTPException(422,"Plot geometry is not valid")
    if db.query(EstateSurveyRequest).filter(EstateSurveyRequest.plot_id==plot_id,EstateSurveyRequest.status.in_(("requested","assigned","in_progress","ready_for_review","approved","failed"))).first(): raise HTTPException(409,"An active Survey request already exists")
    allocation=db.query(EstateAllocation).filter(EstateAllocation.plot_id==plot_id,EstateAllocation.status=="allocated").one_or_none()
    row=EstateSurveyRequest(organization_id=estate.organization_id,estate_id=estate.id,plot_id=plot_id,allocation_id=allocation.id if allocation else None,requested_by_subject_type=access.principal.subject_type,requested_by_subject_id=access.principal.subject_id,status="requested")
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.query(EstateSurveyRequest).filter(EstateSurveyRequest.plot_id == plot_id, EstateSurveyRequest.status.in_(("requested", "assigned", "in_progress", "ready_for_review", "approved", "failed"))).first()
        if existing:
            return _survey_payload(db, existing)
        raise HTTPException(status_code=409, detail="An active Survey request already exists")
    append_estate_audit_event(db,organization_id=estate.organization_id,actor=access.principal,action="survey_request.created",entity_type="estate_survey_request",entity_id=row.id); db.commit(); return _survey_payload(db,row)

@router.get("/survey-requests")
def list_survey_requests(request: Request, estate_id: int | None = None, page: int | None = None, page_size: int = 20, db: Session = Depends(get_db)):
    principal=resolve_estate_principal(db,request); allowed={a.organization_id for a in list_estate_access(db,principal) if has_permission(a.role_key,"survey.read")}
    query = db.query(EstateSurveyRequest).filter(EstateSurveyRequest.organization_id.in_(allowed))
    if estate_id is not None:
        query = query.filter(EstateSurveyRequest.estate_id == estate_id)
    ordered = query.order_by(EstateSurveyRequest.created_at.desc())
    if page is None:
        return _bulk_survey_payloads(db, ordered.all())
    safe_page = max(1, page)
    safe_page_size = min(max(page_size, 1), 50)
    total = ordered.count()
    rows = ordered.offset((safe_page - 1) * safe_page_size).limit(safe_page_size).all()
    return {"items": _bulk_survey_payloads(db, rows), "page": safe_page, "page_size": safe_page_size, "total": total}

@router.get("/survey-requests/{request_id}")
def survey_request_detail(request_id:int,request:Request,db:Session=Depends(get_db)):
    row=db.get(EstateSurveyRequest,request_id)
    if not row: raise HTTPException(404,"Survey request not found")
    require_estate_access(db,request,row.organization_id,permission="survey.read")
    return _survey_payload(db,row)

@router.post("/survey-requests/{request_id}/assign")
def assign_survey_request(request_id:int,payload:SurveyorAssignment,request:Request,db:Session=Depends(get_db)):
    row=db.get(EstateSurveyRequest,request_id)
    if not row: raise HTTPException(404,"Survey request not found")
    access=require_estate_access(db,request,row.organization_id,permission="survey.manage")
    from app.models.estate_foundation import EstateOrganizationMember
    member=db.query(EstateOrganizationMember).filter(EstateOrganizationMember.organization_id==row.organization_id,EstateOrganizationMember.subject_type==payload.subject_type,EstateOrganizationMember.subject_id==payload.subject_id,EstateOrganizationMember.is_active.is_(True)).one_or_none()
    if not member or not has_permission(member.role_key,"survey.manage"): raise HTTPException(422,"Assignee is not a Survey-capable organization member")
    transition(row,"assigned"); row.assigned_surveyor_subject_type=member.subject_type; row.assigned_surveyor_subject_id=member.subject_id
    append_estate_audit_event(db,organization_id=row.organization_id,actor=access.principal,action="survey_request.assigned",entity_type="estate_survey_request",entity_id=row.id); db.commit(); return _survey_payload(db,row)

@router.post("/survey-requests/{request_id}/cancel")
def cancel_survey_request(request_id:int,request:Request,db:Session=Depends(get_db)):
    row=db.get(EstateSurveyRequest,request_id)
    if not row: raise HTTPException(404,"Survey request not found")
    access=require_estate_access(db,request,row.organization_id,permission="survey.manage")
    transition(row,"cancelled")
    append_estate_audit_event(db,organization_id=row.organization_id,actor=access.principal,action="survey_request.cancelled",entity_type="estate_survey_request",entity_id=row.id)
    db.commit(); return _survey_payload(db,row)

def _resolve_survey_owner_user_id(db: Session, principal) -> int:
    """A Survey working plot is owned by an actual Survey-product account, a separate identity
    domain from Estates. A caller already authenticated as a Survey user owns it directly; an
    Estate-account caller (the common case - an org manager acting from the Estates dashboard, not
    signed into Survey separately) is linked by their verified Estate email instead, using the same
    find-or-create-by-email lookup Survey's own passwordless login already uses - so the resulting
    plot is still owned by a real, identifiable account tied to who actually triggered this, not an
    anonymous or shared owner."""
    if principal.subject_type == "survey_user":
        return int(principal.subject_id)
    if principal.subject_type == "estate_account":
        account = db.get(EstateAccount, int(principal.subject_id))
        if account and account.email:
            return find_or_create_survey_user(db, email=account.email, full_name=account.full_name)
    raise HTTPException(403, "Starting Survey requires an authenticated Survey user")


@router.post("/survey-requests/{request_id}/start")
def start_survey_request(request_id:int,request:Request,db:Session=Depends(get_db)):
    row=db.get(EstateSurveyRequest,request_id)
    if not row: raise HTTPException(404,"Survey request not found")
    access=require_estate_access(db,request,row.organization_id,permission="survey.manage")
    if row.survey_working_plot_id: return _survey_payload(db,row)
    survey_owner_user_id = _resolve_survey_owner_user_id(db, access.principal)
    eligibility = survey_eligibility(db, db.get(EstateAllocation, row.allocation_id) if row.allocation_id else None)
    if not eligibility["eligible"]: raise HTTPException(409, eligibility["reason"])
    transition(row,"in_progress")
    try:
        result=materialize_estate_plot_for_survey(db,request=row,plot=db.get(EstatePlot,row.plot_id),survey_owner_user_id=survey_owner_user_id)
        append_estate_audit_event(db,organization_id=row.organization_id,actor=access.principal,action="survey_request.started",entity_type="estate_survey_request",entity_id=row.id,after_data={"survey_plot_id":result.survey_plot_id}); db.commit()
    except Exception:
        db.rollback(); raise HTTPException(422,"Survey materialization failed")
    return _survey_payload(db,row)

@router.post("/survey-requests/{request_id}/survey-session")
def issue_survey_request_session(request_id:int,request:Request,db:Session=Depends(get_db)):
    """The Survey working plot behind an Estate survey request is owned by a real Survey account
    (see _resolve_survey_owner_user_id), not shared/anonymous - so opening it from the Estates
    dashboard only works if the browser's Survey-side session already belongs to that exact
    account. That's rarely true: the request may have been started by a different team member, on
    a different device, or the browser's existing Survey session (if any) may simply be for
    someone else's unrelated work. Rather than surface that mismatch as an error, anyone with
    Estate access to this survey request gets a fresh Survey session for the plot's actual owner
    here, and the frontend swaps it in before handing off - so opening always just works,
    regardless of who started it or what Survey session happened to already be active."""
    row=db.get(EstateSurveyRequest,request_id)
    if not row: raise HTTPException(404,"Survey request not found")
    require_estate_access(db,request,row.organization_id,permission="survey.manage")
    if not row.survey_working_plot_id: raise HTTPException(409,"Survey has not been started for this request yet")
    plot=db.get(Plot,row.survey_working_plot_id)
    if not plot or not plot.owner_user_id: raise HTTPException(404,"Survey working plot not found")
    return {"survey_session": issue_survey_session(db, user_id=plot.owner_user_id)}


@router.post("/survey-requests/{request_id}/complete")
def complete_survey_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(EstateSurveyRequest, request_id)
    if not row:
        raise HTTPException(404, "Survey request not found")
    access = require_estate_access(db, request, row.organization_id, permission="survey.manage")
    if not row.survey_working_plot_id:
        raise HTTPException(409, "Survey must be opened before it can be completed")
    if row.status == "completed":
        return _survey_payload(db, row)
    transition(row, "completed")
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="survey_request.completed", entity_type="estate_survey_request", entity_id=row.id)
    db.commit()
    customer_notified = False
    if row.allocation_id:
        allocation = db.get(EstateAllocation, row.allocation_id)
        if allocation:
            customer_notified = _notify_allocation_customer(db, allocation=allocation, org_name=access.organization_name, event="survey_ready")
    return {**_survey_payload(db, row), "customer_notified": customer_notified}

@router.post("/survey-requests/{request_id}/staking-tasks")
def create_staking_task(request_id:int,request:Request,db:Session=Depends(get_db)):
    survey=db.get(EstateSurveyRequest,request_id)
    if not survey: raise HTTPException(404,"Survey request not found")
    access=require_estate_access(db,request,survey.organization_id,permission="staking.manage")
    if not survey.survey_working_plot_id: raise HTTPException(409,"Survey must be started before staking")
    existing=db.query(EstateStakingTask).filter(EstateStakingTask.survey_request_id==survey.id,EstateStakingTask.status.in_(("pending","assigned","in_progress"))).one_or_none()
    if existing: return {"id":existing.id,"status":existing.status}
    task=EstateStakingTask(organization_id=survey.organization_id,estate_id=survey.estate_id,plot_id=survey.plot_id,survey_request_id=survey.id,status="pending")
    db.add(task)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.query(EstateStakingTask).filter(EstateStakingTask.survey_request_id == survey.id, EstateStakingTask.status.in_(("pending", "assigned", "in_progress"))).one_or_none()
        if existing:
            return {"id": existing.id, "status": existing.status}
        raise HTTPException(status_code=409, detail="An active staking task already exists")
    append_estate_audit_event(db,organization_id=survey.organization_id,actor=access.principal,action="staking_task.created",entity_type="estate_staking_task",entity_id=task.id); db.commit(); return {"id":task.id,"status":task.status}

@router.get("/staking-tasks")
def list_staking_tasks(request: Request, estate_id: int | None = None, page: int | None = None, page_size: int = 20, db: Session = Depends(get_db)):
    principal=resolve_estate_principal(db,request); allowed={item.organization_id for item in list_estate_access(db,principal) if has_permission(item.role_key,"staking.read")}
    query = db.query(EstateStakingTask).filter(EstateStakingTask.organization_id.in_(allowed))
    if estate_id is not None:
        query = query.filter(EstateStakingTask.estate_id == estate_id)
    ordered = query.order_by(EstateStakingTask.created_at.desc())
    if page is None:
        rows = ordered.all()
        return [{"id":row.id,"status":row.status,"plot_id":row.plot_id,"survey_request_id":row.survey_request_id,"assigned_subject_id":row.assigned_subject_id,"completed_at":row.completed_at} for row in rows]
    safe_page = max(1, page)
    safe_page_size = min(max(page_size, 1), 50)
    total = ordered.count()
    rows = ordered.offset((safe_page - 1) * safe_page_size).limit(safe_page_size).all()
    return {"items": [{"id":row.id,"status":row.status,"plot_id":row.plot_id,"survey_request_id":row.survey_request_id,"assigned_subject_id":row.assigned_subject_id,"completed_at":row.completed_at} for row in rows], "page": safe_page, "page_size": safe_page_size, "total": total}

@router.get("/staking-tasks/{task_id}/exports/dgps.csv")
def export_staking_task_dgps_csv(task_id:int, request:Request, raw:bool=False, db:Session=Depends(get_db)):
    """Export a plot's authoritative vertices for field DGPS stakeout."""
    task=db.get(EstateStakingTask,task_id)
    if not task: raise HTTPException(404,"Staking task not found")
    access=require_estate_access(db,request,task.organization_id,permission="staking.read")
    plot=db.get(EstatePlot,task.plot_id)
    if not plot or not plot.geometry: raise HTTPException(422,"Staking task has no plot geometry")
    if plot.geometry_status != "approved": raise HTTPException(409,"Only approved plot geometry can be exported for staking")
    polygon=to_shape(plot.geometry)
    _,issues=validate_polygon(polygon.__geo_interface__)
    if any(issue.severity=="error" for issue in issues): raise HTTPException(422,"Plot geometry is not valid for staking")
    coordinates=list(polygon.exterior.coords)
    if coordinates and coordinates[0] == coordinates[-1]: coordinates=coordinates[:-1]
    if len(coordinates)<3: raise HTTPException(422,"Plot geometry needs at least three vertices")
    longitude,latitude=coordinates[0]
    coordinate_system=resolve_coordinate_system_key("wgs84_nigeria_meters",longitude,latitude)
    epsg=int(COORDINATE_SYSTEMS[coordinate_system]["epsg"])
    transformer=Transformer.from_crs("EPSG:4326",f"EPSG:{epsg}",always_xy=True)
    rows=[]
    for index,(lng,lat) in enumerate(coordinates):
        easting,northing=transformer.transform(float(lng),float(lat))
        rows.append({"station":alpha_station(index),"feature":f"Plot {plot.plot_number}","coordinate_system":coordinate_system,"easting":easting,"northing":northing,"longitude":lng,"latitude":lat,"point_type":"Plot vertex"})
    append_estate_audit_event(db,organization_id=task.organization_id,actor=access.principal,action="staking_task.dgps_exported",entity_type="estate_staking_task",entity_id=task.id,after_data={"vertex_count":len(rows),"coordinate_system":coordinate_system})
    db.commit()
    safe_number=re.sub(r"[^A-Za-z0-9._-]+","-",plot.plot_number).strip("-.") or f"plot-{plot.id}"
    return Response(render_dgps_staking_csv(rows,raw=raw),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":f'attachment; filename="{safe_number}_DGPS_Staking.csv"',"Cache-Control":"no-store, no-cache, must-revalidate","Pragma":"no-cache"})


@router.get("/{estate_id}/plots/{plot_id}/exports/dgps.csv")
def export_plot_dgps_csv(estate_id: int, plot_id: int, request: Request, coordinate_system: str = "wgs84_nigeria_meters", raw: bool = False, db: Session = Depends(get_db)):
    """Export any plot's known boundary vertices as a DGPS CSV, in whichever coordinate system the
    caller chooses - not gated on a staking task existing. A plot that's already been subdivided
    already has every coordinate this needs; requiring a separate staking task first (the older
    /staking-tasks/{id}/exports/dgps.csv endpoint above, kept for that specific field-staking
    record) was an unnecessary block for someone who just wants this plot's points."""
    plot = db.query(EstatePlot).filter(EstatePlot.id == plot_id, EstatePlot.estate_id == estate_id).one_or_none()
    if not plot: raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, estate_id)
    access = require_estate_access(db, request, estate.organization_id, permission="plot.read")
    if not plot.geometry: raise HTTPException(422, "Plot has no geometry to export")
    if plot.geometry_status != "approved": raise HTTPException(409, "Only approved plot geometry can be exported")
    polygon = to_shape(plot.geometry)
    _, issues = validate_polygon(polygon.__geo_interface__)
    if any(issue.severity == "error" for issue in issues): raise HTTPException(422, "Plot geometry is not valid for export")
    coordinates = list(polygon.exterior.coords)
    if coordinates and coordinates[0] == coordinates[-1]: coordinates = coordinates[:-1]
    if len(coordinates) < 3: raise HTTPException(422, "Plot geometry needs at least three vertices")
    sample_lng, sample_lat = coordinates[0]
    resolved_coordinate_system = resolve_coordinate_system_key(coordinate_system, sample_lng, sample_lat)
    target_points = convert_coordinates([[float(lng), float(lat)] for lng, lat in coordinates], "wgs84", resolved_coordinate_system)
    rows = [
        {
            "station": alpha_station(index),
            "feature": f"Plot {plot.plot_number}",
            "coordinate_system": resolved_coordinate_system,
            "easting": target_points[index][0],
            "northing": target_points[index][1],
            "longitude": lng,
            "latitude": lat,
            "point_type": "Plot vertex",
        }
        for index, (lng, lat) in enumerate(coordinates)
    ]
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="plot.dgps_exported", entity_type="estate_plot", entity_id=plot.id, after_data={"vertex_count": len(rows), "coordinate_system": resolved_coordinate_system})
    db.commit()
    safe_number = re.sub(r"[^A-Za-z0-9._-]+", "-", plot.plot_number).strip("-.") or f"plot-{plot.id}"
    return Response(
        render_dgps_staking_csv(rows, raw=raw),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_number}_DGPS.csv"', "Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/{estate_id}/exports/layout-dgps.csv")
def export_estate_layout_dgps_csv(estate_id: int, request: Request, coordinate_system: str = "wgs84_nigeria_meters", raw: bool = False, db: Session = Depends(get_db)):
    """Exports every approved plot's boundary vertices in this Estate as one combined DGPS CSV for
    a whole-layout stakeout, instead of exporting and carrying one file per plot. Station labels are
    prefixed with the plot number (e.g. P-004-A) so points stay unambiguous once every plot's
    vertices are mixed into a single job."""
    estate = db.get(Estate, estate_id)
    if not estate: raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.read")
    plots = db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry_status == "approved").order_by(EstatePlot.plot_number).all()
    plots = [plot for plot in plots if plot.geometry]
    if not plots:
        raise HTTPException(422, "This Estate has no approved plot geometry to export")
    sample_polygon = to_shape(plots[0].geometry)
    sample_lng, sample_lat = list(sample_polygon.exterior.coords)[0]
    resolved_coordinate_system = resolve_coordinate_system_key(coordinate_system, sample_lng, sample_lat)
    rows: list[dict] = []
    skipped: list[str] = []
    for plot in plots:
        polygon = to_shape(plot.geometry)
        _, issues = validate_polygon(polygon.__geo_interface__)
        if any(issue.severity == "error" for issue in issues):
            skipped.append(plot.plot_number)
            continue
        coordinates = list(polygon.exterior.coords)
        if coordinates and coordinates[0] == coordinates[-1]: coordinates = coordinates[:-1]
        if len(coordinates) < 3:
            skipped.append(plot.plot_number)
            continue
        target_points = convert_coordinates([[float(lng), float(lat)] for lng, lat in coordinates], "wgs84", resolved_coordinate_system)
        for index, (lng, lat) in enumerate(coordinates):
            rows.append({
                "station": f"{plot.plot_number}-{alpha_station(index)}",
                "feature": f"Plot {plot.plot_number}",
                "coordinate_system": resolved_coordinate_system,
                "easting": target_points[index][0],
                "northing": target_points[index][1],
                "longitude": lng,
                "latitude": lat,
                "point_type": "Plot vertex",
            })
    if not rows:
        raise HTTPException(422, "No plots had exportable geometry")
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="estate.layout_dgps_exported", entity_type="estate", entity_id=estate.id, after_data={"plot_count": len(plots) - len(skipped), "vertex_count": len(rows), "coordinate_system": resolved_coordinate_system, "skipped_plots": skipped})
    db.commit()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", estate.name).strip("-.") or f"estate-{estate.id}"
    return Response(
        render_dgps_staking_csv(rows, raw=raw),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_Layout_DGPS.csv"', "Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/{estate_id}/exports/layout.pdf")
def export_estate_layout_pdf(estate_id: int, request: Request, paper_size: str = "A3", include_customer_names: bool = False, db: Session = Depends(get_db)):
    """Renders every plot, road, drainage reserve and open space in this Estate as one clean,
    single-page site layout plan PDF - the whole subdivision at once, not one plot at a time.
    paper_size: A0-A4. include_customer_names: adds each allocated/reserved plot's customer name
    under its area - leave off for a clean, name-free plan suited to marketing/PR use."""
    if paper_size.strip().upper() not in {"A0", "A1", "A2", "A3", "A4"}:
        raise HTTPException(422, "paper_size must be one of A0, A1, A2, A3, A4")
    estate = db.get(Estate, estate_id)
    if not estate: raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.read")
    plots = db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry_status == "approved").order_by(EstatePlot.plot_number).all()
    plots = [plot for plot in plots if plot.geometry]
    features = db.query(EstateSpatialFeature).filter(EstateSpatialFeature.estate_id == estate_id, EstateSpatialFeature.status == "active").all()
    if not plots and estate.boundary is None:
        raise HTTPException(422, "This Estate has no plots or boundary to draw yet")
    customer_names_by_plot_id: dict[int, str] = {}
    if include_customer_names:
        allocations = db.query(EstateAllocation).filter(EstateAllocation.estate_id == estate_id, EstateAllocation.status.in_(("reserved", "allocated"))).all()
        customer_ids = {allocation.customer_id for allocation in allocations}
        customers_by_id = {customer.id: customer.full_name for customer in db.query(EstateCustomer).filter(EstateCustomer.id.in_(customer_ids)).all()} if customer_ids else {}
        customer_names_by_plot_id = {allocation.plot_id: customers_by_id[allocation.customer_id] for allocation in allocations if allocation.customer_id in customers_by_id}
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_path = tmp_file.name
        result = render_estate_layout_pdf(estate=estate, organization_name=access.organization_name, plots=plots, features=features, to_shape_fn=to_shape, output_path=tmp_path, paper_size=paper_size, customer_names_by_plot_id=customer_names_by_plot_id or None)
        with open(tmp_path, "rb") as handle:
            pdf_bytes = handle.read()
    except ValueError as error:
        raise HTTPException(422, str(error))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="estate.layout_pdf_exported", entity_type="estate", entity_id=estate.id, after_data=result)
    db.commit()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", estate.name).strip("-.") or f"estate-{estate.id}"
    return Response(pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{safe_name}_Layout_Plan.pdf"'})


@router.get("/{estate_id}/exports/report.pdf")
def export_estate_report_pdf(estate_id: int, request: Request, db: Session = Depends(get_db)):
    """A generated, print-ready Estate Performance Report - inventory, financial, geometry/hazard
    and recent-activity figures plus a layout snapshot, all in one branded PDF - rather than a
    browser Print of the on-screen Reports page."""
    estate = db.get(Estate, estate_id)
    if not estate: raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.read")
    dashboard = estate_dashboard(estate_id, request, db)
    quality = estate_quality_check(estate_id, request, db)
    hazard_data = estate_hazard_dashboard(estate_id, request, db)
    activity = estate_activity(estate_id, request, limit=10, db=db)
    plots = db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry_status == "approved").order_by(EstatePlot.plot_number).all()
    features = db.query(EstateSpatialFeature).filter(EstateSpatialFeature.estate_id == estate_id, EstateSpatialFeature.status == "active").all()
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_path = tmp_file.name
        result = render_estate_report_pdf(estate=estate, organization_name=access.organization_name, dashboard=dashboard, quality=quality, hazards=hazard_data, activity=activity, plots=plots, features=features, to_shape_fn=to_shape, output_path=tmp_path)
        with open(tmp_path, "rb") as handle:
            pdf_bytes = handle.read()
    except ValueError as error:
        raise HTTPException(422, str(error))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="estate.report_pdf_exported", entity_type="estate", entity_id=estate.id, after_data=result)
    db.commit()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", estate.name).strip("-.") or f"estate-{estate.id}"
    return Response(pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{safe_name}_Performance_Report.pdf"'})


@router.post("/staking-tasks/{task_id}/start")
def start_staking_task(task_id:int,request:Request,db:Session=Depends(get_db)):
    task=db.get(EstateStakingTask,task_id)
    if not task: raise HTTPException(404,"Staking task not found")
    access=require_estate_access(db,request,task.organization_id,permission="staking.manage")
    if task.status not in {"pending","assigned"}: raise HTTPException(409,"Staking task cannot be started")
    task.status="in_progress"
    task.assigned_subject_type = task.assigned_subject_type or access.principal.subject_type
    task.assigned_subject_id = task.assigned_subject_id or access.principal.subject_id
    append_estate_audit_event(db,organization_id=task.organization_id,actor=access.principal,action="staking_task.started",entity_type="estate_staking_task",entity_id=task.id); db.commit()
    return {"id":task.id,"status":task.status}

@router.post("/staking-tasks/{task_id}/complete")
def complete_staking_task(task_id:int,request:Request,db:Session=Depends(get_db)):
    task=db.get(EstateStakingTask,task_id)
    if not task: raise HTTPException(404,"Staking task not found")
    access=require_estate_access(db,request,task.organization_id,permission="staking.manage")
    if task.status != "in_progress": raise HTTPException(409,"Only in-progress staking tasks can be completed")
    from datetime import datetime, timezone
    task.status="completed"; task.completed_at=datetime.now(timezone.utc)
    task.assigned_subject_type = task.assigned_subject_type or access.principal.subject_type
    task.assigned_subject_id = task.assigned_subject_id or access.principal.subject_id
    append_estate_audit_event(db,organization_id=task.organization_id,actor=access.principal,action="staking_task.completed",entity_type="estate_staking_task",entity_id=task.id); db.commit()
    active_allocation = db.query(EstateAllocation).filter(EstateAllocation.plot_id == task.plot_id, EstateAllocation.status.in_(("reserved", "allocated"))).one_or_none()
    customer_notified = False
    if active_allocation:
        customer_notified = _notify_allocation_customer(db, allocation=active_allocation, org_name=access.organization_name, event="staked")
    return {"id":task.id,"status":task.status,"completed_at":task.completed_at,"customer_notified":customer_notified}


@router.get("/organizations/{organization_id}/customers")
def list_customers(organization_id: int, request: Request, page: int = 1, page_size: int = 25, search: str | None = None, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="customer.read")
    query = db.query(EstateCustomer).filter(EstateCustomer.organization_id == organization_id)
    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(EstateCustomer.full_name).like(term),
                func.lower(EstateCustomer.phone).like(term),
                func.lower(EstateCustomer.email).like(term),
            )
        )
    ordered = query.order_by(EstateCustomer.created_at.desc(), EstateCustomer.id.desc())
    safe_page = max(1, page)
    safe_page_size = min(max(page_size, 1), 100)
    total = ordered.count()
    rows = ordered.offset((safe_page - 1) * safe_page_size).limit(safe_page_size).all()
    return {"items": [{"id": row.id, "name": row.full_name, "reference": row.reference_no} for row in rows], "page": safe_page, "page_size": safe_page_size, "total": total}


@router.post("/organizations/{organization_id}/customers")
def create_customer(organization_id: int, payload: CustomerCreate, request: Request, idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"), db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="customer.manage"); _enabled(db, organization_id)
    idempotency_key = _normalize_idempotency_key(idempotency_header)
    if idempotency_key:
        existing = db.query(EstateCustomer).filter(EstateCustomer.organization_id == organization_id, EstateCustomer.idempotency_key == idempotency_key).one_or_none()
        if existing:
            return {"id": existing.id, "uid": existing.customer_uid, "name": existing.full_name, "already_processed": True}
    customer = EstateCustomer(organization_id=organization_id, idempotency_key=idempotency_key, full_name=payload.full_name.strip(), full_name_normalized=_normalized(payload.full_name), reference_no=payload.reference_no, phone=payload.phone, email=payload.email, address=payload.address, company_name=payload.company_name, notes=payload.notes)
    db.add(customer)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = db.query(EstateCustomer).filter(EstateCustomer.organization_id == organization_id, EstateCustomer.idempotency_key == idempotency_key).one_or_none()
            if existing:
                return {"id": existing.id, "uid": existing.customer_uid, "name": existing.full_name, "already_processed": True}
        raise HTTPException(status_code=409, detail="This customer was already added")
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="customer.created", entity_type="estate_customer", entity_id=customer.id, after_data={"name": customer.full_name})
    db.commit(); return {"id": customer.id, "uid": customer.customer_uid, "name": customer.full_name, "already_processed": False}


@router.get("/organizations/{organization_id}/survey-eligibility")
def get_survey_eligibility_rule(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="estate.read")
    rule = db.query(EstatePaymentRule).filter(EstatePaymentRule.organization_id == organization_id, EstatePaymentRule.rule_key == "survey_minimum_confirmed_percentage").one_or_none()
    return {"organization_id": organization_id, "is_enabled": bool(rule.is_enabled) if rule else False, "percentage": str(rule.percentage or 0) if rule else "0", "description": rule.description if rule else None}


@router.put("/organizations/{organization_id}/survey-eligibility")
def update_survey_eligibility_rule(organization_id: int, payload: SurveyEligibilityUpdate, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="estate.manage")
    rule = db.query(EstatePaymentRule).filter(EstatePaymentRule.organization_id == organization_id, EstatePaymentRule.rule_key == "survey_minimum_confirmed_percentage").one_or_none()
    if rule is None:
        rule = EstatePaymentRule(organization_id=organization_id, rule_key="survey_minimum_confirmed_percentage")
        db.add(rule)
    rule.is_enabled = payload.is_enabled
    rule.percentage = payload.percentage
    rule.description = payload.description
    db.flush()
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="survey_eligibility.updated", entity_type="estate_payment_rule", entity_id=rule.id, after_data={"is_enabled": rule.is_enabled, "percentage": str(rule.percentage)})
    db.commit()
    return {"organization_id": organization_id, "is_enabled": rule.is_enabled, "percentage": str(rule.percentage), "description": rule.description}


@router.get("/organizations/{organization_id}/sales-agents")
def list_sales_agents(organization_id: int, request: Request, db: Session = Depends(get_db)):
    """Any active org member can be tagged as the sales agent on a reservation/allocation - not
    just members with the "sales" role, since a manager or owner closing a deal directly should be
    creditable too."""
    require_estate_access(db, request, organization_id, permission="allocation.manage")
    rows = db.query(EstateOrganizationMember).filter(EstateOrganizationMember.organization_id == organization_id, EstateOrganizationMember.is_active.is_(True)).all()
    tiers = commissions._tier_dicts(commissions.get_commission_tiers(db, organization_id))
    results = []
    for row in rows:
        display_name = row.subject_id
        if row.subject_type == "estate_account":
            account = db.get(EstateAccount, int(row.subject_id)) if row.subject_id.isdigit() else None
            if account:
                display_name = account.full_name
        # The tier that would apply to this agent's NEXT sale, given what they've already closed -
        # shown up front so it's never a surprise what rate a deal will earn them.
        cumulative_volume = commissions.cumulative_sales_before(db, organization_id=organization_id, subject_type=row.subject_type, subject_id=row.subject_id)
        current_tier = commissions.resolve_tier(tiers, cumulative_volume)
        results.append({
            "subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key, "display_name": display_name, "email": row.contact_email, "phone": row.contact_phone,
            "cumulative_volume": str(cumulative_volume),
            "current_tier_label": current_tier["label"],
            "current_tier_rate_percent": str(current_tier["rate_percent"]),
        })
    return results


@router.get("/organizations/{organization_id}/commission-tiers")
def get_commission_tiers_endpoint(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="estate.read")
    rows = commissions.get_commission_tiers(db, organization_id)
    if not rows:
        return {"organization_id": organization_id, "tiers": [{"label": t["label"], "min_cumulative_sales": str(t["min_cumulative_sales"]), "rate_percent": str(t["rate_percent"])} for t in commissions.DEFAULT_TIERS], "using_defaults": True}
    return {"organization_id": organization_id, "tiers": [{"id": row.id, "label": row.label, "min_cumulative_sales": str(row.min_cumulative_sales), "rate_percent": str(row.rate_percent)} for row in rows], "using_defaults": False}


@router.put("/organizations/{organization_id}/commission-tiers")
def set_commission_tiers_endpoint(organization_id: int, payload: CommissionTiersUpdate, request: Request, db: Session = Depends(get_db)):
    """Replaces the whole tier ladder at once - simpler to reason about than per-row CRUD for a
    short, always-fully-visible list. Past commissions already locked into an allocation are
    untouched; only sales priced after this call see the new ladder."""
    access = require_estate_access(db, request, organization_id, permission="estate.manage")
    db.query(EstateCommissionTier).filter(EstateCommissionTier.organization_id == organization_id).delete()
    for tier in payload.tiers:
        db.add(EstateCommissionTier(organization_id=organization_id, label=tier.label.strip(), min_cumulative_sales=tier.min_cumulative_sales, rate_percent=tier.rate_percent))
    db.flush()
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="commission_tiers.updated", entity_type="estate_commission_tier", entity_id=organization_id, after_data={"tier_count": len(payload.tiers)})
    db.commit()
    return get_commission_tiers_endpoint(organization_id, request, db)


@router.get("/organizations/{organization_id}/commissions")
def commission_report(organization_id: int, request: Request, page: int | None = None, page_size: int = 25, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="payment.read")
    rows = db.query(EstateAllocation).filter(
        EstateAllocation.organization_id == organization_id,
        EstateAllocation.status == "allocated",
        EstateAllocation.sales_agent_subject_id.isnot(None),
    ).all()
    by_agent: dict[tuple, dict] = {}
    for row in rows:
        key = (row.sales_agent_subject_type, row.sales_agent_subject_id)
        bucket = by_agent.setdefault(key, {"subject_type": row.sales_agent_subject_type, "subject_id": row.sales_agent_subject_id, "sale_count": 0, "total_volume": Decimal("0"), "total_commission": Decimal("0"), "current_tier": None})
        bucket["sale_count"] += 1
        bucket["total_volume"] += Decimal(row.agreed_price or 0)
        bucket["total_commission"] += Decimal(row.commission_amount or 0)
        bucket["current_tier"] = row.commission_tier_label
    allocation_ids = [row.id for row in rows]
    paid_out_by_agent: dict[tuple, Decimal] = {}
    if allocation_ids:
        for subject_type, subject_id, total in db.query(EstateCommissionPayout.sales_agent_subject_type, EstateCommissionPayout.sales_agent_subject_id, func.sum(EstateCommissionPayout.amount)).filter(EstateCommissionPayout.allocation_id.in_(allocation_ids)).group_by(EstateCommissionPayout.sales_agent_subject_type, EstateCommissionPayout.sales_agent_subject_id).all():
            paid_out_by_agent[(subject_type, subject_id)] = Decimal(total or 0)
    results = []
    for (subject_type, subject_id), bucket in by_agent.items():
        display_name = subject_id
        if subject_type == "estate_account":
            account = db.get(EstateAccount, int(subject_id)) if subject_id.isdigit() else None
            if account:
                display_name = account.full_name
        paid_out = paid_out_by_agent.get((subject_type, subject_id), Decimal("0"))
        results.append({
            "subject_type": subject_type,
            "subject_id": subject_id,
            "display_name": display_name,
            "sale_count": bucket["sale_count"],
            "total_volume": str(bucket["total_volume"]),
            "total_commission": str(bucket["total_commission"]),
            "total_paid_out": str(paid_out),
            "total_outstanding": str(max(Decimal("0"), bucket["total_commission"] - paid_out)),
            "current_tier": bucket["current_tier"],
        })
    results.sort(key=lambda item: float(item["total_outstanding"]), reverse=True)
    if page is None:
        return {"organization_id": organization_id, "agents": results}
    safe_page = max(1, page)
    safe_page_size = min(max(page_size, 1), 100)
    start = (safe_page - 1) * safe_page_size
    return {"organization_id": organization_id, "agents": results[start:start + safe_page_size], "page": safe_page, "page_size": safe_page_size, "total": len(results)}


@router.get("/organizations/{organization_id}/sales-agents/detail")
def sales_agent_detail(organization_id: int, subject_type: str, subject_id: str, request: Request, db: Session = Depends(get_db)):
    """Everything tied to one sales agent - every plot they're attached to (reserved or
    allocated), each one's own payment status, and the commission it has actually earned (locked
    in only once a sale reaches Allocated - see commissions.py) versus still pending that."""
    require_estate_access(db, request, organization_id, permission="payment.read")
    display_name = subject_id
    role = None
    if subject_type == "estate_account":
        account = db.get(EstateAccount, int(subject_id)) if subject_id.isdigit() else None
        if account:
            display_name = account.full_name
    member = db.query(EstateOrganizationMember).filter(EstateOrganizationMember.organization_id == organization_id, EstateOrganizationMember.subject_type == subject_type, EstateOrganizationMember.subject_id == subject_id).one_or_none()
    if member:
        role = member.role_key

    rows = db.query(EstateAllocation).filter(
        EstateAllocation.organization_id == organization_id,
        EstateAllocation.sales_agent_subject_type == subject_type,
        EstateAllocation.sales_agent_subject_id == subject_id,
        EstateAllocation.status.in_(("reserved", "allocated")),
    ).order_by(EstateAllocation.allocation_date.desc()).all()

    tiers = commissions._tier_dicts(commissions.get_commission_tiers(db, organization_id))
    next_tier = commissions.resolve_tier(tiers, commissions.cumulative_sales_before(db, organization_id=organization_id, subject_type=subject_type, subject_id=subject_id))

    plots = []
    total_volume = Decimal("0")
    total_commission_earned = Decimal("0")
    total_commission_paid_out = Decimal("0")
    pending_commission_count = 0
    for row in rows:
        plot = db.get(EstatePlot, row.plot_id)
        estate = db.get(Estate, row.estate_id)
        customer = db.get(EstateCustomer, row.customer_id)
        summary = financial_summary(db, row)
        total_volume += summary.agreed_price
        payout_rows = db.query(EstateCommissionPayout).filter(EstateCommissionPayout.allocation_id == row.id).order_by(EstateCommissionPayout.payment_date.desc()).all()
        paid_out = sum((Decimal(p.amount) for p in payout_rows), Decimal("0"))
        earned = Decimal(row.commission_amount or 0) if row.status == "allocated" else Decimal("0")
        if row.status == "allocated":
            total_commission_earned += earned
            total_commission_paid_out += paid_out
        else:
            pending_commission_count += 1
        payouts_payload = []
        for payout in payout_rows:
            receipt = db.query(EstateDocument).join(EstateDocumentLink, EstateDocumentLink.document_id == EstateDocument.id).filter(EstateDocumentLink.entity_type == "commission_payout", EstateDocumentLink.entity_id == str(payout.id)).first()
            payouts_payload.append({
                "id": payout.id, "amount": str(payout.amount), "payment_date": payout.payment_date,
                "payment_method": payout.payment_method, "reference_no": payout.reference_no, "notes": payout.notes,
                "receipt_document_id": receipt.id if receipt else None, "receipt_filename": receipt.original_filename if receipt else None,
            })
        plots.append({
            "allocation_id": row.id,
            "plot_id": row.plot_id,
            "plot_number": plot.plot_number if plot else None,
            "estate_id": row.estate_id,
            "estate_name": estate.name if estate else None,
            "customer_name": customer.full_name if customer else None,
            "attribution": _allocation_attribution(db, row),
            "status": row.status,
            "agreed_price": str(summary.agreed_price),
            "confirmed_paid": str(summary.confirmed_paid),
            "outstanding": str(summary.outstanding),
            "allocation_date": row.allocation_date,
            "commission_tier_label": row.commission_tier_label,
            "commission_rate_percent": str(row.commission_rate_percent) if row.commission_rate_percent is not None else None,
            "commission_amount": str(earned) if row.status == "allocated" else None,
            "commission_paid_out": str(paid_out),
            "commission_outstanding": str(max(Decimal("0"), earned - paid_out)) if row.status == "allocated" else None,
            "payouts": payouts_payload,
        })

    return {
        "organization_id": organization_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "display_name": display_name,
        "role": role,
        "summary": {
            "plot_count": len(plots),
            "total_volume": str(total_volume),
            "total_commission_earned": str(total_commission_earned),
            "total_commission_paid_out": str(total_commission_paid_out),
            "total_commission_outstanding": str(max(Decimal("0"), total_commission_earned - total_commission_paid_out)),
            "pending_commission_count": pending_commission_count,
            "current_tier": next_tier["label"],
            "current_tier_rate_percent": str(next_tier["rate_percent"]),
        },
        "plots": plots,
    }


@router.post("/allocations/{allocation_id}/commission-payout")
def pay_commission(allocation_id: int, payload: CommissionPayoutCreate, request: Request, idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"), db: Session = Depends(get_db)):
    """Records the org actually paying an agent the commission they earned on this sale - separate
    from the commission being *earned* (which happens automatically once the sale is Allocated).
    Defaults to paying the full remaining balance when no amount is given; over-payment beyond
    what's still owed is rejected the same way an over-payment on a customer payment would be."""
    allocation = db.get(EstateAllocation, allocation_id)
    if not allocation:
        raise HTTPException(404, "Allocation not found")
    access = require_estate_access(db, request, allocation.organization_id, permission="payment.manage")
    idempotency_key = _normalize_idempotency_key(idempotency_header)
    if idempotency_key:
        existing = db.query(EstateCommissionPayout).filter(EstateCommissionPayout.organization_id == access.organization_id, EstateCommissionPayout.idempotency_key == idempotency_key).one_or_none()
        if existing:
            return {"id": existing.id, "allocation_id": existing.allocation_id, "amount": str(existing.amount), "payment_date": existing.payment_date, "payment_method": existing.payment_method, "already_processed": True}
    if allocation.status != "allocated" or not allocation.commission_amount:
        raise HTTPException(409, "This sale has no earned commission to pay out yet - it becomes payable once the plot is fully Allocated.")
    if not allocation.sales_agent_subject_type or not allocation.sales_agent_subject_id:
        raise HTTPException(409, "This sale has no sales agent tagged.")
    already_paid = Decimal(db.query(func.coalesce(func.sum(EstateCommissionPayout.amount), 0)).filter(EstateCommissionPayout.allocation_id == allocation_id).scalar() or 0)
    outstanding = Decimal(allocation.commission_amount) - already_paid
    amount = payload.amount if payload.amount is not None else outstanding
    if amount <= 0:
        raise HTTPException(409, "This agent's commission on this sale has already been paid in full.")
    if amount > outstanding:
        raise HTTPException(409, f"Amount exceeds the outstanding commission balance of {outstanding}.")
    payout = EstateCommissionPayout(
        organization_id=allocation.organization_id, allocation_id=allocation.id,
        sales_agent_subject_type=allocation.sales_agent_subject_type, sales_agent_subject_id=allocation.sales_agent_subject_id,
        amount=amount, payment_date=payload.payment_date, payment_method=payload.payment_method,
        reference_no=payload.reference_no, notes=payload.notes,
        paid_by_subject_type=access.principal.subject_type, paid_by_subject_id=access.principal.subject_id,
        idempotency_key=idempotency_key,
    )
    db.add(payout)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = db.query(EstateCommissionPayout).filter(EstateCommissionPayout.organization_id == access.organization_id, EstateCommissionPayout.idempotency_key == idempotency_key).one_or_none()
            if existing:
                return {"id": existing.id, "allocation_id": existing.allocation_id, "amount": str(existing.amount), "payment_date": existing.payment_date, "payment_method": existing.payment_method, "already_processed": True}
        raise HTTPException(status_code=409, detail="This commission payment was already submitted or conflicts with another payout")
    append_estate_audit_event(db, organization_id=allocation.organization_id, actor=access.principal, action="commission.paid", entity_type="estate_commission_payout", entity_id=payout.id, after_data={"allocation_id": allocation.id, "amount": str(amount), "sales_agent_subject_id": allocation.sales_agent_subject_id})
    db.commit()
    return {"id": payout.id, "allocation_id": allocation.id, "amount": str(amount), "payment_date": payout.payment_date, "payment_method": payout.payment_method, "already_processed": False}


def _apply_initial_payment(db: Session, *, record: EstateAllocation, payload: AllocationAction, actor, idempotency_key: str | None = None) -> EstatePayment | None:
    """An initial payment entered in the same reserve/allocate call is recorded AND immediately
    confirmed - unlike a normal payment, this represents money the org is directly attesting it
    already received, not a pending customer claim awaiting confirmation. Returns the created
    payment (or None if no initial amount was given) so the caller can attach a receipt to it."""
    if not payload.initial_payment_amount:
        return None
    payment = record_payment(
        db,
        allocation=record,
        amount=payload.initial_payment_amount,
        payment_date=datetime.now(timezone.utc),
        method=payload.initial_payment_method or "bank_transfer",
        reference=None,
        notes="Initial payment recorded at reservation/allocation",
        actor=actor,
        confirmation_required=False,
        idempotency_key=idempotency_key,
    )
    confirm_payment(db, payment=payment, actor=actor)
    # This payment is the first payment received now. The schedule already stores the next
    # instalment date, so do not advance it as if this payment were made on that future date.
    summary = financial_summary(db, record)
    if summary.agreed_price > 0 and summary.outstanding <= 0:
        record.next_payment_due_at = None
        record.payment_reminder_sent_for_due_at = None
    return payment


def _allocation_payment_fields(payload) -> tuple[str | None, datetime | None]:
    schedule = getattr(payload, "payment_schedule", None)
    if schedule:
        next_due_at = getattr(schedule, "next_due_at", None) or getattr(schedule, "first_due_at", None)
        if next_due_at is None:
            raise HTTPException(422, "Enter the next instalment due date.")
        normalized_due_at = next_due_at if next_due_at.tzinfo else next_due_at.replace(tzinfo=timezone.utc)
        if normalized_due_at <= datetime.now(timezone.utc):
            raise HTTPException(422, "The next instalment due date must be after the first payment received today.")
        return (
            json.dumps(
                {
                    "type": "installment",
                    "installment_amount": str(schedule.installment_amount),
                    "interval_months": schedule.interval_months,
                    "next_due_at": normalized_due_at.isoformat(),
                }
            ),
            normalized_due_at,
        )
    return getattr(payload, "payment_plan", None), getattr(payload, "next_payment_due_at", None)


def _notify_allocation_customer(db: Session, *, allocation: EstateAllocation, org_name: str, event: str, amount_just_paid=None) -> bool:
    customer = db.get(EstateCustomer, allocation.customer_id)
    if not customer:
        return False
    plot = db.get(EstatePlot, allocation.plot_id)
    estate = db.get(Estate, allocation.estate_id)
    if not plot or not estate:
        return False
    # Allocations created before the share-token column existed have none yet - generate one on
    # first use rather than requiring a separate backfill migration, so every customer who gets a
    # future email also gets a working "view your plot" link.
    if not allocation.share_token:
        allocation.share_token = uuid.uuid4().hex
    summary = financial_summary(db, allocation)
    portal_url = None
    if customer.email:
        _, portal_url = issue_customer_portal_link(
            db,
            customer=customer,
            actor=EstatePrincipal("system", "customer-notification", "Customer notification"),
        )
    notified = bool(customer.email) and estate_email.notify_customer(
        to_email=customer.email,
        customer_name=customer.full_name,
        org_name=org_name,
        estate_name=estate.name,
        plot_number=plot.plot_number,
        event=event,
        agreed_price=summary.agreed_price,
        confirmed_paid=summary.confirmed_paid,
        outstanding=summary.outstanding,
        amount_just_paid=amount_just_paid,
        share_token=allocation.share_token,
        portal_url=portal_url,
        payment_due_at=allocation.next_payment_due_at,
    )
    record_notification_log(
        db,
        organization_id=allocation.organization_id,
        estate_id=allocation.estate_id,
        customer_id=allocation.customer_id,
        allocation_id=allocation.id,
        event_key=event,
        recipient_email=customer.email,
        recipient_name=customer.full_name,
        subject=f"LandCheck Estate update: {event.replace('_', ' ')}",
        status="sent" if notified else ("failed" if customer.email else "skipped"),
    )
    # Notification delivery is best-effort and must not roll back the allocation/payment action.
    db.commit()
    return notified


@router.get("/public/plots/{share_token}")
def public_plot_view(share_token: str, db: Session = Depends(get_db)):
    """Powers the customer-facing "view your plot on satellite map" link sent in lifecycle emails -
    deliberately unauthenticated (customers have no LandCheck login) and deliberately minimal: only
    what's needed to show the plot on a map, nothing financial or otherwise private, since the link
    itself is the only thing gating access and could be forwarded on."""
    allocation = db.query(EstateAllocation).filter(EstateAllocation.share_token == share_token, EstateAllocation.status.in_(("reserved", "allocated"))).one_or_none()
    if not allocation:
        raise HTTPException(404, "This link is no longer valid.")
    plot = db.get(EstatePlot, allocation.plot_id)
    if not plot or not plot.geometry:
        raise HTTPException(404, "This plot's boundary is not available yet.")
    estate = db.get(Estate, allocation.estate_id)
    customer = db.get(EstateCustomer, allocation.customer_id)
    organization = db.get(EstateOrganization, allocation.organization_id)
    return {
        "customer_name": customer.full_name if customer else None,
        "plot_number": plot.plot_number,
        "area_sqm": float(plot.area_sqm) if plot.area_sqm is not None else None,
        "unit_system": estate.unit_system if estate else "m",
        "status": allocation.status,
        "estate_name": estate.name if estate else None,
        "organization_name": organization.name if organization else None,
        "geometry": mapping(to_shape(plot.geometry)),
        "boundary": mapping(to_shape(estate.boundary)) if estate and estate.boundary is not None else None,
    }


@router.post("/{estate_id}/plots/{plot_id}/reserve")
def reserve_plot(estate_id: int, plot_id: int, payload: AllocationAction, request: Request, idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"), db: Session = Depends(get_db)):
    idempotency_key = _normalize_idempotency_key(idempotency_header)
    plot = db.query(EstatePlot).filter(EstatePlot.id == plot_id, EstatePlot.estate_id == estate_id).one_or_none()
    if not plot: raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, estate_id)
    access = require_estate_access(db, request, estate.organization_id, permission="allocation.manage")
    if idempotency_key:
        existing = db.query(EstateAllocation).filter(EstateAllocation.organization_id == access.organization_id, EstateAllocation.idempotency_key == idempotency_key).one_or_none()
        if existing:
            initial_payment = db.query(EstatePayment).filter(EstatePayment.organization_id == access.organization_id, EstatePayment.idempotency_key == idempotency_key).order_by(EstatePayment.id.desc()).first()
            return {"id": existing.id, "status": existing.status, "agreed_price": str(existing.agreed_price) if existing.agreed_price is not None else None, "initial_payment_id": initial_payment.id if initial_payment else None, "customer_notified": False, "already_processed": True}
    customer = db.get(EstateCustomer, payload.customer_id)
    if not customer or customer.organization_id != access.organization_id: raise HTTPException(404, "Customer not found")
    if not payload.initial_payment_amount:
        raise HTTPException(422, "Record the customer's first payment before reserving this plot.")
    payment_plan, next_payment_due_at = _allocation_payment_fields(payload)
    try:
        record = reserve_or_allocate(db, plot=plot, customer=customer, actor=access.principal, allocate=False, expires_at=payload.expires_at, agreed_price=payload.agreed_price, payment_plan=payment_plan, next_payment_due_at=next_payment_due_at, notes=payload.notes)
        record.idempotency_key = idempotency_key
        if payload.sales_agent_subject_type and payload.sales_agent_subject_id:
            record.sales_agent_subject_type = payload.sales_agent_subject_type
            record.sales_agent_subject_id = payload.sales_agent_subject_id
        initial_payment = _apply_initial_payment(db, record=record, payload=payload, actor=access.principal, idempotency_key=idempotency_key)
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = db.query(EstateAllocation).filter(EstateAllocation.organization_id == access.organization_id, EstateAllocation.idempotency_key == idempotency_key).one_or_none()
            if existing:
                initial_payment = db.query(EstatePayment).filter(EstatePayment.organization_id == access.organization_id, EstatePayment.idempotency_key == idempotency_key).order_by(EstatePayment.id.desc()).first()
                return {"id": existing.id, "status": existing.status, "agreed_price": str(existing.agreed_price) if existing.agreed_price is not None else None, "initial_payment_id": initial_payment.id if initial_payment else None, "customer_notified": False, "already_processed": True}
        raise HTTPException(status_code=409, detail="This reservation was already submitted or the plot was reserved by another request")
    customer_notified = _notify_allocation_customer(db, allocation=record, org_name=access.organization_name, event="reserved")
    return {"id": record.id, "status": record.status, "agreed_price": str(record.agreed_price) if record.agreed_price is not None else None, "initial_payment_id": initial_payment.id if initial_payment else None, "customer_notified": customer_notified, "already_processed": False}


@router.post("/{estate_id}/plots/{plot_id}/allocate")
def allocate_plot(estate_id: int, plot_id: int, payload: AllocationAction, request: Request, idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"), db: Session = Depends(get_db)):
    """Marks a plot Allocated - the official, title-bearing status a Nigerian estate normally only
    grants once a plot is fully paid for (a signed reservation typically comes first, on a deposit
    or nothing at all; full allocation and its paperwork follow full payment). Enforced here rather
    than left to operator discretion: this call is refused unless the confirmed payments already on
    record, plus any initial payment submitted in this same call, cover the full agreed price."""
    idempotency_key = _normalize_idempotency_key(idempotency_header)
    plot = db.query(EstatePlot).filter(EstatePlot.id == plot_id, EstatePlot.estate_id == estate_id).one_or_none()
    if not plot: raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, estate_id)
    access = require_estate_access(db, request, estate.organization_id, permission="allocation.manage")
    if idempotency_key:
        existing = db.query(EstateAllocation).filter(EstateAllocation.organization_id == access.organization_id, EstateAllocation.idempotency_key == idempotency_key).one_or_none()
        if existing:
            initial_payment = db.query(EstatePayment).filter(EstatePayment.organization_id == access.organization_id, EstatePayment.idempotency_key == idempotency_key).order_by(EstatePayment.id.desc()).first()
            return {"id": existing.id, "status": existing.status, "agreed_price": str(existing.agreed_price) if existing.agreed_price is not None else None, "initial_payment_id": initial_payment.id if initial_payment else None, "customer_notified": False, "already_processed": True}
    customer = db.get(EstateCustomer, payload.customer_id)
    if not customer or customer.organization_id != access.organization_id: raise HTTPException(404, "Customer not found")
    existing = db.query(EstateAllocation).filter(EstateAllocation.plot_id == plot.id, EstateAllocation.status.in_(("reserved", "allocated"))).one_or_none()
    effective_agreed_price = payload.agreed_price if payload.agreed_price is not None else (existing.agreed_price if existing else None)
    already_confirmed = financial_summary(db, existing).confirmed_paid if existing else Decimal("0")
    incoming = payload.initial_payment_amount or Decimal("0")
    if not existing and not incoming:
        raise HTTPException(422, "Record the customer's first payment before allocating this plot.")
    if effective_agreed_price and (already_confirmed + incoming) < effective_agreed_price:
        raise HTTPException(
            409,
            f"This plot cannot be marked Allocated until it is fully paid. Confirmed so far: "
            f"{estate_email.format_naira(already_confirmed + incoming)} of {estate_email.format_naira(effective_agreed_price)} agreed. "
            f"Reserve it instead, or record the remaining payment first.",
        )
    payment_plan, next_payment_due_at = _allocation_payment_fields(payload)
    try:
        record = reserve_or_allocate(db, plot=plot, customer=customer, actor=access.principal, allocate=True, agreed_price=payload.agreed_price, payment_plan=payment_plan, next_payment_due_at=next_payment_due_at, notes=payload.notes)
        record.idempotency_key = idempotency_key
        if payload.sales_agent_subject_type and payload.sales_agent_subject_id:
            record.sales_agent_subject_type = payload.sales_agent_subject_type
            record.sales_agent_subject_id = payload.sales_agent_subject_id
        initial_payment = _apply_initial_payment(db, record=record, payload=payload, actor=access.principal, idempotency_key=idempotency_key)
        commissions.apply_commission(db, allocation=record)
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = db.query(EstateAllocation).filter(EstateAllocation.organization_id == access.organization_id, EstateAllocation.idempotency_key == idempotency_key).one_or_none()
            if existing:
                initial_payment = db.query(EstatePayment).filter(EstatePayment.organization_id == access.organization_id, EstatePayment.idempotency_key == idempotency_key).order_by(EstatePayment.id.desc()).first()
                return {"id": existing.id, "status": existing.status, "agreed_price": str(existing.agreed_price) if existing.agreed_price is not None else None, "initial_payment_id": initial_payment.id if initial_payment else None, "customer_notified": False, "already_processed": True}
        raise HTTPException(status_code=409, detail="This allocation was already submitted or the plot was allocated by another request")
    customer_notified = _notify_allocation_customer(db, allocation=record, org_name=access.organization_name, event="allocated")
    return {"id": record.id, "status": record.status, "agreed_price": str(record.agreed_price) if record.agreed_price is not None else None, "initial_payment_id": initial_payment.id if initial_payment else None, "customer_notified": customer_notified, "already_processed": False}


@router.post("/allocations/{allocation_id}/release")
def release_plot(allocation_id: int, request: Request, db: Session = Depends(get_db)):
    record = db.get(EstateAllocation, allocation_id)
    if not record: raise HTTPException(404, "Allocation not found")
    access = require_estate_access(db, request, record.organization_id, permission="allocation.manage")
    release_allocation(db, allocation=record, actor=access.principal, reason="Released by authorized user"); db.commit(); return {"id": record.id, "status": record.status}

@router.post("/allocations/{allocation_id}/payments")
def add_payment(allocation_id: int, payload: PaymentCreate, request: Request, idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"), db: Session = Depends(get_db)):
    idempotency_key = _normalize_idempotency_key(idempotency_header)
    allocation=db.get(EstateAllocation, allocation_id)
    if not allocation: raise HTTPException(404,"Allocation not found")
    access=require_estate_access(db,request,allocation.organization_id,permission="payment.manage")
    if idempotency_key:
        existing = db.query(EstatePayment).filter(EstatePayment.organization_id == access.organization_id, EstatePayment.idempotency_key == idempotency_key).one_or_none()
        if existing:
            return {"id": existing.id, "status": existing.status, "customer_notified": False, "already_processed": True}
    try:
        payment=record_payment(db,allocation=allocation,amount=payload.amount,payment_date=payload.payment_date,method=payload.payment_method,reference=payload.reference_no,notes=payload.notes,actor=access.principal,idempotency_key=idempotency_key)
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = db.query(EstatePayment).filter(EstatePayment.organization_id == access.organization_id, EstatePayment.idempotency_key == idempotency_key).one_or_none()
            if existing:
                return {"id": existing.id, "status": existing.status, "customer_notified": False, "already_processed": True}
        raise HTTPException(status_code=409, detail="This payment was already submitted or conflicts with an existing payment reference")
    customer_notified = _notify_allocation_customer(db, allocation=allocation, org_name=access.organization_name, event="payment_recorded", amount_just_paid=payload.amount)
    return {"id":payment.id,"status":payment.status,"customer_notified":customer_notified,"already_processed":False}

@router.post("/payments/{payment_id}/confirm")
def confirm(payment_id:int, request:Request, db:Session=Depends(get_db)):
    payment=db.get(EstatePayment,payment_id)
    if not payment: raise HTTPException(404,"Payment not found")
    access=require_estate_access(db,request,payment.organization_id,permission="payment.manage")
    confirm_payment(db,payment=payment,actor=access.principal)
    allocation = db.get(EstateAllocation, payment.allocation_id)
    if allocation:
        advance_payment_schedule_after_payment(db, allocation=allocation, amount=payment.amount)
    db.commit()
    customer_notified = False
    if allocation:
        summary = financial_summary(db, allocation)
        fully_paid = summary.agreed_price > 0 and summary.outstanding <= 0
        # Mirrors how a Nigerian estate normally works: a reservation is held on a deposit (or
        # nothing), and full Allocation - the official, title-bearing status - follows automatically
        # once the price is fully paid, rather than requiring staff to remember to flip it by hand.
        if fully_paid and allocation.status == "reserved":
            plot = db.get(EstatePlot, allocation.plot_id)
            customer = db.get(EstateCustomer, allocation.customer_id)
            if plot and customer:
                reserve_or_allocate(db, plot=plot, customer=customer, actor=access.principal, allocate=True)
                commissions.apply_commission(db, allocation=allocation)
                db.commit()
        customer_notified = _notify_allocation_customer(db, allocation=allocation, org_name=access.organization_name, event="payment_completed" if fully_paid else "payment_recorded", amount_just_paid=payment.amount)
    return {"id":payment.id,"status":payment.status,"customer_notified":customer_notified}

@router.post("/payments/{payment_id}/void")
def void(payment_id:int,payload:VoidAction,request:Request,db:Session=Depends(get_db)):
    payment=db.get(EstatePayment,payment_id)
    if not payment: raise HTTPException(404,"Payment not found")
    access=require_estate_access(db,request,payment.organization_id,permission="payment.manage"); void_payment(db,payment=payment,actor=access.principal,reason=payload.reason); db.commit(); return {"id":payment.id,"status":payment.status}

@router.get("/allocations/{allocation_id}/financial-summary")
def allocation_summary(allocation_id:int,request:Request,db:Session=Depends(get_db)):
    allocation=db.get(EstateAllocation,allocation_id)
    if not allocation: raise HTTPException(404,"Allocation not found")
    require_estate_access(db,request,allocation.organization_id,permission="payment.read"); result=financial_summary(db,allocation)
    return {"agreed_price":str(result.agreed_price),"confirmed_paid":str(result.confirmed_paid),"pending_paid":str(result.pending_paid),"outstanding":str(result.outstanding),"percentage":str(result.percentage),"fully_paid":result.outstanding==0 and result.agreed_price>0}

@router.post("/payments/{payment_id}/evidence")
async def upload_payment_evidence(payment_id:int, request:Request, file:UploadFile=File(...), db:Session=Depends(get_db)):
    payment=db.get(EstatePayment,payment_id)
    if not payment: raise HTTPException(404,"Payment not found")
    access=require_estate_access(db,request,payment.organization_id,permission="document.manage")
    organization=db.get(EstateOrganization,payment.organization_id)
    stored=store_private_estate_file(organization_uid=organization.organization_uid,category="payments",entity_uid=payment.payment_uid,filename=file.filename or "receipt",content_type=file.content_type or "",data=await file.read())
    document=EstateDocument(organization_id=payment.organization_id,object_key=stored.object_key,original_filename=stored.filename,mime_type=stored.mime_type,size_bytes=stored.size_bytes,checksum=stored.checksum,document_type="receipt",uploaded_by_subject_type=access.principal.subject_type,uploaded_by_subject_id=access.principal.subject_id)
    db.add(document); db.flush(); db.add(EstateDocumentLink(document_id=document.id,entity_type="payment",entity_id=str(payment.id))); append_estate_audit_event(db,organization_id=payment.organization_id,actor=access.principal,action="payment.evidence_uploaded",entity_type="estate_document",entity_id=document.id,after_data={"payment_id":payment.id,"filename":stored.filename}); db.commit()
    return {"id":document.id,"filename":document.original_filename}

@router.get("/payments/{payment_id}/evidence/{document_id}/download")
def download_payment_evidence(payment_id:int,document_id:int,request:Request,db:Session=Depends(get_db)):
    payment=db.get(EstatePayment,payment_id); document=db.get(EstateDocument,document_id)
    if not payment or not document or document.organization_id != payment.organization_id or not db.query(EstateDocumentLink).filter(EstateDocumentLink.document_id==document_id,EstateDocumentLink.entity_type=="payment",EstateDocumentLink.entity_id==str(payment_id)).first(): raise HTTPException(404,"Evidence not found")
    access=require_estate_access(db,request,payment.organization_id,permission="document.read"); data,mime=read_private_estate_file(document.object_key); append_estate_audit_event(db,organization_id=payment.organization_id,actor=access.principal,action="payment.evidence_downloaded",entity_type="estate_document",entity_id=document.id); db.commit()
    return Response(data,media_type=mime,headers={"Content-Disposition":f'inline; filename="{document.original_filename}"'})

def _linked_entity_organization(db: Session, entity_type: str, entity_id: int) -> int | None:
    model = {"estate": Estate, "plot": EstatePlot, "customer": EstateCustomer, "allocation": EstateAllocation, "payment": EstatePayment, "staking_task": EstateStakingTask, "field_inspection": EstateFieldInspection, "import_review": EstateImportReview, "commission_payout": EstateCommissionPayout}.get(entity_type)
    row = db.get(model, entity_id) if model else None
    return getattr(row, "organization_id", None)

@router.get("/document-types")
def estate_document_types(request: Request, db: Session = Depends(get_db)):
    principal = resolve_estate_principal(db, request)
    if not any(has_permission(item.role_key, "document.read") for item in list_estate_access(db, principal)):
        raise HTTPException(403, "Document access is required")
    return list(ESTATE_DOCUMENT_TYPES)

@router.post("/documents")
async def upload_document(entity_type: str, entity_id: int, document_type: str, request: Request, file: UploadFile = File(...), description: str | None = None, db: Session = Depends(get_db)):
    organization_id = _linked_entity_organization(db, entity_type, entity_id)
    if not organization_id: raise HTTPException(404, "Linked Estate entity was not found")
    access = require_estate_access(db, request, organization_id, permission="document.manage")
    normalized_document_type = document_type.strip().lower()
    if normalized_document_type not in ESTATE_DOCUMENT_TYPE_CODES:
        raise HTTPException(422, "Choose a supported Estate document type")
    organization = db.get(EstateOrganization, organization_id)
    stored = store_private_estate_file(organization_uid=organization.organization_uid, category="documents", entity_uid=f"{entity_type}_{entity_id}", filename=file.filename or "document", content_type=file.content_type or "", data=await file.read())
    document = EstateDocument(organization_id=organization_id, object_key=stored.object_key, original_filename=stored.filename, mime_type=stored.mime_type, size_bytes=stored.size_bytes, checksum=stored.checksum, document_type=normalized_document_type, description=description, uploaded_by_subject_type=access.principal.subject_type, uploaded_by_subject_id=access.principal.subject_id)
    db.add(document); db.flush(); db.add(EstateDocumentLink(document_id=document.id, entity_type=entity_type, entity_id=str(entity_id))); append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="document.uploaded", entity_type="estate_document", entity_id=document.id, after_data={"linked_entity":entity_type,"linked_id":entity_id}); db.commit()
    return {"id":document.id,"filename":document.original_filename}

@router.get("/documents/{document_id}/download")
def download_document(document_id:int, request:Request, db:Session=Depends(get_db)):
    document=db.get(EstateDocument,document_id)
    if not document: raise HTTPException(404,"Document not found")
    access=require_estate_access(db,request,document.organization_id,permission="document.read"); data,mime=read_private_estate_file(document.object_key); append_estate_audit_event(db,organization_id=document.organization_id,actor=access.principal,action="document.downloaded",entity_type="estate_document",entity_id=document.id); db.commit()
    return Response(data,media_type=mime,headers={"Content-Disposition":f'inline; filename="{document.original_filename}"'})

@router.get("/customers/{customer_id}/statement")
def customer_statement(customer_id:int, request:Request, estate_id:int|None=None, allocation_id:int|None=None, db:Session=Depends(get_db)):
    customer=db.get(EstateCustomer,customer_id)
    if not customer: raise HTTPException(404,"Customer not found")
    access=require_estate_access(db,request,customer.organization_id,permission="payment.read")
    allocations=db.query(EstateAllocation).filter(EstateAllocation.customer_id==customer_id, EstateAllocation.organization_id==customer.organization_id)
    if estate_id: allocations=allocations.filter(EstateAllocation.estate_id==estate_id)
    if allocation_id: allocations=allocations.filter(EstateAllocation.id==allocation_id)
    allocations=allocations.all()
    rows=[]
    for allocation in allocations:
        summary=financial_summary(db,allocation); plot=db.get(EstatePlot,allocation.plot_id); estate=db.get(Estate,allocation.estate_id)
        payments=db.query(EstatePayment).filter(EstatePayment.allocation_id==allocation.id).order_by(EstatePayment.payment_date).all()
        allocation_documents = {document.id: document for document in linked_documents(db, allocation)}
        survey = db.query(EstateSurveyRequest).filter(EstateSurveyRequest.allocation_id == allocation.id).order_by(EstateSurveyRequest.created_at.desc()).first()
        staking = db.query(EstateStakingTask).filter(EstateStakingTask.plot_id == allocation.plot_id).order_by(EstateStakingTask.created_at.desc()).first()
        evidence_by_payment: dict[str, list[dict]] = {}
        payment_ids=[str(p.id) for p in payments]
        if payment_ids:
            evidence_rows=db.query(EstateDocument,EstateDocumentLink.entity_id).join(EstateDocumentLink,EstateDocumentLink.document_id==EstateDocument.id).filter(EstateDocumentLink.entity_type=="payment",EstateDocumentLink.entity_id.in_(payment_ids)).all()
            for document,entity_id in evidence_rows:
                evidence_by_payment.setdefault(entity_id,[]).append({"id":document.id,"filename":document.original_filename})
        rows.append({"allocation_id":allocation.id,"allocation_date":allocation.allocation_date,"status":allocation.status,"estate":estate.name if estate else None,"plot":plot.plot_number if plot else None,"plot_area_sqm":float(plot.area_sqm or 0) if plot else None,"plot_address":plot.public_address if plot else None,"payment_plan":allocation.payment_plan,"agreed_price":str(summary.agreed_price),"confirmed_paid":str(summary.confirmed_paid),"pending_paid":str(summary.pending_paid),"outstanding":str(summary.outstanding),"survey_status":survey.status if survey else "not_started","staking_status":staking.status if staking else "not_started","documents":[{"id":document.id,"filename":document.original_filename,"type":document.document_type,"description":document.description} for document in allocation_documents.values()],"transactions":[{"id":p.id,"date":p.payment_date,"reference":p.reference_no,"method":p.payment_method,"amount":str(p.amount),"status":p.status,"receipt_number":p.receipt_number,"receipts":evidence_by_payment.get(str(p.id),[])} for p in payments]})
    organization=db.get(EstateOrganization,customer.organization_id)
    return {"statement_date":__import__("datetime").datetime.utcnow().isoformat()+"Z","organization":{"id":customer.organization_id,"name":organization.name if organization else access.organization_name},"customer":{"id":customer.id,"name":customer.full_name,"reference":customer.reference_no},"allocations":rows}


@router.get("/customers/{customer_id}/statement.pdf")
def customer_statement_pdf(customer_id: int, request: Request, estate_id: int | None = None, allocation_id: int | None = None, db: Session = Depends(get_db)):
    """Generate a clean customer packet instead of printing the dashboard modal."""
    data = customer_statement(customer_id, request, estate_id, allocation_id, db)
    document_assets = []
    seen_document_ids: set[int] = set()
    for allocation_data in data.get("allocations", []):
        document_rows = list(allocation_data.get("documents") or [])
        for transaction in allocation_data.get("transactions", []):
            document_rows.extend({**receipt, "type": "receipt"} for receipt in transaction.get("receipts") or [])
        for document_row in document_rows:
            document_id = int(document_row.get("id") or 0)
            if not document_id or document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            document = db.get(EstateDocument, document_id)
            if not document:
                continue
            try:
                document_data, document_mime = read_private_estate_file(document.object_key)
            except Exception:
                document_data, document_mime = b"", document.mime_type
            document_assets.append({
                "allocation_id": allocation_data.get("allocation_id"),
                "document_type": document.document_type or document_row.get("type") or "document",
                "filename": document.original_filename,
                "description": document.description,
                "mime_type": document_mime or document.mime_type,
                "data": document_data,
            })
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_path = tmp_file.name
        render_customer_statement_pdf(
            organization_name=data["organization"]["name"],
            customer_name=data["customer"]["name"],
            customer_reference=data["customer"]["reference"],
            allocations=data["allocations"],
            output_path=tmp_path,
            document_assets=document_assets,
        )
        with open(tmp_path, "rb") as handle:
            pdf_bytes = handle.read()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", data["customer"]["name"] or "customer").strip("-.") or f"customer-{customer_id}"
    return Response(pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{safe_name}_Customer_Packet.pdf"'})

@router.get("/allocations/{allocation_id}/financial-detail")
def allocation_financial_detail(allocation_id:int,request:Request,db:Session=Depends(get_db)):
    allocation=db.get(EstateAllocation,allocation_id)
    if not allocation: raise HTTPException(404,"Allocation not found")
    require_estate_access(db,request,allocation.organization_id,permission="payment.read")
    summary=financial_summary(db,allocation); customer=db.get(EstateCustomer,allocation.customer_id); plot=db.get(EstatePlot,allocation.plot_id); estate=db.get(Estate,allocation.estate_id)
    payments=db.query(EstatePayment).filter(EstatePayment.allocation_id==allocation.id).order_by(EstatePayment.payment_date.desc()).all()
    return {"allocation":{"id":allocation.id,"status":allocation.status,"allocation_date":allocation.allocation_date,"payment_plan":allocation.payment_plan},"customer":{"id":customer.id,"name":customer.full_name,"reference":customer.reference_no,"phone":customer.phone,"email":customer.email},"estate":{"id":estate.id,"name":estate.name},"plot":{"id":plot.id,"number":plot.plot_number},"financial":{"agreed_price":str(summary.agreed_price),"confirmed_paid":str(summary.confirmed_paid),"pending_paid":str(summary.pending_paid),"outstanding":str(summary.outstanding),"percentage":str(summary.percentage),"fully_paid":summary.agreed_price>0 and summary.outstanding==0},"payments":[{"id":p.id,"date":p.payment_date,"amount":str(p.amount),"status":p.status,"method":p.payment_method,"reference":p.reference_no} for p in payments]}

@router.get("/allocations/{allocation_id}/record")
def allocation_record(allocation_id: int, request: Request, db: Session = Depends(get_db)):
    allocation = db.get(EstateAllocation, allocation_id)
    if not allocation:
        raise HTTPException(404, "Allocation not found")
    require_estate_access(db, request, allocation.organization_id, permission="payment.read")
    customer = db.get(EstateCustomer, allocation.customer_id)
    estate = db.get(Estate, allocation.estate_id)
    plot = db.get(EstatePlot, allocation.plot_id)
    summary = financial_summary(db, allocation)
    payments = db.query(EstatePayment).filter(EstatePayment.allocation_id == allocation.id).order_by(EstatePayment.payment_date.desc()).all()
    payment_ids = [str(payment.id) for payment in payments]
    evidence_by_payment: dict[str, list[dict]] = {}
    if payment_ids:
        evidence_rows = db.query(EstateDocument, EstateDocumentLink.entity_id).join(
            EstateDocumentLink, EstateDocumentLink.document_id == EstateDocument.id,
        ).filter(
            EstateDocument.organization_id == allocation.organization_id,
            EstateDocumentLink.entity_type == "payment",
            EstateDocumentLink.entity_id.in_(payment_ids),
        ).all()
        for document, payment_id in evidence_rows:
            evidence_by_payment.setdefault(payment_id, []).append({"id": document.id, "filename": document.original_filename, "type": document.document_type})
    documents = {document.id: document for document in linked_documents(db, allocation)}
    survey = db.query(EstateSurveyRequest).filter(EstateSurveyRequest.allocation_id == allocation.id).order_by(EstateSurveyRequest.created_at.desc()).first()
    staking = db.query(EstateStakingTask).filter(EstateStakingTask.plot_id == allocation.plot_id).order_by(EstateStakingTask.created_at.desc()).first()
    return {
        "allocation": {
            "id": allocation.id,
            "status": allocation.status,
            "reservation_date": allocation.reservation_date,
            "reservation_expires_at": allocation.reservation_expires_at,
            "allocation_date": allocation.allocation_date,
            "next_payment_due_at": allocation.next_payment_due_at,
            "agreed_price": str(allocation.agreed_price or 0),
            "payment_plan": allocation.payment_plan,
            "notes": allocation.notes,
        },
        "customer": {"id": customer.id, "name": customer.full_name, "reference": customer.reference_no, "phone": customer.phone, "email": customer.email} if customer else None,
        "estate": {"id": estate.id, "name": estate.name, "state": estate.state, "locality": estate.locality} if estate else None,
        "plot": {"id": plot.id, "number": plot.plot_number, "area_sqm": float(plot.area_sqm or 0), "public_address": plot.public_address, "land_use": plot.land_use, "commercial_status": plot.commercial_status, "development_status": plot.development_status} if plot else None,
        "financial": {"agreed_price": str(summary.agreed_price), "confirmed_paid": str(summary.confirmed_paid), "pending_paid": str(summary.pending_paid), "outstanding": str(summary.outstanding), "percentage": str(summary.percentage), "fully_paid": summary.agreed_price > 0 and summary.outstanding == 0},
        "documents": [{"id": document.id, "filename": document.original_filename, "type": document.document_type, "description": document.description, "mime_type": document.mime_type, "size": document.size_bytes, "uploaded_at": document.created_at} for document in documents.values()],
        "document_readiness": document_readiness(db, allocation),
        "survey": {"status": survey.status, "reference": survey.survey_reference, "completed_at": survey.materialized_at} if survey else {"status": "not_started"},
        "staking": {"status": staking.status, "completed_at": staking.completed_at} if staking else {"status": "not_started"},
        "payments": [{"id": payment.id, "date": payment.payment_date, "amount": str(payment.amount), "status": payment.status, "method": payment.payment_method, "reference": payment.reference_no, "receipt_number": payment.receipt_number, "confirmed_at": payment.confirmed_at, "evidence": evidence_by_payment.get(str(payment.id), [])} for payment in payments],
    }

@router.get("/customers/{customer_id}/financial-detail")
def customer_financial_detail(customer_id:int,request:Request,db:Session=Depends(get_db)):
    customer=db.get(EstateCustomer,customer_id)
    if not customer: raise HTTPException(404,"Customer not found")
    require_estate_access(db,request,customer.organization_id,permission="payment.read")
    allocations=db.query(EstateAllocation).filter(EstateAllocation.customer_id==customer_id, EstateAllocation.organization_id==customer.organization_id).all(); rows=[]
    for allocation in allocations:
        summary=financial_summary(db,allocation); estate=db.get(Estate,allocation.estate_id); plot=db.get(EstatePlot,allocation.plot_id)
        rows.append({"allocation_id":allocation.id,"allocation_date":allocation.allocation_date,"payment_plan":allocation.payment_plan,"estate":{"id":estate.id,"name":estate.name},"plot":{"id":plot.id,"number":plot.plot_number},"agreed_price":str(summary.agreed_price),"confirmed":str(summary.confirmed_paid),"pending":str(summary.pending_paid),"outstanding":str(summary.outstanding),"percentage":str(summary.percentage),"payment_count":db.query(EstatePayment).filter(EstatePayment.allocation_id==allocation.id).count()})
    return {"customer":{"id":customer.id,"name":customer.full_name,"reference":customer.reference_no,"phone":customer.phone,"email":customer.email},"allocations":rows}

@router.get("/{estate_id}/financial-summary")
def estate_financial_summary(estate_id:int,request:Request,db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    require_estate_access(db,request,estate.organization_id,permission="payment.read")
    allocations=db.query(EstateAllocation).filter(EstateAllocation.estate_id==estate_id).all(); summaries=_bulk_financial_summaries(db, allocations).values()
    return {"estate":{"id":estate.id,"name":estate.name},"contracted_sales":str(sum((s["agreed_price"] for s in summaries),Decimal(0))),"confirmed_collections":str(sum((s["confirmed_paid"] for s in summaries),Decimal(0))),"pending_collections":str(sum((s["pending_paid"] for s in summaries),Decimal(0))),"outstanding_balance":str(sum((s["outstanding"] for s in summaries),Decimal(0))),"fully_paid_allocations":sum(1 for s in summaries if s["agreed_price"]>0 and s["outstanding"]==0),"allocations_with_outstanding":sum(1 for s in summaries if s["outstanding"]>0)}

@router.get("/financial-summary")
def organization_financial_summary(request:Request, db:Session=Depends(get_db)):
    principal=resolve_estate_principal(db,request); access=list_estate_access(db,principal); result=[]
    for item in access:
        allocations=db.query(EstateAllocation).filter(EstateAllocation.organization_id==item.organization_id).all()
        summaries=[financial_summary(db,a) for a in allocations]
        result.append({"organization_id":item.organization_id,"contracted_sales_value":str(sum((s.agreed_price for s in summaries),0)),"confirmed_collections":str(sum((s.confirmed_paid for s in summaries),0)),"pending_collections":str(sum((s.pending_paid for s in summaries),0)),"outstanding_balance":str(sum((s.outstanding for s in summaries),0)),"fully_paid_allocations":sum(1 for s in summaries if s.agreed_price>0 and s.outstanding==0)})
    return result

@router.get("/payments")
def list_payments(request: Request, page: int = 1, page_size: int = 25, search: str | None = None, status: str | None = None, estate_id: int | None = None, customer_id: int | None = None, method: str | None = None, date_from: str | None = None, date_to: str | None = None, db: Session = Depends(get_db)):
    principal=resolve_estate_principal(db,request); memberships={a.organization_id:a for a in list_estate_access(db,principal)}; allowed=set(memberships)
    query=db.query(EstatePayment,EstateCustomer,Estate,EstatePlot).join(EstateCustomer,EstateCustomer.id==EstatePayment.customer_id).join(EstatePlot,EstatePlot.id==EstatePayment.plot_id).join(Estate,Estate.id==EstatePlot.estate_id).filter(EstatePayment.organization_id.in_(allowed))
    if estate_id: query=query.filter(Estate.id==estate_id)
    if customer_id: query=query.filter(EstateCustomer.id==customer_id)
    if status: query=query.filter(EstatePayment.status==status)
    if method: query=query.filter(EstatePayment.payment_method==method)
    if date_from: query=query.filter(EstatePayment.payment_date>=date_from)
    if date_to: query=query.filter(EstatePayment.payment_date<=date_to)
    if search:
        term=f"%{search.strip()}%"; query=query.filter((EstateCustomer.full_name.ilike(term)) | (EstatePlot.plot_number.ilike(term)) | (EstatePayment.reference_no.ilike(term)))
    total=query.count(); rows=query.order_by(EstatePayment.payment_date.desc()).offset(max(page-1,0)*min(max(page_size,1),100)).limit(min(max(page_size,1),100)).all()
    return {"page":page,"page_size":min(max(page_size,1),100),"total":total,"items":[{"id":p.id,"date":p.payment_date,"amount":str(p.amount),"currency":p.currency,"status":p.status,"method":p.payment_method,"reference":p.reference_no,"receipt_number":p.receipt_number,"customer":{"id":c.id,"name":c.full_name},"estate":{"id":e.id,"name":e.name},"plot":{"id":plot.id,"number":plot.plot_number},"allocation_id":p.allocation_id,"recorded_by":p.recorded_by_subject_id,"confirmed_by":p.confirmed_by_subject_id,"can_confirm":has_permission(memberships[p.organization_id].role_key,"payment.manage") and p.status in {"recorded","pending_confirmation"},"can_void":has_permission(memberships[p.organization_id].role_key,"payment.manage") and p.status not in {"voided","reversed"},"can_view_receipt":has_permission(memberships[p.organization_id].role_key,"document.read")} for p,c,e,plot in rows]}

@router.post("/payment-inbox", status_code=201)
def create_payment_inbox(payload: PaymentInboxCreate, request: Request, db: Session = Depends(get_db)):
    if payload.estate_id is not None:
        estate = db.get(Estate, payload.estate_id)
        if not estate:
            raise HTTPException(404, "Estate not found")
        access = require_estate_access(db, request, estate.organization_id, permission="payment.manage")
        organization_id = estate.organization_id
    else:
        principal = resolve_estate_principal(db, request)
        access = next((item for item in list_estate_access(db, principal) if has_permission(item.role_key, "payment.manage")), None)
        if not access:
            raise HTTPException(403, "Payment management access is required")
        organization_id = access.organization_id
    row = EstatePaymentInbox(organization_id=organization_id, estate_id=payload.estate_id, amount=payload.amount, payment_date=payload.payment_date, payer_name=payload.payer_name, payer_reference=payload.payer_reference, source=payload.source.strip().lower() or "manual", raw_payload=payload.raw_payload)
    db.add(row); db.flush(); append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="payment.inbox.created", entity_type="estate_payment_inbox", entity_id=row.id, after_data={"amount": str(row.amount), "source": row.source}); db.commit()
    return {"id": row.id, "status": row.status, "amount": str(row.amount), "payment_date": row.payment_date, "payer_name": row.payer_name, "payer_reference": row.payer_reference}


@router.get("/payment-inbox")
def list_payment_inbox(request: Request, status: str = "unmatched", estate_id: int | None = None, db: Session = Depends(get_db)):
    principal = resolve_estate_principal(db, request)
    allowed = {item.organization_id for item in list_estate_access(db, principal) if has_permission(item.role_key, "payment.read")}
    query = db.query(EstatePaymentInbox).filter(EstatePaymentInbox.organization_id.in_(allowed))
    if status != "all":
        query = query.filter(EstatePaymentInbox.status == status)
    if estate_id is not None:
        query = query.filter(EstatePaymentInbox.estate_id == estate_id)
    rows = query.order_by(EstatePaymentInbox.payment_date.desc()).limit(200).all()
    return [{"id": row.id, "estate_id": row.estate_id, "amount": str(row.amount), "currency": row.currency, "payment_date": row.payment_date, "payer_name": row.payer_name, "payer_reference": row.payer_reference, "source": row.source, "status": row.status, "matched_payment_id": row.matched_payment_id} for row in rows]


@router.post("/payment-inbox/{inbox_id}/match")
def match_payment_inbox(inbox_id: int, payload: PaymentInboxMatch, request: Request, db: Session = Depends(get_db)):
    inbox = db.get(EstatePaymentInbox, inbox_id)
    if not inbox:
        raise HTTPException(404, "Payment inbox item not found")
    access = require_estate_access(db, request, inbox.organization_id, permission="payment.manage")
    if inbox.status != "unmatched":
        raise HTTPException(409, "This payment inbox item has already been resolved")
    allocation = db.get(EstateAllocation, payload.allocation_id)
    if not allocation or allocation.organization_id != inbox.organization_id:
        raise HTTPException(404, "Allocation not found")
    payment = record_payment(db, allocation=allocation, amount=Decimal(str(inbox.amount)), payment_date=inbox.payment_date, method=payload.payment_method, reference=payload.reference_no or inbox.payer_reference, notes=payload.notes or f"Matched from {inbox.source} payment inbox", actor=access.principal)
    inbox.status = "matched"; inbox.matched_payment_id = payment.id; inbox.matched_at = datetime.now(timezone.utc); inbox.matched_by_subject_type = access.principal.subject_type; inbox.matched_by_subject_id = access.principal.subject_id; inbox.match_notes = payload.notes
    append_estate_audit_event(db, organization_id=inbox.organization_id, actor=access.principal, action="payment.inbox.matched", entity_type="estate_payment_inbox", entity_id=inbox.id, after_data={"payment_id": payment.id, "allocation_id": allocation.id})
    db.commit()
    return {"id": inbox.id, "status": inbox.status, "payment_id": payment.id, "payment_status": payment.status, "receipt_number": payment.receipt_number}


@router.post("/payment-inbox/{inbox_id}/ignore")
def ignore_payment_inbox(inbox_id: int, request: Request, db: Session = Depends(get_db)):
    inbox = db.get(EstatePaymentInbox, inbox_id)
    if not inbox:
        raise HTTPException(404, "Payment inbox item not found")
    access = require_estate_access(db, request, inbox.organization_id, permission="payment.manage")
    if inbox.status != "unmatched":
        raise HTTPException(409, "This payment inbox item has already been resolved")
    inbox.status = "ignored"; inbox.matched_at = datetime.now(timezone.utc); inbox.matched_by_subject_type = access.principal.subject_type; inbox.matched_by_subject_id = access.principal.subject_id
    append_estate_audit_event(db, organization_id=inbox.organization_id, actor=access.principal, action="payment.inbox.ignored", entity_type="estate_payment_inbox", entity_id=inbox.id); db.commit()
    return {"id": inbox.id, "status": inbox.status}


@router.get("/payments/{payment_id}/receipt.pdf")
def payment_receipt_pdf(payment_id: int, request: Request, db: Session = Depends(get_db)):
    payment = db.get(EstatePayment, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    access = require_estate_access(db, request, payment.organization_id, permission="document.read")
    allocation = db.get(EstateAllocation, payment.allocation_id); customer = db.get(EstateCustomer, payment.customer_id); plot = db.get(EstatePlot, payment.plot_id); estate = db.get(Estate, allocation.estate_id) if allocation else None
    buffer = io.BytesIO(); pdf = canvas.Canvas(buffer, pagesize=A4); width, height = A4
    pdf.setTitle(payment.receipt_number or f"Payment receipt {payment.id}"); pdf.setFont("Helvetica-Bold", 19); pdf.drawString(42, height - 65, "LandCheck Estate payment receipt")
    pdf.setFont("Helvetica", 11); y = height - 105
    for line in [f"Receipt: {payment.receipt_number or 'Pending'}", f"Customer: {customer.full_name if customer else '-'}", f"Estate: {estate.name if estate else '-'}", f"Plot: {plot.plot_number if plot else '-'}", f"Amount: {payment.currency} {Decimal(str(payment.amount)):,.2f}", f"Payment date: {payment.payment_date:%d %b %Y}", f"Method: {payment.payment_method.replace('_', ' ').title()}", f"Reference: {payment.reference_no or '-'}", f"Status: {payment.status.replace('_', ' ').title()}"]:
        pdf.drawString(48, y, line); y -= 20
    pdf.setFont("Helvetica-Oblique", 9); pdf.drawString(48, y - 16, "Keep this receipt for your Estate records."); pdf.save()
    append_estate_audit_event(db, organization_id=payment.organization_id, actor=access.principal, action="payment.receipt_downloaded", entity_type="estate_payment", entity_id=payment.id); db.commit()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", payment.receipt_number or f"payment-{payment.id}").strip("-.")
    return Response(buffer.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{safe}.pdf"'})


@router.get("/payments/{payment_id}")
def payment_detail(payment_id:int,request:Request,db:Session=Depends(get_db)):
    payment=db.get(EstatePayment,payment_id)
    if not payment: raise HTTPException(404,"Payment not found")
    access=require_estate_access(db,request,payment.organization_id,permission="payment.read")
    allocation=db.get(EstateAllocation,payment.allocation_id); customer=db.get(EstateCustomer,payment.customer_id); plot=db.get(EstatePlot,payment.plot_id); estate=db.get(Estate,allocation.estate_id); summary=financial_summary(db,allocation)
    evidence=db.query(EstateDocument).join(EstateDocumentLink,EstateDocumentLink.document_id==EstateDocument.id).filter(EstateDocumentLink.entity_type=="payment",EstateDocumentLink.entity_id==str(payment_id)).all()
    return {"payment":{"id":payment.id,"amount":str(payment.amount),"currency":payment.currency,"date":payment.payment_date,"method":payment.payment_method,"reference":payment.reference_no,"receipt_number":payment.receipt_number,"notes":payment.notes,"status":payment.status,"recorded_by":payment.recorded_by_subject_id,"confirmed_by":payment.confirmed_by_subject_id,"confirmed_at":payment.confirmed_at,"void_reason":payment.void_reason},"customer":{"id":customer.id,"name":customer.full_name},"estate":{"id":estate.id,"name":estate.name},"plot":{"id":plot.id,"number":plot.plot_number},"allocation_id":allocation.id,"financial":{"agreed_price":str(summary.agreed_price),"confirmed":str(summary.confirmed_paid),"pending":str(summary.pending_paid),"outstanding":str(summary.outstanding)},"capabilities":{"can_confirm":has_permission(access.role_key,"payment.manage") and payment.status in {"recorded","pending_confirmation"},"can_void":has_permission(access.role_key,"payment.manage") and payment.status not in {"voided","reversed"},"can_view_receipt":has_permission(access.role_key,"document.read")},"evidence":[{"id":d.id,"filename":d.original_filename,"mime_type":d.mime_type} for d in evidence]}

@router.get("/documents")
def list_documents(request:Request,page:int=1,page_size:int=25,document_type:str|None=None,entity_type:str|None=None,entity_id:int|None=None,db:Session=Depends(get_db)):
    principal=resolve_estate_principal(db,request); allowed={a.organization_id for a in list_estate_access(db,principal)}
    query=db.query(EstateDocument,EstateDocumentLink).join(EstateDocumentLink,EstateDocumentLink.document_id==EstateDocument.id).filter(EstateDocument.organization_id.in_(allowed))
    if document_type: query=query.filter(EstateDocument.document_type==document_type.lower())
    if entity_type: query=query.filter(EstateDocumentLink.entity_type==entity_type)
    if entity_id is not None: query=query.filter(EstateDocumentLink.entity_id==str(entity_id))
    total=query.count(); rows=query.order_by(EstateDocument.created_at.desc()).offset(max(page-1,0)*min(max(page_size,1),100)).limit(min(max(page_size,1),100)).all()
    return {"page":page,"page_size":min(max(page_size,1),100),"total":total,"items":[{"id":d.id,"filename":d.original_filename,"type":d.document_type,"description":d.description,"entity_type":link.entity_type,"entity_id":link.entity_id,"mime_type":d.mime_type,"size":d.size_bytes,"uploaded_at":d.created_at} for d,link in rows]}

@router.get("/selectors")
def estate_selectors(request:Request, estate_id:int|None=None, customer_id:int|None=None, plot_id:int|None=None, status:str|None=None, db:Session=Depends(get_db)):
    principal=resolve_estate_principal(db,request); access=list_estate_access(db,principal); allowed={a.organization_id for a in access}
    estates=db.query(Estate).filter(Estate.organization_id.in_(allowed),Estate.archived_at.is_(None)).all()
    customers=db.query(EstateCustomer).filter(EstateCustomer.organization_id.in_(allowed)).all()
    plots=db.query(EstatePlot,Estate).join(Estate,Estate.id==EstatePlot.estate_id).filter(Estate.organization_id.in_(allowed))
    if estate_id: plots=plots.filter(EstatePlot.estate_id==estate_id)
    plots=plots.all()
    allocations=db.query(EstateAllocation,Estate,EstatePlot,EstateCustomer).join(Estate,Estate.id==EstateAllocation.estate_id).join(EstatePlot,EstatePlot.id==EstateAllocation.plot_id).join(EstateCustomer,EstateCustomer.id==EstateAllocation.customer_id).filter(EstateAllocation.organization_id.in_(allowed))
    if estate_id: allocations=allocations.filter(EstateAllocation.estate_id==estate_id)
    if customer_id: allocations=allocations.filter(EstateAllocation.customer_id==customer_id)
    if plot_id: allocations=allocations.filter(EstateAllocation.plot_id==plot_id)
    if status: allocations=allocations.filter(EstateAllocation.status==status)
    allocation_rows = allocations.all()
    financials = _bulk_financial_summaries(db, [allocation for allocation, _, _, _ in allocation_rows])
    allocation_items=[]
    for allocation,estate,plot,customer in allocation_rows:
        summary=financials[allocation.id]; allocation_items.append({"id":allocation.id,"estate_id":estate.id,"estate_name":estate.name,"plot_id":plot.id,"plot_number":plot.plot_number,"customer_id":customer.id,"customer_name":customer.full_name,"status":allocation.status,"allocation_date":allocation.allocation_date,"payment_plan":allocation.payment_plan,"lead_source_code":allocation.lead_source_code,"lead_source_channel":allocation.lead_source_channel,"attribution":_allocation_attribution(db, allocation),"agreed_price":str(summary["agreed_price"]),"currency":"NGN","confirmed":str(summary["confirmed_paid"]),"pending":str(summary["pending_paid"]),"outstanding":str(summary["outstanding"])})
    return {"estates":[{"id":e.id,"name":e.name} for e in estates],"customers":[{"id":c.id,"name":c.full_name,"reference":c.reference_no} for c in customers],"plots":[{"id":p.id,"plot_number":p.plot_number,"estate_id":e.id,"estate_name":e.name,"commercial_status":p.commercial_status,"development_status":p.development_status,"geometry_status":p.geometry_status,"block_id":p.block_id,"area_sqm":float(p.area_sqm) if p.area_sqm is not None else 0.0} for p,e in plots],"allocations":allocation_items}


@router.get("/{estate_id}/operations-summary")
def estate_operations_summary(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="estate.read")
    return build_operations_summary(db, estate)


@router.get("/{estate_id}/document-readiness")
def estate_document_readiness(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="document.read")
    allocations = db.query(EstateAllocation).filter(EstateAllocation.estate_id == estate_id, EstateAllocation.status.in_(("reserved", "allocated"))).order_by(EstateAllocation.created_at.desc()).all()
    result = []
    for allocation in allocations:
        customer = db.get(EstateCustomer, allocation.customer_id)
        plot = db.get(EstatePlot, allocation.plot_id)
        result.append({"allocation_id": allocation.id, "customer": customer.full_name if customer else None, "plot": plot.plot_number if plot else None, **document_readiness(db, allocation)})
    return {"requirements": list(DOCUMENT_REQUIREMENTS), "items": result}


@router.get("/{estate_id}/notifications")
def estate_notification_log(estate_id: int, request: Request, limit: int = 100, status: str | None = None, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="estate.read")
    safe_limit = max(1, min(limit, 250))
    base_query = db.query(EstateNotificationLog).filter(
        EstateNotificationLog.estate_id == estate_id,
        EstateNotificationLog.organization_id == estate.organization_id,
    )
    if status in {"sent", "failed", "skipped"}:
        base_query = base_query.filter(EstateNotificationLog.status == status)
    rows = base_query.order_by(EstateNotificationLog.created_at.desc()).limit(safe_limit).all()
    def status_count(value: str) -> int:
        return int(
            db.query(func.count(EstateNotificationLog.id)).filter(
                EstateNotificationLog.estate_id == estate_id,
                EstateNotificationLog.organization_id == estate.organization_id,
                EstateNotificationLog.status == value,
            ).scalar() or 0
        )
    return {
        "items": [
            {
                "id": row.id,
                "event_key": row.event_key,
                "channel": row.channel,
                "recipient_email": row.recipient_email,
                "recipient_name": row.recipient_name,
                "subject": row.subject,
                "status": row.status,
                "error_message": row.error_message,
                "details": row.details or {},
                "sent_at": row.sent_at,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        "counts": {
            "sent": status_count("sent"),
            "failed": status_count("failed"),
            "skipped": status_count("skipped"),
        },
    }


@router.post("/customers/{customer_id}/portal-token")
def create_customer_portal_token(customer_id: int, payload: PortalTokenCreate, request: Request, db: Session = Depends(get_db)):
    customer = db.get(EstateCustomer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    access = require_estate_access(db, request, customer.organization_id, permission="customer.manage")
    row, raw_token = issue_customer_portal_token(db, customer=customer, actor=access.principal, expires_in_days=payload.expires_in_days)
    db.commit()
    portal_url = customer_portal_url(raw_token)
    organization = db.get(EstateOrganization, customer.organization_id)
    customer_allocation = db.query(EstateAllocation).filter(
        EstateAllocation.customer_id == customer.id,
        EstateAllocation.status.in_(("reserved", "allocated")),
    ).order_by(EstateAllocation.created_at.desc()).first()
    email_sent = estate_email.send_customer_portal_link(
        to_email=customer.email,
        customer_name=customer.full_name,
        org_name=organization.name if organization else "Estate team",
        portal_url=portal_url,
    )
    record_notification_log(
        db,
        organization_id=customer.organization_id,
        estate_id=customer_allocation.estate_id if customer_allocation else None,
        customer_id=customer.id,
        allocation_id=customer_allocation.id if customer_allocation else None,
        event_key="customer_portal_issued",
        recipient_email=customer.email,
        recipient_name=customer.full_name,
        subject=f"Your buyer portal - {organization.name if organization else 'Estate team'}",
        status="sent" if email_sent else ("failed" if customer.email else "skipped"),
    )
    db.commit()
    return {"token": raw_token, "expires_at": row.expires_at, "url": portal_url, "email_sent": email_sent}


@router.get("/buyer/{token}")
def buyer_portal(token: str, db: Session = Depends(get_db)):
    row = db.query(EstateCustomerPortalToken).filter(EstateCustomerPortalToken.token_hash == hash_portal_token(token)).one_or_none()
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at if row and row.expires_at.tzinfo else (row.expires_at.replace(tzinfo=timezone.utc) if row else None)
    if not row or row.revoked_at is not None or not expires_at or expires_at <= now:
        raise HTTPException(404, "This buyer portal link is invalid or expired")
    payload = buyer_portal_payload(db, row)
    db.commit()
    return {**payload, "expires_at": expires_at}


@router.get("/buyer/{token}/packet/{allocation_id}.pdf")
def buyer_packet_pdf(token: str, allocation_id: int, db: Session = Depends(get_db)):
    row = db.query(EstateCustomerPortalToken).filter(EstateCustomerPortalToken.token_hash == hash_portal_token(token)).one_or_none()
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at if row and row.expires_at.tzinfo else (row.expires_at.replace(tzinfo=timezone.utc) if row else None)
    if not row or row.revoked_at is not None or not expires_at or expires_at <= now:
        raise HTTPException(404, "This buyer portal link is invalid or expired")
    allocation = db.get(EstateAllocation, allocation_id)
    if not allocation or allocation.customer_id != row.customer_id or allocation.organization_id != row.organization_id:
        raise HTTPException(404, "Allocation not found")
    customer = db.get(EstateCustomer, allocation.customer_id)
    estate = db.get(Estate, allocation.estate_id)
    plot = db.get(EstatePlot, allocation.plot_id)
    summary = financial_summary(db, allocation)
    payments = db.query(EstatePayment).filter(EstatePayment.allocation_id == allocation.id).order_by(EstatePayment.payment_date.asc()).all()
    readiness = document_readiness(db, allocation)
    documents = [document for document in linked_documents(db, allocation) if document.document_type in PUBLIC_DOCUMENT_TYPES]
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50
    pdf.setTitle(f"Buyer packet - Plot {plot.plot_number if plot else allocation.plot_id}")
    pdf.setFont("Helvetica-Bold", 18); pdf.drawString(42, y, estate.name if estate else "LandCheck Estate"); y -= 28
    pdf.setFont("Helvetica", 11); pdf.drawString(42, y, f"Buyer packet for {customer.full_name if customer else 'Customer'}"); y -= 28
    pdf.setFont("Helvetica-Bold", 12); pdf.drawString(42, y, f"Plot {plot.plot_number if plot else '-'}"); y -= 20
    if plot and plot.geometry:
        outline = list(to_shape(plot.geometry).exterior.coords)
        if len(outline) > 2:
            min_x = min(point[0] for point in outline); max_x = max(point[0] for point in outline); min_y = min(point[1] for point in outline); max_y = max(point[1] for point in outline)
            scale = min(145 / max(max_x - min_x, 0.000001), 110 / max(max_y - min_y, 0.000001))
            points = [(350 + (point[0] - min_x) * scale, height - 205 + (point[1] - min_y) * scale) for point in outline]
            pdf.setStrokeColorRGB(0.05, 0.45, 0.25); pdf.setFillColorRGB(0.86, 0.96, 0.89); pdf.setLineWidth(1.5)
            path = pdf.beginPath(); path.moveTo(*points[0])
            for point in points[1:]: path.lineTo(*point)
            path.close(); pdf.drawPath(path, fill=1, stroke=1); pdf.setFillColorRGB(0, 0, 0)
            pdf.setFont("Helvetica", 8); pdf.drawString(350, height - 220, "Plot boundary (not to scale)")
    pdf.setFont("Helvetica", 10)
    for line in [
        f"Status: {allocation.status.title()}",
        f"Area: {float(plot.area_sqm or 0):,.2f} m2" if plot else "Area: Not recorded",
        f"Agreed price: NGN {summary.agreed_price:,.2f}",
        f"Confirmed paid: NGN {summary.confirmed_paid:,.2f}",
        f"Outstanding: NGN {summary.outstanding:,.2f}",
        f"Payment progress: {summary.percentage:.1f}%",
    ]:
        pdf.drawString(42, y, line); y -= 17
    y -= 12; pdf.setFont("Helvetica-Bold", 12); pdf.drawString(42, y, "Document readiness"); y -= 20; pdf.setFont("Helvetica", 10)
    for item in readiness["stages"]:
        pdf.drawString(52, y, f"{'Ready' if item['status'] == 'ready' else 'Missing'} - {item['label']}"); y -= 16
    y -= 10; pdf.setFont("Helvetica-Bold", 12); pdf.drawString(42, y, "Published documents"); y -= 20; pdf.setFont("Helvetica", 10)
    if documents:
        for document in documents:
            pdf.drawString(52, y, f"{document.document_type.replace('_', ' ').title()} - {document.original_filename}"); y -= 16
            if y < 60:
                pdf.showPage(); y = height - 50; pdf.setFont("Helvetica", 10)
    else:
        pdf.drawString(52, y, "No published documents linked yet."); y -= 16
    y -= 10; pdf.setFont("Helvetica-Bold", 12); pdf.drawString(42, y, "Payment receipts"); y -= 20; pdf.setFont("Helvetica", 10)
    if payments:
        for payment in payments:
            pdf.drawString(52, y, f"{payment.payment_date:%d %b %Y} | NGN {Decimal(str(payment.amount)):,.2f} | {payment.status.title()} | {payment.receipt_number or 'Receipt pending'}"); y -= 16
            if y < 60:
                pdf.showPage(); y = height - 50; pdf.setFont("Helvetica", 10)
    else:
        pdf.drawString(52, y, "No payments recorded yet."); y -= 16
    y -= 18; pdf.setFont("Helvetica-Oblique", 9); pdf.drawString(42, y, "This packet shows the buyer-safe record available through the LandCheck portal.")
    pdf.save()
    db.commit()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"plot-{plot.plot_number if plot else allocation_id}").strip("-.") or f"plot-{allocation_id}"
    return Response(buffer.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{safe}_Buyer_Packet.pdf"'})


@router.get("/buyer/{token}/documents/{document_id}/download")
def buyer_document_download(token: str, document_id: int, db: Session = Depends(get_db)):
    row = db.query(EstateCustomerPortalToken).filter(EstateCustomerPortalToken.token_hash == hash_portal_token(token)).one_or_none()
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at if row and row.expires_at.tzinfo else (row.expires_at.replace(tzinfo=timezone.utc) if row else None)
    if not row or row.revoked_at is not None or not expires_at or expires_at <= now:
        raise HTTPException(404, "This buyer portal link is invalid or expired")
    document = db.get(EstateDocument, document_id)
    if not document or document.organization_id != row.organization_id or document.document_type not in PUBLIC_DOCUMENT_TYPES:
        raise HTTPException(404, "Document not found")
    customer_allocation_ids = {str(allocation.id) for allocation in db.query(EstateAllocation).filter(EstateAllocation.customer_id == row.customer_id, EstateAllocation.organization_id == row.organization_id).all()}
    customer_plot_ids = {str(allocation.plot_id) for allocation in db.query(EstateAllocation).filter(EstateAllocation.customer_id == row.customer_id, EstateAllocation.organization_id == row.organization_id).all()}
    allowed = db.query(EstateDocumentLink).filter(EstateDocumentLink.document_id == document.id, or_(
        (EstateDocumentLink.entity_type == "customer") & (EstateDocumentLink.entity_id == str(row.customer_id)),
        (EstateDocumentLink.entity_type == "allocation") & EstateDocumentLink.entity_id.in_(customer_allocation_ids or {"-1"}),
        (EstateDocumentLink.entity_type == "plot") & EstateDocumentLink.entity_id.in_(customer_plot_ids or {"-1"}),
    )).first()
    if not allowed:
        raise HTTPException(404, "Document not found")
    data, mime = read_private_estate_file(document.object_key)
    return Response(data, media_type=mime, headers={"Content-Disposition": f'inline; filename="{document.original_filename}"'})


def _agent_portal_context(db: Session, token: str):
    row = db.query(EstateAgentPortalToken).filter(EstateAgentPortalToken.token_hash == hash_portal_token(token)).one_or_none()
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at.replace(tzinfo=timezone.utc) if row and row.expires_at.tzinfo is None else (row.expires_at if row else None)
    if not row or row.revoked_at is not None or not expires_at or expires_at <= now:
        raise HTTPException(404, "This agent workspace link is invalid or expired")
    member = db.get(EstateOrganizationMember, row.member_id)
    organization = db.get(EstateOrganization, row.organization_id)
    if not member or not member.is_active or not organization or organization.status != "active":
        raise HTTPException(404, "This agent workspace is no longer available")
    if member.role_key not in {"sales", "marketer"}:
        raise HTTPException(403, "This link is not assigned to an agent workspace")
    row.last_used_at = now
    db.flush()
    return row, member, organization


def _agent_workspace_payload(db: Session, *, member: EstateOrganizationMember, organization: EstateOrganization) -> dict:
    subject_type = member.subject_type
    subject_id = member.subject_id
    estates = db.query(Estate).filter(Estate.organization_id == organization.id, Estate.status != "archived").order_by(Estate.name.asc()).all()
    all_leads = []
    all_sales = []
    estate_payloads = []
    total_outstanding = Decimal("0")
    total_commission = Decimal("0")
    total_paid_commission = Decimal("0")

    for estate in estates:
        leads = db.query(EstatePublicReservationRequest).filter(
            EstatePublicReservationRequest.estate_id == estate.id,
            EstatePublicReservationRequest.assigned_agent_subject_type == subject_type,
            EstatePublicReservationRequest.assigned_agent_subject_id == subject_id,
        ).order_by(EstatePublicReservationRequest.created_at.desc()).limit(200).all()
        allocations = db.query(EstateAllocation).filter(
            EstateAllocation.estate_id == estate.id,
            EstateAllocation.sales_agent_subject_type == subject_type,
            EstateAllocation.sales_agent_subject_id == subject_id,
            EstateAllocation.status.in_(("reserved", "allocated")),
        ).order_by(EstateAllocation.created_at.desc()).all()
        campaigns = db.query(EstateQrCampaign).filter(
            EstateQrCampaign.estate_id == estate.id,
            EstateQrCampaign.assigned_agent_subject_type == subject_type,
            EstateQrCampaign.assigned_agent_subject_id == subject_id,
            EstateQrCampaign.is_active.is_(True),
        ).order_by(EstateQrCampaign.created_at.desc()).all()
        allocation_plot_ids = {allocation.plot_id for allocation in allocations}
        lead_plot_ids = {lead.plot_id for lead in leads if lead.status in {"new", "contacted", "converted"}}
        plots = []
        for plot in db.query(EstatePlot).filter(EstatePlot.estate_id == estate.id).order_by(EstatePlot.plot_number_normalized.asc()).all():
            plots.append({
                "id": plot.id,
                "plot_number": plot.plot_number,
                "status": plot.commercial_status,
                "agent_record": plot.id in allocation_plot_ids or plot.id in lead_plot_ids,
                "agent_lead": plot.id in lead_plot_ids,
                "agent_sale": plot.id in allocation_plot_ids,
                "geometry": mapping(to_shape(plot.geometry)) if plot.geometry else None,
            })

        estate_leads = []
        for lead in leads:
            plot = db.get(EstatePlot, lead.plot_id)
            estate_leads.append({"id": lead.id, "name": lead.full_name, "phone": lead.phone, "email": lead.email, "status": lead.status, "plot": plot.plot_number if plot else None, "source_code": lead.source_code, "source_channel": lead.source_channel, "created_at": lead.created_at})
        estate_sales = []
        for allocation in allocations:
            customer = db.get(EstateCustomer, allocation.customer_id)
            plot = db.get(EstatePlot, allocation.plot_id)
            summary = financial_summary(db, allocation)
            payout_total = Decimal(str(db.query(func.coalesce(func.sum(EstateCommissionPayout.amount), 0)).filter(EstateCommissionPayout.allocation_id == allocation.id).scalar() or 0))
            commission = Decimal(str(allocation.commission_amount or 0))
            payments = db.query(EstatePayment).filter(
                EstatePayment.allocation_id == allocation.id,
                EstatePayment.status.in_(("confirmed", "recorded", "pending_confirmation")),
            ).order_by(EstatePayment.payment_date.desc()).limit(50).all()
            total_outstanding += summary.outstanding
            total_commission += commission
            total_paid_commission += payout_total
            estate_sales.append({
                "allocation_id": allocation.id,
                "customer": customer.full_name if customer else None,
                "plot": plot.plot_number if plot else None,
                "status": allocation.status,
                "agreed_price": str(summary.agreed_price),
                "confirmed_paid": str(summary.confirmed_paid),
                "outstanding": str(summary.outstanding),
                "paid": summary.outstanding <= 0,
                "commission_due": str(max(Decimal("0"), commission - payout_total)),
                "payments": [{"amount": str(payment.amount), "date": payment.payment_date, "receipt_number": payment.receipt_number, "status": payment.status} for payment in payments],
            })
        estate_payloads.append({
            "id": estate.id,
            "name": estate.name,
            "public_slug": estate.public_slug,
            "public_url": f"{str(os.getenv('LANDCHECK_WEB_URL') or 'https://landcheck.online').rstrip('/')}/estates/public/{estate.public_slug}" if estate.public_slug else None,
            "plots": plots,
            "leads": estate_leads,
            "sales": estate_sales,
            "campaigns": [_qr_campaign_payload(campaign, estate) for campaign in campaigns],
        })
        all_leads.extend([{**lead, "estate_name": estate.name} for lead in estate_leads])
        all_sales.extend([{**sale, "estate_name": estate.name} for sale in estate_sales])

    return {
        "agent": {"subject_type": subject_type, "subject_id": subject_id, "name": _agent_display_name(db, organization_id=organization.id, subject_type=subject_type, subject_id=subject_id), "role": member.role_key, "email": member.contact_email, "phone": member.contact_phone},
        "summary": {"lead_count": len(all_leads), "sale_count": len(all_sales), "paid_sale_count": sum(1 for sale in all_sales if sale["paid"]), "outstanding": str(total_outstanding), "commission_earned": str(total_commission), "commission_paid": str(total_paid_commission), "commission_due": str(max(Decimal("0"), total_commission - total_paid_commission))},
        "leads": all_leads,
        "sales": all_sales,
        "estates": estate_payloads,
    }


@router.get("/agent-portal/{token}")
def public_agent_workspace(token: str, db: Session = Depends(get_db)):
    row, member, organization = _agent_portal_context(db, token)
    payload = _agent_workspace_payload(db, member=member, organization=organization)
    db.commit()
    return {"organization": {"id": organization.id, "name": organization.name, "slug": organization.slug}, **payload}


@router.post("/agent-portal/{token}/qr-campaigns", status_code=201)
def public_agent_create_qr_campaign(token: str, estate_id: int, payload: QrCampaignCreate, db: Session = Depends(get_db)):
    _row, member, organization = _agent_portal_context(db, token)
    estate = db.get(Estate, estate_id)
    if not estate or estate.organization_id != organization.id:
        raise HTTPException(404, "Estate not found")
    base = re.sub(r"[^a-z0-9]+", "-", payload.name.strip().lower()).strip("-")[:80] or "campaign"
    code = base
    if db.query(EstateQrCampaign.id).filter(EstateQrCampaign.estate_id == estate_id, EstateQrCampaign.code == code).first():
        code = f"{base}-{uuid.uuid4().hex[:6]}"
    row = EstateQrCampaign(
        organization_id=organization.id,
        estate_id=estate.id,
        code=code,
        name=payload.name.strip(),
        channel=payload.channel.strip().lower() or "agent",
        assigned_agent_subject_type=member.subject_type,
        assigned_agent_subject_id=member.subject_id,
        created_by_subject_type=member.subject_type,
        created_by_subject_id=member.subject_id,
    )
    db.add(row)
    db.flush()
    db.commit()
    return _qr_campaign_payload(row, estate)


@router.get("/agent-portal/{token}/qr-campaigns/{campaign_id}/print.pdf")
def public_agent_print_qr_campaign(token: str, campaign_id: int, db: Session = Depends(get_db)):
    _row, member, organization = _agent_portal_context(db, token)
    row = db.get(EstateQrCampaign, campaign_id)
    if not row or row.organization_id != organization.id or row.assigned_agent_subject_type != member.subject_type or row.assigned_agent_subject_id != member.subject_id:
        raise HTTPException(404, "QR campaign not found")
    estate = db.get(Estate, row.estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    result = _render_qr_campaign_pdf(row, estate)
    db.commit()
    return result


@router.get("/agent/workspace")
def agent_workspace(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="payment.read")
    is_sales_agent = access.role_key in {"sales", "marketer"}
    principal = access.principal
    lead_query = db.query(EstatePublicReservationRequest).filter(EstatePublicReservationRequest.estate_id == estate_id)
    allocation_query = db.query(EstateAllocation).filter(EstateAllocation.estate_id == estate_id, EstateAllocation.status.in_(("reserved", "allocated")))
    campaign_query = db.query(EstateQrCampaign).filter(EstateQrCampaign.estate_id == estate_id, EstateQrCampaign.is_active.is_(True))
    if is_sales_agent:
        lead_query = lead_query.filter(EstatePublicReservationRequest.assigned_agent_subject_type == principal.subject_type, EstatePublicReservationRequest.assigned_agent_subject_id == principal.subject_id)
        allocation_query = allocation_query.filter(EstateAllocation.sales_agent_subject_type == principal.subject_type, EstateAllocation.sales_agent_subject_id == principal.subject_id)
        campaign_query = campaign_query.filter(EstateQrCampaign.assigned_agent_subject_type == principal.subject_type, EstateQrCampaign.assigned_agent_subject_id == principal.subject_id)
    leads = []
    for lead in lead_query.order_by(EstatePublicReservationRequest.created_at.desc()).limit(100).all():
        plot = db.get(EstatePlot, lead.plot_id)
        leads.append({"id": lead.id, "name": lead.full_name, "phone": lead.phone, "email": lead.email, "status": lead.status, "source_code": lead.source_code, "source_channel": lead.source_channel, "plot": plot.plot_number if plot else None, "created_at": lead.created_at})
    sales = []
    total_outstanding = Decimal("0")
    total_commission = Decimal("0")
    total_paid_commission = Decimal("0")
    for allocation in allocation_query.order_by(EstateAllocation.created_at.desc()).all():
        customer = db.get(EstateCustomer, allocation.customer_id); plot = db.get(EstatePlot, allocation.plot_id); summary = financial_summary(db, allocation)
        payout_total = Decimal(str(db.query(func.coalesce(func.sum(EstateCommissionPayout.amount), 0)).filter(EstateCommissionPayout.allocation_id == allocation.id).scalar() or 0))
        commission = Decimal(str(allocation.commission_amount or 0))
        total_outstanding += summary.outstanding; total_commission += commission; total_paid_commission += payout_total
        sales.append({"allocation_id": allocation.id, "customer": customer.full_name if customer else None, "plot": plot.plot_number if plot else None, "status": allocation.status, "agreed_price": str(summary.agreed_price), "confirmed_paid": str(summary.confirmed_paid), "outstanding": str(summary.outstanding), "paid": summary.outstanding <= 0, "commission": str(commission), "commission_paid": str(payout_total), "commission_due": str(max(Decimal("0"), commission - payout_total))})
    campaigns = [{"id": row.id, "code": row.code, "name": row.name, "channel": row.channel, "scan_count": row.scan_count, "last_scanned_at": row.last_scanned_at, "assigned_agent_subject_id": row.assigned_agent_subject_id, "public_url": f"{str(os.getenv('LANDCHECK_WEB_URL') or 'https://landcheck.online').rstrip('/')}/estates/public/{estate.public_slug}?source={row.code}"} for row in campaign_query.order_by(EstateQrCampaign.created_at.desc()).all()]
    return {"estate": {"id": estate.id, "name": estate.name, "public_slug": estate.public_slug}, "agent": {"subject_type": principal.subject_type, "subject_id": principal.subject_id, "name": principal.display_name, "role": access.role_key}, "summary": {"lead_count": len(leads), "sale_count": len(sales), "outstanding": str(total_outstanding), "commission_earned": str(total_commission), "commission_paid": str(total_paid_commission), "commission_due": str(max(Decimal("0"), total_commission - total_paid_commission))}, "leads": leads, "sales": sales, "campaigns": campaigns}


def _qr_campaign_payload(row: EstateQrCampaign, estate: Estate) -> dict:
    web_url = str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").rstrip("/")
    return {"id": row.id, "estate_id": row.estate_id, "code": row.code, "name": row.name, "channel": row.channel, "scan_count": row.scan_count, "last_scanned_at": row.last_scanned_at, "assigned_agent_subject_type": row.assigned_agent_subject_type, "assigned_agent_subject_id": row.assigned_agent_subject_id, "public_url": f"{web_url}/estates/public/{estate.public_slug}?source={row.code}"}


@router.get("/{estate_id}/qr-campaigns")
def list_qr_campaigns(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="estate.read")
    return [_qr_campaign_payload(row, estate) for row in db.query(EstateQrCampaign).filter(EstateQrCampaign.estate_id == estate_id).order_by(EstateQrCampaign.created_at.desc()).all()]


@router.post("/{estate_id}/qr-campaigns", status_code=201)
def create_qr_campaign(estate_id: int, payload: QrCampaignCreate, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="estate.read")
    can_assign = has_permission(access.role_key, "estate.manage")
    assigned_type = payload.assigned_agent_subject_type if can_assign else access.principal.subject_type
    assigned_id = payload.assigned_agent_subject_id if can_assign else access.principal.subject_id
    base = re.sub(r"[^a-z0-9]+", "-", payload.name.strip().lower()).strip("-")[:80] or "campaign"
    code = base
    if db.query(EstateQrCampaign.id).filter(EstateQrCampaign.estate_id == estate_id, EstateQrCampaign.code == code).first():
        code = f"{base}-{uuid.uuid4().hex[:6]}"
    row = EstateQrCampaign(organization_id=estate.organization_id, estate_id=estate.id, code=code, name=payload.name.strip(), channel=payload.channel.strip().lower() or "other", assigned_agent_subject_type=assigned_type, assigned_agent_subject_id=assigned_id, created_by_subject_type=access.principal.subject_type, created_by_subject_id=access.principal.subject_id)
    db.add(row); db.flush(); append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="public_qr_campaign.created", entity_type="estate_qr_campaign", entity_id=row.id, after_data={"code": row.code, "name": row.name}); db.commit()
    return _qr_campaign_payload(row, estate)


def _render_qr_campaign_pdf(row: EstateQrCampaign, estate: Estate) -> Response:
    web_url = str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").rstrip("/")
    public_url = f"{web_url}/estates/public/{estate.public_slug}?source={row.code}"
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    pdf.setTitle(f"LandCheck QR - {row.name}")
    pdf.setFillColorRGB(0.06, 0.25, 0.15)
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawCentredString(width / 2, height - 64, "Want verified land with documents?")
    pdf.setFillColorRGB(0.12, 0.16, 0.14)
    pdf.setFont("Helvetica", 11)
    pdf.drawCentredString(width / 2, height - 87, "Scan this code to view the live estate map and make an enquiry.")

    # ReportLab's QR widget uses barWidth/barHeight as the complete drawing size. The previous
    # value of 4 rendered a technically valid but invisible-looking mark in the exported PDF.
    qr_widget = qr.QrCodeWidget(public_url)
    qr_widget.barWidth = 220
    qr_widget.barHeight = 220
    qr_widget.x = 10
    qr_widget.y = 10
    drawing = Drawing(240, 240)
    drawing.add(qr_widget)
    renderPDF.draw(drawing, pdf, (width - 240) / 2, height - 365)

    pdf.setFillColorRGB(0.06, 0.25, 0.15)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawCentredString(width / 2, height - 400, estate.name)
    pdf.setFillColorRGB(0.25, 0.30, 0.27)
    pdf.setFont("Helvetica", 9)
    pdf.drawCentredString(width / 2, height - 420, f"Campaign: {row.name} | {row.channel}")
    pdf.drawCentredString(width / 2, height - 438, public_url)
    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawCentredString(width / 2, 52, "Powered by LandCheck Estates")
    pdf.save()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", row.name).strip("-.") or "estate-qr"
    return Response(buffer.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{safe}_QR_Tag.pdf"'})


@router.get("/qr-campaigns/{campaign_id}/print.pdf")
def print_qr_campaign(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(EstateQrCampaign, campaign_id)
    if not row:
        raise HTTPException(404, "QR campaign not found")
    estate = db.get(Estate, row.estate_id)
    access = require_estate_access(db, request, row.organization_id, permission="estate.read")
    result = _render_qr_campaign_pdf(row, estate)
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="public_qr_campaign.printed", entity_type="estate_qr_campaign", entity_id=row.id)
    db.commit()
    return result


@router.get("/{estate_id}")
def estate_detail(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="estate.read")
    return {
        "id": estate.id,
        "uid": estate.estate_uid,
        "organization_id": estate.organization_id,
        "name": estate.name,
        "status": estate.status,
        "description": estate.description,
        "state": estate.state,
        "locality": estate.locality,
        "location_text": estate.location_text,
        "crs": estate.crs,
        "datum": estate.datum,
        "unit_system": estate.unit_system,
        "approximate_area_sqm": str(estate.approximate_area_sqm) if estate.approximate_area_sqm is not None else None,
        "project_reference": estate.project_reference,
        "project_owner": estate.project_owner,
        "ownership_details": estate.ownership_details,
        "boundary": mapping(to_shape(estate.boundary)) if estate.boundary else None,
    }


@router.get("/organizations/{organization_id}/members")
def list_organization_members(organization_id: int, request: Request, page: int | None = None, page_size: int = 25, search: str | None = None, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="estate.manage")
    query = db.query(EstateOrganizationMember).filter(EstateOrganizationMember.organization_id == organization_id)
    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query = query.filter(or_(func.lower(EstateOrganizationMember.subject_id).like(term), func.lower(EstateOrganizationMember.contact_email).like(term), func.lower(EstateOrganizationMember.contact_phone).like(term)))
    ordered = query.order_by(EstateOrganizationMember.created_at.asc(), EstateOrganizationMember.id.asc())
    if page is None:
        rows = ordered.all()
        return [{"id": row.id, "subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key, "email": row.contact_email, "phone": row.contact_phone, "is_active": row.is_active} for row in rows]
    safe_page = max(1, page)
    safe_page_size = min(max(page_size, 1), 100)
    total = ordered.count()
    rows = ordered.offset((safe_page - 1) * safe_page_size).limit(safe_page_size).all()
    return {"items": [{"id": row.id, "subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key, "email": row.contact_email, "phone": row.contact_phone, "is_active": row.is_active} for row in rows], "page": safe_page, "page_size": safe_page_size, "total": total}


@router.post("/organizations/{organization_id}/members")
def add_organization_member(organization_id: int, payload: MemberCreate, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="estate.manage")
    existing = db.query(EstateOrganizationMember).filter(EstateOrganizationMember.organization_id == organization_id, EstateOrganizationMember.subject_type == payload.subject_type.strip(), EstateOrganizationMember.subject_id == payload.subject_id.strip()).one_or_none()
    if existing:
        raise HTTPException(409, "This identity is already an organization member")
    row = EstateOrganizationMember(
        organization_id=organization_id,
        subject_type=payload.subject_type.strip().lower(),
        subject_id=payload.subject_id.strip(),
        role_key=payload.role_key,
        contact_email=payload.email.strip().lower() if payload.email and payload.email.strip() else None,
        contact_phone=payload.phone.strip() if payload.phone and payload.phone.strip() else None,
    )
    db.add(row)
    db.flush()
    portal_url = None
    email_sent = False
    if row.role_key in {"sales", "marketer"}:
        _token_row, raw_token = issue_agent_portal_token(db, member=row, actor=access.principal)
        portal_url = _agent_portal_url(raw_token)
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="organization_member.added", entity_type="estate_organization_member", entity_id=row.id, after_data={"subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key})
    db.commit()
    if portal_url and row.contact_email:
        organization = db.get(EstateOrganization, organization_id)
        email_sent = estate_email.send_agent_workspace_invite(organization=organization, member=row, portal_url=portal_url) if organization else False
    return {"id": row.id, "subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key, "email": row.contact_email, "phone": row.contact_phone, "is_active": row.is_active, "portal_url": portal_url, "email_sent": email_sent}


@router.patch("/organizations/{organization_id}/members/{member_id}")
def update_organization_member(organization_id: int, member_id: int, payload: MemberUpdate, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="estate.manage")
    row = db.query(EstateOrganizationMember).filter(EstateOrganizationMember.id == member_id, EstateOrganizationMember.organization_id == organization_id).one_or_none()
    if not row:
        raise HTTPException(404, "Organization member not found")
    values = payload.model_dump(exclude_unset=True)
    next_role = values.get("role_key", row.role_key)
    next_active = values.get("is_active", row.is_active)
    if row.role_key == "owner" and (next_role != "owner" or not next_active):
        owner_count = db.query(EstateOrganizationMember).filter(EstateOrganizationMember.organization_id == organization_id, EstateOrganizationMember.role_key == "owner", EstateOrganizationMember.is_active.is_(True)).count()
        if owner_count <= 1:
            raise HTTPException(409, "An organization must retain at least one active owner")
    before = {"role": row.role_key, "is_active": row.is_active, "email": row.contact_email, "phone": row.contact_phone}
    row.role_key = next_role
    row.is_active = next_active
    if "email" in values:
        row.contact_email = values["email"].strip().lower() if values["email"] and values["email"].strip() else None
    if "phone" in values:
        row.contact_phone = values["phone"].strip() if values["phone"] and values["phone"].strip() else None
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="organization_member.updated", entity_type="estate_organization_member", entity_id=row.id, before_data=before, after_data={"role": row.role_key, "is_active": row.is_active, "email": row.contact_email, "phone": row.contact_phone})
    db.commit()
    return {"id": row.id, "subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key, "email": row.contact_email, "phone": row.contact_phone, "is_active": row.is_active}


@router.post("/organizations/{organization_id}/members/{member_id}/portal-invite")
def send_agent_portal_invite(organization_id: int, member_id: int, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="estate.manage")
    member = db.query(EstateOrganizationMember).filter(EstateOrganizationMember.id == member_id, EstateOrganizationMember.organization_id == organization_id).one_or_none()
    if not member or member.role_key not in {"sales", "marketer"}:
        raise HTTPException(404, "Agent member not found")
    if not member.contact_email:
        raise HTTPException(422, "Add an email address before sending an invite")
    token_row, raw_token = issue_agent_portal_token(db, member=member, actor=access.principal)
    portal_url = _agent_portal_url(raw_token)
    db.commit()
    organization = db.get(EstateOrganization, organization_id)
    email_sent = estate_email.send_agent_workspace_invite(organization=organization, member=member, portal_url=portal_url) if organization else False
    return {"member_id": member.id, "portal_url": portal_url, "expires_at": token_row.expires_at, "email_sent": email_sent}


@router.post("/organizations/{organization_id}/members/{member_id}/portal-preview")
def preview_agent_portal(organization_id: int, member_id: int, request: Request, db: Session = Depends(get_db)):
    """Create a test link without emailing the agent or revoking their current link."""
    access = require_estate_access(db, request, organization_id, permission="estate.manage")
    member = db.query(EstateOrganizationMember).filter(
        EstateOrganizationMember.id == member_id,
        EstateOrganizationMember.organization_id == organization_id,
    ).one_or_none()
    if not member or member.role_key not in {"sales", "marketer"}:
        raise HTTPException(404, "Agent member not found")
    token_row, raw_token = issue_agent_portal_token(
        db,
        member=member,
        actor=access.principal,
        revoke_existing=False,
    )
    db.commit()
    return {"member_id": member.id, "portal_url": _agent_portal_url(raw_token), "expires_at": token_row.expires_at}


@router.get("/foundation/access")
def estate_foundation_access(request: Request, db: Session = Depends(get_db)):
    principal = resolve_estate_principal(db, request)
    access = list_estate_access(db, principal)
    if not access:
        raise HTTPException(status_code=403, detail="No active Estate organization membership")
    organizations = []
    for item in access:
        entitlements = {
            feature: get_estate_entitlement(db, item.organization_id, feature).is_enabled
            for feature in sorted(ESTATE_FEATURES)
        }
        organizations.append(
            {
                "id": item.organization_id,
                "name": item.organization_name,
                "slug": item.organization_slug,
                "role": item.role_key,
                "entitlements": entitlements,
            }
        )
    return {
        "principal": {"subject_type": principal.subject_type, "subject_id": principal.subject_id, "display_name": principal.display_name},
        "organizations": organizations,
    }
