from __future__ import annotations

"""The "Connect your tools" Excel/CSV import wizard (Phase 1 of Integrations - see
app/services/estates/data_import.py for the matching/commit logic and the module docstring there
for why Google Sheets live sync is a deliberately separate, later phase)."""

import csv
import io
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy.orm import Session

from app.models.estate_foundation import (
    Estate,
    EstateImportBatch,
    EstateImportMappingPreset,
    EstateImportRow,
    EstatePlot,
)
from app.routers.plots import get_db
from app.schemas.estates import ImportCommitRequest, ImportMappingPresetCreate, ImportMappingSubmit, ImportRowOverride
from app.services.estates import data_import
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import list_estate_access, require_estate_access, resolve_estate_principal
from app.services.estates.permissions import has_permission
from app.utils.upload_security import MAX_IMPORT_UPLOAD_BYTES, read_limited_upload, upload_extension

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/estates/import-batches", tags=["estate-imports"])

ALLOWED_IMPORT_EXTENSIONS = {".xlsx", ".xls", ".csv"}
ENTITY_TYPES = ("customer", "plot", "sale", "payment")


def _sheet_summary(rows: list[dict]) -> dict:
    headers = list(rows[0].keys()) if rows else []
    suggestions = {entity: data_import.suggest_column_mapping(headers, entity) for entity in ENTITY_TYPES}
    return {"headers": headers, "row_count": len(rows), "sample_rows": rows[:5], "suggested_mappings": suggestions}


@router.get("/field-templates")
def download_field_template():
    """A downloadable starting-point workbook - entirely optional, per the product spec; a
    developer's own column names work fine via the mapping step without ever touching this.
    A real .xlsx (not a comma-delimited .csv) so every field lands in its own column regardless
    of the user's Excel locale/list-separator settings, instead of spilling into column A."""
    headers = ["Customer ID", "Client Name", "Phone", "Email", "Plot No.", "Block", "Agreed Price", "Sale Date", "Contract No.", "Amount Paid", "Payment Date", "Payment Method", "Reference"]
    sample_row = ["", "Jane Doe", "08031234567", "jane@example.com", "A-015", "Block A", 5000000, "2025-03-01", "CN-1001", 500000, "2025-03-01", "bank_transfer", "TXN-001"]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Import template"
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.append(sample_row)
    for column_cells in sheet.columns:
        width = max((len(str(cell.value)) for cell in column_cells if cell.value is not None), default=10)
        sheet.column_dimensions[column_cells[0].column_letter].width = max(10, width + 2)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return Response(
        buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=\"landcheck-import-template.xlsx\""},
    )


