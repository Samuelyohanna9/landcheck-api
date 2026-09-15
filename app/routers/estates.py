from __future__ import annotations

import csv
import io
import json
import re
import tempfile
import os
import math
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from geoalchemy2.shape import from_shape, to_shape
from pyproj import Transformer
import ezdxf
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform, unary_union
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.routers.plots import _metric_epsg_for_wgs84_polygon, _subdivide_polygon_equal_count, get_db
from app.services.estates.authorization import list_estate_access, resolve_estate_principal
from app.services.estates.entitlements import ESTATE_FEATURES, get_estate_entitlement
from app.models.estate_foundation import Estate, EstateAllocation, EstateAuditEvent, EstateBlock, EstateCommissionTier, EstateCustomer, EstateDocument, EstateDocumentLink, EstateFieldInspection, EstateHazardAssessment, EstateImportReview, EstateLayoutProposal, EstateOrganization, EstateOrganizationMember, EstatePayment, EstatePaymentRule, EstatePlot, EstateSpatialFeature, EstateSurveyRequest, EstateStakingTask
from app.schemas.estates import AllocationAction, BlockCreate, BlockUpdate, CommissionTiersUpdate, CustomerCreate, DevelopmentStatusUpdate, EstateCreate, EstateLayoutCriteria, EstateLayoutDecision, EstateLayoutFeatureAdd, EstateLayoutFeatureRemove, EstateLayoutProposalEdit, EstateSubdivisionCreate, EstateUpdate, FieldInspectionCreate, GeoreferenceSessionLink, ImportFromGeoreference, ImportReviewCreate, ImportReviewDecision, ImportReviewFromGeoreferenceSession, MemberCreate, MemberUpdate, PlotCreate, PlotGeometryUpdate, PaymentCreate, SpatialFeatureCreate, SpatialFeatureUpdate, SurveyEligibilityUpdate, VoidAction
from app.services.estates.payments import confirm_payment, financial_summary, record_payment, void_payment
from app.services.estates.documents import read_private_estate_file, store_private_estate_file
from app.services.estates.permissions import has_permission
from sqlalchemy import func
from app.services.estates.allocations import release_allocation, reserve_or_allocate
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import require_estate_access
from app.services.estates.survey_requests import transition
from app.services.estates.survey_adapter import materialize_estate_plot_for_survey
from app.schemas.estate_survey import SurveyorAssignment
from app.services.estates.survey_eligibility import survey_eligibility
from app.services.estates.qc import validate_polygon
from app.services.estates.layout_generation import generate_estate_layout
from app.services.survey.dgps import alpha_station, render_dgps_staking_csv
from app.utils.coordinate_converter import COORDINATE_SYSTEMS, resolve_coordinate_system_key, convert_coordinates
from app.models.estate_auth import EstateAccount
from app.utils.survey_auth_security import find_or_create_survey_user
from app.services.estates import estate_email
from app.services.estates.layout_export import render_estate_layout_pdf
from app.services.estates.report_export import render_estate_report_pdf
from app.services.estates import commissions


router = APIRouter(prefix="/estates", tags=["estates"])

def _survey_payload(db, row):
    plot=db.get(EstatePlot,row.plot_id); estate=db.get(Estate,row.estate_id)
    allocation = db.get(EstateAllocation, row.allocation_id) if row.allocation_id else None
    return {"id":row.id,"reference":row.request_uid,"status":row.status,"estate":{"id":estate.id,"name":estate.name},"plot":{"id":plot.id,"number":plot.plot_number},"assigned_surveyor":row.assigned_surveyor_subject_id,"survey_reference":row.survey_reference,"survey_working_plot_id":row.survey_working_plot_id,"materialized":bool(row.survey_working_plot_id),"eligibility":survey_eligibility(db, allocation),"created_at":row.created_at}


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
        "approximate_area_sqm": str(estate.approximate_area_sqm) if estate.approximate_area_sqm is not None else None,
        "project_reference": estate.project_reference,
        "project_owner": estate.project_owner,
        "ownership_details": estate.ownership_details,
    }


def _enabled(db: Session, org_id: int) -> None:
    if not get_estate_entitlement(db, org_id, "ESTATES_ENABLED").is_enabled:
        raise HTTPException(status_code=404, detail="LandCheck Estates is not enabled")


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
    return [{"id": row.id, "uid": row.estate_uid, "name": row.name, "status": row.status, "organization_id": row.organization_id, "location": row.location_text, "crs": row.crs, "project_reference": row.project_reference, "project_owner": row.project_owner} for row in rows]


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
    plots = db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id).all()
    counts = {key: sum(1 for plot in plots if plot.commercial_status == key) for key in ("available", "reserved", "allocated", "on_hold")}
    development = {key: sum(1 for plot in plots if plot.development_status == key) for key in ("not_started", "site_cleared", "foundation", "under_construction", "developed")}
    allocations = db.query(EstateAllocation).filter(EstateAllocation.estate_id == estate_id).all()
    summaries = [financial_summary(db, allocation) for allocation in allocations]
    staked_plot_ids = {task.plot_id for task in db.query(EstateStakingTask).filter(EstateStakingTask.estate_id == estate_id, EstateStakingTask.status == "completed").all()}
    awaiting_survey = sum(1 for plot in plots if plot.commercial_status == "allocated" and not db.query(EstateSurveyRequest).filter(EstateSurveyRequest.plot_id == plot.id, EstateSurveyRequest.status.in_(("in_progress", "ready_for_review", "approved", "completed"))).first())
    survey_completed = sum(1 for plot in plots if db.query(EstateSurveyRequest).filter(EstateSurveyRequest.plot_id == plot.id, EstateSurveyRequest.status.in_(("approved", "completed"))).first())
    return {
        "estate": {"id": estate.id, "name": estate.name, "status": estate.status},
        "total_plots": len(plots),
        "statuses": {**counts, "sold": counts["allocated"]},
        "development": development,
        "staked_plots": len(staked_plot_ids),
        "awaiting_survey": awaiting_survey,
        "survey_completed": survey_completed,
        "awaiting_staking": sum(1 for plot in plots if plot.commercial_status == "allocated" and plot.id not in staked_plot_ids),
        "geometry_issues": sum(1 for plot in plots if plot.geometry_status != "approved"),
        "mapped_area_sqm": float(sum(float(plot.area_sqm) for plot in plots)),
        "financial": {
            "contracted_sales_value": str(sum((summary.agreed_price for summary in summaries), 0)),
            "confirmed_collections": str(sum((summary.confirmed_paid for summary in summaries), 0)),
            "pending_collections": str(sum((summary.pending_paid for summary in summaries), 0)),
            "outstanding_balance": str(sum((summary.outstanding for summary in summaries), 0)),
        },
    }

@router.get("/{estate_id}/plots.geojson")
def estate_plots_geojson(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    require_estate_access(db,request,estate.organization_id,permission="plot.read")
    rows=db.query(EstatePlot).filter(EstatePlot.estate_id==estate_id).all()
    return {"type":"FeatureCollection","features":[{"type":"Feature","id":plot.id,"properties":{"id":plot.id,"plot_number":plot.plot_number,"commercial_status":plot.commercial_status,"development_status":plot.development_status,"geometry_status":plot.geometry_status,"area_sqm":float(plot.area_sqm),"block_id":plot.block_id},"geometry":mapping(to_shape(plot.geometry))} for plot in rows if plot.geometry]}

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
            closed = getattr(entity, "is_closed", False)
            if callable(closed):
                closed = closed()
            if not bool(closed):
                continue
            if entity.dxftype() == "LWPOLYLINE":
                raw_points = entity.get_points()
            else:
                vertices = entity.vertices() if callable(getattr(entity, "vertices", None)) else entity.vertices
                raw_points = [(vertex.dxf.location.x, vertex.dxf.location.y) for vertex in vertices]
            points=[transformer.transform(float(point[0]),float(point[1])) for point in raw_points]
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
    polygons = [feature for feature in features if str(feature.get("feature_type") or "") == "polygon"]
    if not polygons:
        raise HTTPException(422, "No digitized polygons were saved in this georeference session yet")
    prefix = (payload.plot_prefix or "P").strip() or "P"
    candidates = []
    for index, feature in enumerate(polygons, 1):
        geometry = _polygon_geometry_from_georeference_feature(feature)
        plot_number = _plot_number_from_georeference_feature(feature, index, prefix)
        candidates.append(_import_candidate(row_number=index, plot_number=plot_number, geometry=geometry))
    row.candidate_data = candidates
    estate = db.get(Estate, row.estate_id)
    created = _create_plots_from_candidates(db, estate=estate, candidates=candidates, source_type=row.source_type, source_reference=str(row.id), actor=access.principal)
    row.notes = (row.notes or "") + f"; {created} operational plot(s) created from the georeferenced layout"
    row.status = "approved"
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="import_review.approved", entity_type="estate_import_review", entity_id=row.id, after_data={"status": row.status, "source": "georeference", "created_plots": created})
    db.commit()
    return {"id": row.id, "status": row.status, "created_plots": created}


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
    row.diagnostics = diagnostics

    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="layout_proposal.feature_added", entity_type="estate_layout_proposal", entity_id=row.id, after_data={"feature_type": payload.feature_type, "name": new_feature["name"], "plots_removed": removed, "plots_remaining": len(updated_candidates)})
    db.commit()
    return _layout_proposal_payload(row)