@router.post("/detect")
async def detect_spreadsheet(request: Request, estate_id: int = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Stateless preview of a file's sheets/headers before anything is created - lets the mapping
    step show real headers and suggested mappings without committing to an upload yet."""
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    require_estate_access(db, request, estate.organization_id, permission="integration.manage")
    if upload_extension(file) not in ALLOWED_IMPORT_EXTENSIONS:
        raise HTTPException(422, "Upload a .xlsx or .csv file")
    data = await read_limited_upload(file, max_bytes=MAX_IMPORT_UPLOAD_BYTES)
    try:
        sheets = data_import.parse_spreadsheet(data, file.filename or "upload")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"filename": file.filename, "sheets": {name: _sheet_summary(rows) for name, rows in sheets.items()}}


@router.post("")
async def create_import_batch(
    request: Request,
    estate_id: int = Form(...),
    mapping_json: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    estate = db.get(Estate, estate_id)
    if not estate:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, estate.organization_id, permission="integration.manage")
    if upload_extension(file) not in ALLOWED_IMPORT_EXTENSIONS:
        raise HTTPException(422, "Upload a .xlsx or .csv file")
    try:
        mapping_payload = ImportMappingSubmit(**json.loads(mapping_json))
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, f"Invalid mapping: {exc}")

    data = await read_limited_upload(file, max_bytes=MAX_IMPORT_UPLOAD_BYTES)
    try:
        sheets = data_import.parse_spreadsheet(data, file.filename or "upload")
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    if mapping_payload.opening_balance_cutoff is None and any(sheet.opening_balance for sheet in mapping_payload.sheets.values()):
        has_dated_transactions = any(not sheet.opening_balance and "payment" in sheet.entity_types for sheet in mapping_payload.sheets.values())
        if has_dated_transactions:
            raise HTTPException(422, "An opening-balance cutoff date is required when a batch mixes opening balances with dated transactions")

    normalized_records: list[dict] = []
    for sheet_name, sheet_mapping in mapping_payload.sheets.items():
        rows = sheets.get(sheet_name, [])
        normalized_records.extend(data_import.build_normalized_records(rows, sheet_mapping.mapping, sheet_mapping.entity_types, is_opening_balance=sheet_mapping.opening_balance))
    if not normalized_records:
        raise HTTPException(422, "No records could be read from the selected sheets with this mapping")

    batch = EstateImportBatch(
        organization_id=estate.organization_id,
        estate_id=estate.id,
        source_filename=file.filename or "upload",
        status="previewed",
        column_mapping={name: sheet.model_dump() for name, sheet in mapping_payload.sheets.items()},
        opening_balance_cutoff=mapping_payload.opening_balance_cutoff,
        uploaded_by_subject_type=access.principal.subject_type,
        uploaded_by_subject_id=access.principal.subject_id,
    )
    db.add(batch)
    db.flush()

    results = data_import.match_and_validate_rows(db, organization_id=estate.organization_id, estate=estate, normalized_records=normalized_records, day_first=mapping_payload.day_first)
    counts = {"create": 0, "update": 0, "review": 0, "skip": 0}
    for result in results:
        row = EstateImportRow(
            batch_id=batch.id,
            row_index=result.row_index,
            source_data=result.source_data,
            match_status=result.match_status,
            matched_entity_type=result.matched_entity_type,
            matched_entity_id=result.matched_entity_id,
            resolution=result.resolution,
            issues=result.issues,
        )
        db.add(row)
        if result.match_status in ("ambiguous", "conflict", "error"):
            counts["review"] += 1
        elif result.resolution == "skip":
            counts["skip"] += 1
        elif result.match_status == "matched":
            counts["update"] += 1
        else:
            counts["create"] += 1
    batch.row_counts = counts

    if mapping_payload.save_preset_name:
        for sheet_name, sheet_mapping in mapping_payload.sheets.items():
            db.add(EstateImportMappingPreset(organization_id=estate.organization_id, name=f"{mapping_payload.save_preset_name} - {sheet_name}", entity_type=",".join(sheet_mapping.entity_types), mapping=sheet_mapping.mapping))

    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="import_batch.uploaded", entity_type="estate_import_batch", entity_id=batch.id, after_data={"filename": batch.source_filename, "row_count": len(results), **counts})
    db.commit()
    return {"id": batch.id, "batch_uid": batch.batch_uid, "status": batch.status, "row_counts": counts}


def _get_batch(db: Session, request: Request, batch_id: int, *, permission: str) -> EstateImportBatch:
    batch = db.get(EstateImportBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Import batch not found")
    require_estate_access(db, request, batch.organization_id, permission=permission)
    return batch


@router.get("/{batch_id}/preview")
def get_import_preview(batch_id: int, request: Request, status: str | None = None, page: int = 1, page_size: int = 50, db: Session = Depends(get_db)):
    batch = _get_batch(db, request, batch_id, permission="integration.read")
    query = db.query(EstateImportRow).filter(EstateImportRow.batch_id == batch.id)
    if status == "review":
        query = query.filter(EstateImportRow.match_status.in_(("ambiguous", "conflict", "error")))
    elif status in ("create", "update", "skip"):
        query = query.filter(EstateImportRow.resolution == status, EstateImportRow.match_status.notin_(("ambiguous", "conflict", "error")))
    total = query.count()
    rows = query.order_by(EstateImportRow.row_index).offset(max(page - 1, 0) * min(max(page_size, 1), 200)).limit(min(max(page_size, 1), 200)).all()
    plot_ids = {r.manual_plot_id for r in rows if r.manual_plot_id}
    plot_numbers = {p.id: p.plot_number for p in db.query(EstatePlot).filter(EstatePlot.id.in_(plot_ids)).all()} if plot_ids else {}
    return {
        "batch": {"id": batch.id, "status": batch.status, "source_filename": batch.source_filename, "row_counts": batch.row_counts, "created_at": batch.created_at},
        "page": page,
        "total": total,
        "items": [{"id": r.id, "row_index": r.row_index, "source_data": r.source_data, "match_status": r.match_status, "resolution": r.resolution, "issues": r.issues, "manual_plot_id": r.manual_plot_id, "manual_plot_number": plot_numbers.get(r.manual_plot_id)} for r in rows],
    }


@router.get("/{batch_id}/plot-options")
def search_batch_plots(batch_id: int, request: Request, q: str = "", db: Session = Depends(get_db)):
    """Existing plots in this batch's estate, for the review step's "link an existing plot"
    picker - plots are never created from a spreadsheet row, only matched against real ones."""
    batch = _get_batch(db, request, batch_id, permission="integration.read")
    query = db.query(EstatePlot).filter(EstatePlot.estate_id == batch.estate_id)
    term = q.strip()
    if term:
        query = query.filter(EstatePlot.plot_number.ilike(f"%{term}%"))
    plots = query.order_by(EstatePlot.plot_number_normalized).limit(25).all()
    return {"rows": [{"id": p.id, "plot_number": p.plot_number, "commercial_status": p.commercial_status, "geometry_status": p.geometry_status} for p in plots]}


@router.patch("/{batch_id}/rows/{row_id}")
def override_import_row(batch_id: int, row_id: int, payload: ImportRowOverride, request: Request, db: Session = Depends(get_db)):
    batch = _get_batch(db, request, batch_id, permission="integration.manage")
    row = db.query(EstateImportRow).filter(EstateImportRow.id == row_id, EstateImportRow.batch_id == batch.id).one_or_none()
    if not row:
        raise HTTPException(404, "Row not found")
    if payload.plot_id is not None:
        plot = db.query(EstatePlot).filter(EstatePlot.id == payload.plot_id, EstatePlot.estate_id == batch.estate_id).one_or_none()
        if not plot:
            raise HTTPException(422, "That plot was not found in this estate")
        row.manual_plot_id = plot.id
        row.issues = [issue for issue in (row.issues or []) if "plot" not in issue.lower()]
    if payload.resolution is not None:
        row.resolution = payload.resolution
    db.commit()
    return {"id": row.id, "resolution": row.resolution, "manual_plot_id": row.manual_plot_id}


@router.post("/{batch_id}/commit")
def commit_import_batch(batch_id: int, payload: ImportCommitRequest, request: Request, db: Session = Depends(get_db)):
    batch = _get_batch(db, request, batch_id, permission="integration.manage")
    access = require_estate_access(db, request, batch.organization_id, permission="integration.manage")
    if batch.status == "committed":
        raise HTTPException(409, "This batch has already been committed")
    query = db.query(EstateImportRow).filter(EstateImportRow.batch_id == batch.id, EstateImportRow.resolution != "skip")
    if payload.row_ids is not None:
        query = query.filter(EstateImportRow.id.in_(payload.row_ids))
    rows = query.order_by(EstateImportRow.row_index).all()
    outcome = data_import.commit_import_rows(db, batch=batch, rows=rows, actor=access.principal)
    batch.status = "committed"
    batch.committed_at = datetime.now(timezone.utc)
    append_estate_audit_event(db, organization_id=batch.organization_id, actor=access.principal, action="import_batch.committed", entity_type="estate_import_batch", entity_id=batch.id, after_data={"created": outcome.created, "updated": outcome.updated, "skipped": outcome.skipped, "errors": len(outcome.errors)})
    db.commit()
    return {"id": batch.id, "status": batch.status, "created": outcome.created, "updated": outcome.updated, "skipped": outcome.skipped, "errors": outcome.errors}


@router.get("/{batch_id}/errors.csv")
def download_import_errors(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = _get_batch(db, request, batch_id, permission="integration.read")
    rows = db.query(EstateImportRow).filter(EstateImportRow.batch_id == batch.id, EstateImportRow.issues != []).order_by(EstateImportRow.row_index).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Row", "Match status", "Resolution", "Issues", "Source data"])
    for row in rows:
        writer.writerow([row.row_index + 1, row.match_status, row.resolution, "; ".join(row.issues or []), json.dumps(row.source_data)])
    return Response(buffer.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="import-batch-{batch.batch_uid}-errors.csv"'})


@router.post("/{batch_id}/reverse")
def reverse_import_batch(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = _get_batch(db, request, batch_id, permission="integration.manage")
    access = require_estate_access(db, request, batch.organization_id, permission="integration.manage")
    if batch.status != "committed":
        raise HTTPException(409, "Only a committed batch can be reversed")
    blockers = data_import.reverse_import_batch(db, batch=batch, actor=access.principal)
    if blockers:
        db.rollback()
        raise HTTPException(409, {"message": "This batch cannot be reversed", "blockers": blockers})
    batch.status = "reversed"
    batch.reversed_at = datetime.now(timezone.utc)
    batch.reversed_by_subject_type = access.principal.subject_type
    batch.reversed_by_subject_id = access.principal.subject_id
    append_estate_audit_event(db, organization_id=batch.organization_id, actor=access.principal, action="import_batch.reversed", entity_type="estate_import_batch", entity_id=batch.id)
    db.commit()
    return {"id": batch.id, "status": batch.status}


@router.get("")
def list_import_batches(request: Request, db: Session = Depends(get_db)):
    principal = resolve_estate_principal(db, request)
    allowed = {a.organization_id for a in list_estate_access(db, principal) if has_permission(a.role_key, "integration.read")}
    batches = db.query(EstateImportBatch).filter(EstateImportBatch.organization_id.in_(allowed)).order_by(EstateImportBatch.created_at.desc()).limit(100).all()
    estates_by_id = {e.id: e.name for e in db.query(Estate).filter(Estate.id.in_({b.estate_id for b in batches})).all()} if batches else {}
    return {"rows": [{"id": b.id, "estate_name": estates_by_id.get(b.estate_id, "-"), "source_filename": b.source_filename, "status": b.status, "row_counts": b.row_counts, "created_at": b.created_at, "committed_at": b.committed_at} for b in batches]}


@router.get("/mapping-presets")
def list_mapping_presets(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="integration.read")
    presets = db.query(EstateImportMappingPreset).filter(EstateImportMappingPreset.organization_id == organization_id).order_by(EstateImportMappingPreset.created_at.desc()).all()
    return {"rows": [{"id": p.id, "name": p.name, "entity_type": p.entity_type, "mapping": p.mapping} for p in presets]}