@router.post("/layout-proposals/{proposal_id}/remove-feature")
def remove_layout_proposal_feature(proposal_id: int, payload: EstateLayoutFeatureRemove, request: Request, db: Session = Depends(get_db)):
    """Removes a road or open-space shape and gives the vacated space back to the plots that
    fronted it - see EstateLayoutFeatureRemove for the two ways that happens."""
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

    carved_plots = feature.get("carved_plots") or []
    if carved_plots:
        # This feature carved these plots when it was added - hand each one back exactly as it
        # was, regardless of what shape it holds right now.
        for entry in carved_plots:
            candidates_by_number[str(entry.get("plot_number"))] = dict(entry)
    elif feature.get("feature_type") == "road":
        try:
            road_shape = shape(feature.get("geometry") or {})
        except Exception:
            road_shape = None
        if road_shape is not None and not road_shape.is_empty and road_shape.geom_type in {"LineString", "MultiLineString"}:
            metric_epsg = _metric_epsg_for_wgs84_polygon(road_shape)
            forward = Transformer.from_crs("EPSG:4326", f"EPSG:{metric_epsg}", always_xy=True).transform
            backward = Transformer.from_crs(f"EPSG:{metric_epsg}", "EPSG:4326", always_xy=True).transform
            road_metric = shapely_transform(forward, road_shape)
            width_m = float(feature.get("width_m") or (row.criteria or {}).get("road_width_m") or 9.0)
            # A plain generated road has no "before" shape to restore to - it was part of the grid
            # from the start. Instead, split the vacated corridor along its centreline (Shapely's
            # single_sided buffer gives exactly one side at a time) and merge each half into
            # whichever plots actually front it, so the road's old footprint doesn't become
            # nobody's land.
            for half in (road_metric.buffer(width_m / 2, single_sided=True), road_metric.buffer(-(width_m / 2), single_sided=True)):
                if half.is_empty:
                    continue
                margin = width_m / 2 + 0.05
                for plot_number, candidate in list(candidates_by_number.items()):
                    try:
                        plot_metric = shapely_transform(forward, shape(candidate.get("geometry") or {}))
                    except Exception:
                        continue
                    shadow = half.intersection(plot_metric.buffer(margin))
                    if shadow.is_empty:
                        continue
                    grown = _largest_polygon_piece(unary_union([plot_metric, shadow])) or plot_metric
                    grown_wgs84 = shapely_transform(backward, grown)
                    area_sqm, issues = validate_polygon(mapping(grown_wgs84))
                    if any(issue.severity == "error" for issue in issues):
                        continue
                    candidates_by_number[plot_number] = {**candidate, "geometry": mapping(grown_wgs84), "area_sqm": round(float(area_sqm), 2), "valid": True, "issues": []}

    row.plot_candidates = list(candidates_by_number.values())
    row.feature_candidates = [item for index, item in enumerate(features) if index != payload.feature_index]
    diagnostics = dict(row.diagnostics or {})
    diagnostics["estimated_plot_count"] = len(row.plot_candidates)
    diagnostics["total_plot_area_sqm"] = round(sum(float(candidate.get("area_sqm") or 0) for candidate in row.plot_candidates), 2)
    row.diagnostics = diagnostics

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
    for plot in plots:
        seen.setdefault(plot.plot_number_normalized,[]).append(plot.id)
        if not plot.geometry: issues.append({"severity":"error","plot_id":plot.id,"code":"missing_geometry","message":"Plot has no geometry"}); continue
        _, qc=validate_polygon(mapping(to_shape(plot.geometry)))
        issues.extend({"severity":item.severity,"plot_id":plot.id,"code":item.code,"message":item.message} for item in qc)
    for number, ids in seen.items():
        if len(ids)>1: issues.append({"severity":"error","plot_id":ids[0],"code":"duplicate_plot_number","message":f"Duplicate plot number {number}"})
    for index, plot in enumerate(plots):
        if not plot.geometry: continue
        geom=to_shape(plot.geometry)
        for other in plots[index+1:]:
            if other.geometry and geom.intersection(to_shape(other.geometry)).area > 1e-12: issues.append({"severity":"error","plot_id":plot.id,"related_plot_id":other.id,"code":"overlap","message":f"Overlaps plot {other.plot_number}"})
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


@router.get("/plots/{plot_id}/hazards")
def plot_hazards(plot_id: int, request: Request, db: Session = Depends(get_db)):
    """Return the latest stored result, with a read-only calculation for legacy records."""
    plot = db.get(EstatePlot, plot_id)
    if not plot:
        raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, plot.estate_id)
    require_estate_access(db, request, estate.organization_id, permission="plot.read")
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
    if plot.geometry_status != "approved":
        raise HTTPException(409, "Only approved plot geometry can be screened")
    results = _calculate_plot_hazards(plot.geometry, db)
    rows = _persist_hazard_results(db, estate=estate, plot_id=plot.id, results=results, access=access)
    db.commit()
    return {"plot_id": plot.id, "persisted": True, "assessments": [_hazard_row_payload(row) for row in rows], **results}


@router.post("/{estate_id}/hazards/assess-all")
def assess_estate_hazards(estate_id: int, request: Request, db: Session = Depends(get_db)):
    """Runs flood + erosion screening for every approved plot in this Estate in one action - the
    whole-layout equivalent of running it one plot at a time from each plot's drawer. Returns the
    same aggregate shape as GET .../hazards so both this page and the Dashboard's Risk Overview
    card (which reads that same endpoint) reflect the fresh run immediately."""
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="plot.manage")
    plots = db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry_status == "approved").all()
    plots = [plot for plot in plots if plot.geometry]
    if not plots:
        raise HTTPException(422, "This Estate has no approved plot geometry to screen yet")
    for plot in plots:
        results = _calculate_plot_hazards(plot.geometry, db)
        _persist_hazard_results(db, estate=estate, plot_id=plot.id, results=results, access=access)
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="hazard.estate_assessment_completed", entity_type="estate", entity_id=estate.id, after_data={"plots_screened": len(plots)})
    db.commit()
    return estate_hazard_dashboard(estate_id, request, db)


@router.get("/{estate_id}/hazards")
def estate_hazard_dashboard(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="plot.read")
    latest = {}
    rows = db.query(EstateHazardAssessment).filter(EstateHazardAssessment.estate_id == estate_id).order_by(EstateHazardAssessment.assessed_at.desc()).all()
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

@router.get("/{estate_id}/activity")
def estate_activity(estate_id:int, request:Request, limit:int=100, db:Session=Depends(get_db)):
    estate=db.get(Estate,estate_id)
    if not estate: raise HTTPException(404,"Estate not found")
    require_estate_access(db,request,estate.organization_id,permission="audit.read")
    rows=db.query(EstateAuditEvent).filter(EstateAuditEvent.organization_id==estate.organization_id).order_by(EstateAuditEvent.created_at.desc()).limit(min(max(limit,1),200)).all()
    return [{"id":row.id,"action":row.action,"entity_type":row.entity_type,"entity_id":row.entity_id,"actor":row.actor_subject_id,"created_at":row.created_at,"details":row.after_data} for row in rows]


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
    plot = EstatePlot(estate_id=estate_id, block_id=payload.block_id, plot_number=payload.plot_number.strip(), plot_number_normalized=normalized, geometry=from_shape(shape(payload.geometry), srid=4326), area_sqm=area, land_use=payload.land_use, geometry_status=payload.geometry_status, created_by_subject_type=access.principal.subject_type, created_by_subject_id=access.principal.subject_id)
    db.add(plot); db.flush(); append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="plot.created", entity_type="estate_plot", entity_id=plot.id, after_data={"plot_number": plot.plot_number, "area_sqm": area, "geometry_status": plot.geometry_status})
    db.commit(); return {"id": plot.id, "uid": plot.plot_uid, "area_sqm": area, "qc": [{"severity": issue.severity, "code": issue.code, "message": issue.message} for issue in issues]}


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
    if plot.development_status == "developed" and previous != "developed":
        active_allocation = db.query(EstateAllocation).filter(EstateAllocation.plot_id == plot.id, EstateAllocation.status.in_(("reserved", "allocated"))).one_or_none()
        if active_allocation:
            _notify_allocation_customer(db, allocation=active_allocation, org_name=access.organization_name, event="land_developed")
    return {"id":plot.id,"development_status":plot.development_status}

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
    db.add(row); db.flush(); append_estate_audit_event(db,organization_id=estate.organization_id,actor=access.principal,action="survey_request.created",entity_type="estate_survey_request",entity_id=row.id); db.commit(); return _survey_payload(db,row)

@router.get("/survey-requests")
def list_survey_requests(request:Request,db:Session=Depends(get_db)):
    principal=resolve_estate_principal(db,request); allowed={a.organization_id for a in list_estate_access(db,principal) if has_permission(a.role_key,"survey.read")}
    return [_survey_payload(db,row) for row in db.query(EstateSurveyRequest).filter(EstateSurveyRequest.organization_id.in_(allowed)).all()]

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
    if row.allocation_id:
        allocation = db.get(EstateAllocation, row.allocation_id)
        if allocation:
            _notify_allocation_customer(db, allocation=allocation, org_name=access.organization_name, event="survey_ready")
    return _survey_payload(db, row)

@router.post("/survey-requests/{request_id}/staking-tasks")
def create_staking_task(request_id:int,request:Request,db:Session=Depends(get_db)):
    survey=db.get(EstateSurveyRequest,request_id)
    if not survey: raise HTTPException(404,"Survey request not found")
    access=require_estate_access(db,request,survey.organization_id,permission="staking.manage")
    if not survey.survey_working_plot_id: raise HTTPException(409,"Survey must be started before staking")
    existing=db.query(EstateStakingTask).filter(EstateStakingTask.survey_request_id==survey.id,EstateStakingTask.status.in_(("pending","assigned","in_progress"))).one_or_none()
    if existing: return {"id":existing.id,"status":existing.status}
    task=EstateStakingTask(organization_id=survey.organization_id,estate_id=survey.estate_id,plot_id=survey.plot_id,survey_request_id=survey.id,status="pending")
    db.add(task); db.flush(); append_estate_audit_event(db,organization_id=survey.organization_id,actor=access.principal,action="staking_task.created",entity_type="estate_staking_task",entity_id=task.id); db.commit(); return {"id":task.id,"status":task.status}

@router.get("/staking-tasks")
def list_staking_tasks(request:Request,db:Session=Depends(get_db)):
    principal=resolve_estate_principal(db,request); allowed={item.organization_id for item in list_estate_access(db,principal) if has_permission(item.role_key,"staking.read")}
    rows=db.query(EstateStakingTask).filter(EstateStakingTask.organization_id.in_(allowed)).all()
    return [{"id":row.id,"status":row.status,"plot_id":row.plot_id,"survey_request_id":row.survey_request_id,"assigned_subject_id":row.assigned_subject_id,"completed_at":row.completed_at} for row in rows]

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
    if active_allocation:
        _notify_allocation_customer(db, allocation=active_allocation, org_name=access.organization_name, event="staked")
    return {"id":task.id,"status":task.status,"completed_at":task.completed_at}


@router.post("/organizations/{organization_id}/customers")
def create_customer(organization_id: int, payload: CustomerCreate, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="customer.manage"); _enabled(db, organization_id)
    customer = EstateCustomer(organization_id=organization_id, full_name=payload.full_name.strip(), full_name_normalized=_normalized(payload.full_name), reference_no=payload.reference_no, phone=payload.phone, email=payload.email, address=payload.address, company_name=payload.company_name, notes=payload.notes)
    db.add(customer); db.flush(); append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="customer.created", entity_type="estate_customer", entity_id=customer.id, after_data={"name": customer.full_name})
    db.commit(); return {"id": customer.id, "uid": customer.customer_uid, "name": customer.full_name}


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
    results = []
    for row in rows:
        display_name = row.subject_id
        if row.subject_type == "estate_account":
            account = db.get(EstateAccount, int(row.subject_id)) if row.subject_id.isdigit() else None
            if account:
                display_name = account.full_name
        results.append({"subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key, "display_name": display_name})
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
def commission_report(organization_id: int, request: Request, db: Session = Depends(get_db)):
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
    results = []
    for (subject_type, subject_id), bucket in by_agent.items():
        display_name = subject_id
        if subject_type == "estate_account":
            account = db.get(EstateAccount, int(subject_id)) if subject_id.isdigit() else None
            if account:
                display_name = account.full_name
        results.append({
            "subject_type": subject_type,
            "subject_id": subject_id,
            "display_name": display_name,
            "sale_count": bucket["sale_count"],
            "total_volume": str(bucket["total_volume"]),
            "total_commission": str(bucket["total_commission"]),
            "current_tier": bucket["current_tier"],
        })
    results.sort(key=lambda item: float(item["total_commission"]), reverse=True)
    return {"organization_id": organization_id, "agents": results}


def _apply_initial_payment(db: Session, *, record: EstateAllocation, payload: AllocationAction, actor) -> None:
    """An initial payment entered in the same reserve/allocate call is recorded AND immediately
    confirmed - unlike a normal payment, this represents money the org is directly attesting it
    already received, not a pending customer claim awaiting confirmation."""
    if not payload.initial_payment_amount:
        return
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
    )
    confirm_payment(db, payment=payment, actor=actor)


def _notify_allocation_customer(db: Session, *, allocation: EstateAllocation, org_name: str, event: str, amount_just_paid=None) -> None:
    customer = db.get(EstateCustomer, allocation.customer_id)
    if not customer or not customer.email:
        return
    plot = db.get(EstatePlot, allocation.plot_id)
    estate = db.get(Estate, allocation.estate_id)
    if not plot or not estate:
        return
    summary = financial_summary(db, allocation)
    estate_email.notify_customer(
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
    )


@router.post("/{estate_id}/plots/{plot_id}/reserve")
def reserve_plot(estate_id: int, plot_id: int, payload: AllocationAction, request: Request, db: Session = Depends(get_db)):
    plot = db.query(EstatePlot).filter(EstatePlot.id == plot_id, EstatePlot.estate_id == estate_id).one_or_none()
    if not plot: raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, estate_id)
    access = require_estate_access(db, request, estate.organization_id, permission="allocation.manage")
    customer = db.get(EstateCustomer, payload.customer_id)
    if not customer or customer.organization_id != access.organization_id: raise HTTPException(404, "Customer not found")
    record = reserve_or_allocate(db, plot=plot, customer=customer, actor=access.principal, allocate=False, expires_at=payload.expires_at, agreed_price=payload.agreed_price, payment_plan=payload.payment_plan, notes=payload.notes)
    if payload.sales_agent_subject_type and payload.sales_agent_subject_id:
        record.sales_agent_subject_type = payload.sales_agent_subject_type
        record.sales_agent_subject_id = payload.sales_agent_subject_id
    _apply_initial_payment(db, record=record, payload=payload, actor=access.principal)
    db.commit()
    _notify_allocation_customer(db, allocation=record, org_name=access.organization_name, event="reserved")
    return {"id": record.id, "status": record.status, "agreed_price": str(record.agreed_price) if record.agreed_price is not None else None}


@router.post("/{estate_id}/plots/{plot_id}/allocate")
def allocate_plot(estate_id: int, plot_id: int, payload: AllocationAction, request: Request, db: Session = Depends(get_db)):
    """Marks a plot Allocated - the official, title-bearing status a Nigerian estate normally only
    grants once a plot is fully paid for (a signed reservation typically comes first, on a deposit
    or nothing at all; full allocation and its paperwork follow full payment). Enforced here rather
    than left to operator discretion: this call is refused unless the confirmed payments already on
    record, plus any initial payment submitted in this same call, cover the full agreed price."""
    plot = db.query(EstatePlot).filter(EstatePlot.id == plot_id, EstatePlot.estate_id == estate_id).one_or_none()
    if not plot: raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, estate_id)
    access = require_estate_access(db, request, estate.organization_id, permission="allocation.manage")
    customer = db.get(EstateCustomer, payload.customer_id)
    if not customer or customer.organization_id != access.organization_id: raise HTTPException(404, "Customer not found")
    existing = db.query(EstateAllocation).filter(EstateAllocation.plot_id == plot.id, EstateAllocation.status.in_(("reserved", "allocated"))).one_or_none()
    effective_agreed_price = payload.agreed_price if payload.agreed_price is not None else (existing.agreed_price if existing else None)
    already_confirmed = financial_summary(db, existing).confirmed_paid if existing else Decimal("0")
    incoming = payload.initial_payment_amount or Decimal("0")
    if effective_agreed_price and (already_confirmed + incoming) < effective_agreed_price:
        raise HTTPException(
            409,
            f"This plot cannot be marked Allocated until it is fully paid. Confirmed so far: "
            f"{estate_email.format_naira(already_confirmed + incoming)} of {estate_email.format_naira(effective_agreed_price)} agreed. "
            f"Reserve it instead, or record the remaining payment first.",
        )
    record = reserve_or_allocate(db, plot=plot, customer=customer, actor=access.principal, allocate=True, agreed_price=payload.agreed_price, payment_plan=payload.payment_plan, notes=payload.notes)
    if payload.sales_agent_subject_type and payload.sales_agent_subject_id:
        record.sales_agent_subject_type = payload.sales_agent_subject_type
        record.sales_agent_subject_id = payload.sales_agent_subject_id
    _apply_initial_payment(db, record=record, payload=payload, actor=access.principal)
    commissions.apply_commission(db, allocation=record)
    db.commit()
    _notify_allocation_customer(db, allocation=record, org_name=access.organization_name, event="allocated")
    return {"id": record.id, "status": record.status, "agreed_price": str(record.agreed_price) if record.agreed_price is not None else None}


@router.post("/allocations/{allocation_id}/release")
def release_plot(allocation_id: int, request: Request, db: Session = Depends(get_db)):
    record = db.get(EstateAllocation, allocation_id)
    if not record: raise HTTPException(404, "Allocation not found")
    access = require_estate_access(db, request, record.organization_id, permission="allocation.manage")
    release_allocation(db, allocation=record, actor=access.principal, reason="Released by authorized user"); db.commit(); return {"id": record.id, "status": record.status}

@router.post("/allocations/{allocation_id}/payments")
def add_payment(allocation_id: int, payload: PaymentCreate, request: Request, db: Session = Depends(get_db)):
    allocation=db.get(EstateAllocation, allocation_id)
    if not allocation: raise HTTPException(404,"Allocation not found")
    access=require_estate_access(db,request,allocation.organization_id,permission="payment.manage")
    payment=record_payment(db,allocation=allocation,amount=payload.amount,payment_date=payload.payment_date,method=payload.payment_method,reference=payload.reference_no,notes=payload.notes,actor=access.principal)
    db.commit()
    _notify_allocation_customer(db, allocation=allocation, org_name=access.organization_name, event="payment_recorded", amount_just_paid=payload.amount)
    return {"id":payment.id,"status":payment.status}

@router.post("/payments/{payment_id}/confirm")
def confirm(payment_id:int, request:Request, db:Session=Depends(get_db)):
    payment=db.get(EstatePayment,payment_id)
    if not payment: raise HTTPException(404,"Payment not found")
    access=require_estate_access(db,request,payment.organization_id,permission="payment.manage")
    confirm_payment(db,payment=payment,actor=access.principal)
    db.commit()
    allocation = db.get(EstateAllocation, payment.allocation_id)
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
        _notify_allocation_customer(db, allocation=allocation, org_name=access.organization_name, event="payment_completed" if fully_paid else "payment_recorded", amount_just_paid=payment.amount)
    return {"id":payment.id,"status":payment.status}

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
    model = {"estate": Estate, "plot": EstatePlot, "customer": EstateCustomer, "allocation": EstateAllocation, "payment": EstatePayment, "staking_task": EstateStakingTask, "field_inspection": EstateFieldInspection, "import_review": EstateImportReview}.get(entity_type)
    row = db.get(model, entity_id) if model else None
    return getattr(row, "organization_id", None)

@router.post("/documents")
async def upload_document(entity_type: str, entity_id: int, document_type: str, request: Request, file: UploadFile = File(...), description: str | None = None, db: Session = Depends(get_db)):
    organization_id = _linked_entity_organization(db, entity_type, entity_id)
    if not organization_id: raise HTTPException(404, "Linked Estate entity was not found")
    access = require_estate_access(db, request, organization_id, permission="document.manage")
    organization = db.get(EstateOrganization, organization_id)
    stored = store_private_estate_file(organization_uid=organization.organization_uid, category="documents", entity_uid=f"{entity_type}_{entity_id}", filename=file.filename or "document", content_type=file.content_type or "", data=await file.read())
    document = EstateDocument(organization_id=organization_id, object_key=stored.object_key, original_filename=stored.filename, mime_type=stored.mime_type, size_bytes=stored.size_bytes, checksum=stored.checksum, document_type=document_type.lower(), description=description, uploaded_by_subject_type=access.principal.subject_type, uploaded_by_subject_id=access.principal.subject_id)
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
        evidence_by_payment: dict[str, list[dict]] = {}
        payment_ids=[str(p.id) for p in payments]
        if payment_ids:
            evidence_rows=db.query(EstateDocument,EstateDocumentLink.entity_id).join(EstateDocumentLink,EstateDocumentLink.document_id==EstateDocument.id).filter(EstateDocumentLink.entity_type=="payment",EstateDocumentLink.entity_id.in_(payment_ids)).all()
            for document,entity_id in evidence_rows:
                evidence_by_payment.setdefault(entity_id,[]).append({"id":document.id,"filename":document.original_filename})
        rows.append({"allocation_id":allocation.id,"allocation_date":allocation.allocation_date,"estate":estate.name if estate else None,"plot":plot.plot_number if plot else None,"payment_plan":allocation.payment_plan,"agreed_price":str(summary.agreed_price),"confirmed_paid":str(summary.confirmed_paid),"pending_paid":str(summary.pending_paid),"outstanding":str(summary.outstanding),"transactions":[{"id":p.id,"date":p.payment_date,"reference":p.reference_no,"method":p.payment_method,"amount":str(p.amount),"status":p.status,"receipts":evidence_by_payment.get(str(p.id),[])} for p in payments]})
    organization=db.get(EstateOrganization,customer.organization_id)
    return {"statement_date":__import__("datetime").datetime.utcnow().isoformat()+"Z","organization":{"id":customer.organization_id,"name":organization.name if organization else access.organization_name},"customer":{"id":customer.id,"name":customer.full_name,"reference":customer.reference_no},"allocations":rows}

@router.get("/allocations/{allocation_id}/financial-detail")
def allocation_financial_detail(allocation_id:int,request:Request,db:Session=Depends(get_db)):
    allocation=db.get(EstateAllocation,allocation_id)
    if not allocation: raise HTTPException(404,"Allocation not found")
    require_estate_access(db,request,allocation.organization_id,permission="payment.read")
    summary=financial_summary(db,allocation); customer=db.get(EstateCustomer,allocation.customer_id); plot=db.get(EstatePlot,allocation.plot_id); estate=db.get(Estate,allocation.estate_id)
    payments=db.query(EstatePayment).filter(EstatePayment.allocation_id==allocation.id).order_by(EstatePayment.payment_date.desc()).all()
    return {"allocation":{"id":allocation.id,"status":allocation.status,"allocation_date":allocation.allocation_date,"payment_plan":allocation.payment_plan},"customer":{"id":customer.id,"name":customer.full_name,"reference":customer.reference_no,"phone":customer.phone,"email":customer.email},"estate":{"id":estate.id,"name":estate.name},"plot":{"id":plot.id,"number":plot.plot_number},"financial":{"agreed_price":str(summary.agreed_price),"confirmed_paid":str(summary.confirmed_paid),"pending_paid":str(summary.pending_paid),"outstanding":str(summary.outstanding),"percentage":str(summary.percentage),"fully_paid":summary.agreed_price>0 and summary.outstanding==0},"payments":[{"id":p.id,"date":p.payment_date,"amount":str(p.amount),"status":p.status,"method":p.payment_method,"reference":p.reference_no} for p in payments]}

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
    allocations=db.query(EstateAllocation).filter(EstateAllocation.estate_id==estate_id).all(); summaries=[financial_summary(db,a) for a in allocations]
    return {"estate":{"id":estate.id,"name":estate.name},"contracted_sales":str(sum((s.agreed_price for s in summaries),0)),"confirmed_collections":str(sum((s.confirmed_paid for s in summaries),0)),"pending_collections":str(sum((s.pending_paid for s in summaries),0)),"outstanding_balance":str(sum((s.outstanding for s in summaries),0)),"fully_paid_allocations":sum(1 for s in summaries if s.agreed_price>0 and s.outstanding==0),"allocations_with_outstanding":sum(1 for s in summaries if s.outstanding>0)}

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
    return {"page":page,"page_size":min(max(page_size,1),100),"total":total,"items":[{"id":p.id,"date":p.payment_date,"amount":str(p.amount),"currency":p.currency,"status":p.status,"method":p.payment_method,"reference":p.reference_no,"customer":{"id":c.id,"name":c.full_name},"estate":{"id":e.id,"name":e.name},"plot":{"id":plot.id,"number":plot.plot_number},"allocation_id":p.allocation_id,"recorded_by":p.recorded_by_subject_id,"confirmed_by":p.confirmed_by_subject_id,"can_confirm":has_permission(memberships[p.organization_id].role_key,"payment.manage") and p.status in {"recorded","pending_confirmation"},"can_void":has_permission(memberships[p.organization_id].role_key,"payment.manage") and p.status not in {"voided","reversed"},"can_view_receipt":has_permission(memberships[p.organization_id].role_key,"document.read")} for p,c,e,plot in rows]}

@router.get("/payments/{payment_id}")
def payment_detail(payment_id:int,request:Request,db:Session=Depends(get_db)):
    payment=db.get(EstatePayment,payment_id)
    if not payment: raise HTTPException(404,"Payment not found")
    access=require_estate_access(db,request,payment.organization_id,permission="payment.read")
    allocation=db.get(EstateAllocation,payment.allocation_id); customer=db.get(EstateCustomer,payment.customer_id); plot=db.get(EstatePlot,payment.plot_id); estate=db.get(Estate,allocation.estate_id); summary=financial_summary(db,allocation)
    evidence=db.query(EstateDocument).join(EstateDocumentLink,EstateDocumentLink.document_id==EstateDocument.id).filter(EstateDocumentLink.entity_type=="payment",EstateDocumentLink.entity_id==str(payment_id)).all()
    return {"payment":{"id":payment.id,"amount":str(payment.amount),"currency":payment.currency,"date":payment.payment_date,"method":payment.payment_method,"reference":payment.reference_no,"notes":payment.notes,"status":payment.status,"recorded_by":payment.recorded_by_subject_id,"confirmed_by":payment.confirmed_by_subject_id,"confirmed_at":payment.confirmed_at,"void_reason":payment.void_reason},"customer":{"id":customer.id,"name":customer.full_name},"estate":{"id":estate.id,"name":estate.name},"plot":{"id":plot.id,"number":plot.plot_number},"allocation_id":allocation.id,"financial":{"agreed_price":str(summary.agreed_price),"confirmed":str(summary.confirmed_paid),"pending":str(summary.pending_paid),"outstanding":str(summary.outstanding)},"capabilities":{"can_confirm":has_permission(access.role_key,"payment.manage") and payment.status in {"recorded","pending_confirmation"},"can_void":has_permission(access.role_key,"payment.manage") and payment.status not in {"voided","reversed"},"can_view_receipt":has_permission(access.role_key,"document.read")},"evidence":[{"id":d.id,"filename":d.original_filename,"mime_type":d.mime_type} for d in evidence]}

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
    allocation_items=[]
    for allocation,estate,plot,customer in allocations.all():
        summary=financial_summary(db,allocation); allocation_items.append({"id":allocation.id,"estate_id":estate.id,"estate_name":estate.name,"plot_id":plot.id,"plot_number":plot.plot_number,"customer_id":customer.id,"customer_name":customer.full_name,"status":allocation.status,"allocation_date":allocation.allocation_date,"payment_plan":allocation.payment_plan,"agreed_price":str(summary.agreed_price),"currency":"NGN","confirmed":str(summary.confirmed_paid),"pending":str(summary.pending_paid),"outstanding":str(summary.outstanding)})
    return {"estates":[{"id":e.id,"name":e.name} for e in estates],"customers":[{"id":c.id,"name":c.full_name,"reference":c.reference_no} for c in customers],"plots":[{"id":p.id,"plot_number":p.plot_number,"estate_id":e.id,"estate_name":e.name,"commercial_status":p.commercial_status,"development_status":p.development_status,"geometry_status":p.geometry_status,"block_id":p.block_id,"area_sqm":float(p.area_sqm) if p.area_sqm is not None else 0.0} for p,e in plots],"allocations":allocation_items}


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
        "approximate_area_sqm": str(estate.approximate_area_sqm) if estate.approximate_area_sqm is not None else None,
        "project_reference": estate.project_reference,
        "project_owner": estate.project_owner,
        "ownership_details": estate.ownership_details,
        "boundary": mapping(to_shape(estate.boundary)) if estate.boundary else None,
    }


@router.get("/organizations/{organization_id}/members")
def list_organization_members(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="estate.manage")
    rows = db.query(EstateOrganizationMember).filter(EstateOrganizationMember.organization_id == organization_id).order_by(EstateOrganizationMember.created_at.asc()).all()
    return [{"id": row.id, "subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key, "is_active": row.is_active} for row in rows]


@router.post("/organizations/{organization_id}/members")
def add_organization_member(organization_id: int, payload: MemberCreate, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="estate.manage")
    existing = db.query(EstateOrganizationMember).filter(EstateOrganizationMember.organization_id == organization_id, EstateOrganizationMember.subject_type == payload.subject_type.strip(), EstateOrganizationMember.subject_id == payload.subject_id.strip()).one_or_none()
    if existing:
        raise HTTPException(409, "This identity is already an organization member")
    row = EstateOrganizationMember(organization_id=organization_id, subject_type=payload.subject_type.strip().lower(), subject_id=payload.subject_id.strip(), role_key=payload.role_key)
    db.add(row)
    db.flush()
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="organization_member.added", entity_type="estate_organization_member", entity_id=row.id, after_data={"subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key})
    db.commit()
    return {"id": row.id, "subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key, "is_active": row.is_active}


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
    before = {"role": row.role_key, "is_active": row.is_active}
    row.role_key = next_role
    row.is_active = next_active
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="organization_member.updated", entity_type="estate_organization_member", entity_id=row.id, before_data=before, after_data={"role": row.role_key, "is_active": row.is_active})
    db.commit()
    return {"id": row.id, "subject_type": row.subject_type, "subject_id": row.subject_id, "role": row.role_key, "is_active": row.is_active}


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
