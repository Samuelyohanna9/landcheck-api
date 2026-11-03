from __future__ import annotations

"""Construction/build tracking for Estate developers who build houses themselves, not just sell
land - per-plot budgets, a contractor directory, and contractor draw requests. The draw-request
flow deliberately mirrors EstateSettlementRequest's shape (requested -> in_review/declined -> paid)
from estates.py's accounting endpoints: a read permission is enough to ask for a draw, a manage
permission is required to approve or record the real payment that actually reaches "paid"."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.estate_foundation import (
    Estate,
    EstateBillOfQuantities,
    EstateBOQLineItem,
    EstateConstructionBudgetLine,
    EstateConstructionDrawRequest,
    EstateContractor,
    EstateContractorPayment,
    EstateDocument,
    EstateDocumentLink,
    EstateDrawingAnnotation,
    EstateDrawingApproval,
    EstatePlot,
    EstatePlotConstructionBudget,
    EstateRFI,
    EstateSiteInspection,
    EstateSnagItem,
)
from app.routers.plots import get_db
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import require_estate_access, require_estate_access_for_estate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/estates", tags=["estate-construction"])

READ = "construction.read"
WRITE = "construction.manage"
DRAW_STATUSES = ("requested", "in_review", "declined", "paid")


def _plot_and_estate(db: Session, plot_id: int) -> tuple[EstatePlot, Estate]:
    plot = db.get(EstatePlot, plot_id)
    if plot is None:
        raise HTTPException(404, "Plot not found")
    estate = db.get(Estate, plot.estate_id)
    if estate is None:
        raise HTTPException(404, "Estate not found")
    return plot, estate


def _budget_payload(budget: EstatePlotConstructionBudget, lines: list[EstateConstructionBudgetLine]) -> dict:
    return {
        "id": budget.id, "plot_id": budget.plot_id, "total_budget": budget.total_budget, "currency": budget.currency,
        "lines": [
            {"id": line.id, "category": line.category, "budgeted_amount": line.budgeted_amount, "actual_amount": line.actual_amount, "notes": line.notes, "sort_order": line.sort_order}
            for line in sorted(lines, key=lambda item: (item.sort_order, item.id))
        ],
    }


# ── Schemas ──────────────────────────────────────────────────────────────────────────────────
class BudgetLineInput(BaseModel):
    id: int | None = None
    category: str = Field(min_length=1, max_length=120)
    budgeted_amount: float = Field(ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class BudgetUpsert(BaseModel):
    total_budget: float | None = Field(default=None, ge=0)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    lines: list[BudgetLineInput] = Field(default_factory=list)


class BOQLineInput(BaseModel):
    id: int | None = None
    category: str | None = Field(default=None, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    unit: str | None = Field(default=None, max_length=32)
    quantity: float = Field(ge=0)
    rate: float = Field(ge=0)


class BOQUpsert(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    lines: list[BOQLineInput] = Field(default_factory=list)


class AnnotationCreate(BaseModel):
    kind: str = Field(pattern="^(calibration|measurement|markup)$")
    data: dict = Field(default_factory=dict)
    label: str | None = Field(default=None, max_length=255)


class DrawingApprovalReview(BaseModel):
    status: str = Field(pattern="^(approved|revision_requested)$")
    note: str | None = Field(default=None, max_length=2000)


class RFICreate(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class RFIAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


class SiteInspectionCreate(BaseModel):
    visit_date: datetime
    notes: str | None = Field(default=None, max_length=4000)
    phase_at_inspection: str | None = Field(default=None, max_length=32)
    signed_off: bool = False


class SnagItemCreate(BaseModel):
    description: str = Field(min_length=1, max_length=4000)
    assigned_to_contractor_id: int | None = None


class ContractorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    trade: str | None = Field(default=None, max_length=120)
    contact_phone: str | None = Field(default=None, max_length=32)
    contact_email: str | None = Field(default=None, max_length=255)
    payment_terms: str | None = Field(default=None, max_length=2000)


class DrawRequestCreate(BaseModel):
    budget_line_id: int
    contractor_id: int
    amount_requested: float = Field(gt=0)
    percent_complete_claimed: int | None = Field(default=None, ge=0, le=100)
    retention_amount: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=2000)


class DrawRequestReview(BaseModel):
    status: str = Field(pattern="^(in_review|declined)$")
    note: str | None = Field(default=None, max_length=2000)


class DrawRequestPay(BaseModel):
    amount: float = Field(gt=0)
    payment_date: datetime
    payment_method: str = Field(min_length=1, max_length=80)
    reference_no: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=2000)


# ── Budgets ──────────────────────────────────────────────────────────────────────────────────
@router.get("/plots/{plot_id}/construction-budget")
def get_construction_budget(plot_id: int, request: Request, db: Session = Depends(get_db)):
    plot, estate = _plot_and_estate(db, plot_id)
    require_estate_access_for_estate(db, request, estate.id, permission=READ)
    budget = db.query(EstatePlotConstructionBudget).filter(EstatePlotConstructionBudget.plot_id == plot_id).one_or_none()
    if budget is None:
        return {"id": None, "plot_id": plot_id, "total_budget": None, "currency": "NGN", "lines": []}
    lines = db.query(EstateConstructionBudgetLine).filter(EstateConstructionBudgetLine.budget_id == budget.id).all()
    return _budget_payload(budget, lines)


@router.put("/plots/{plot_id}/construction-budget")
def upsert_construction_budget(plot_id: int, payload: BudgetUpsert, request: Request, db: Session = Depends(get_db)):
    """Replaces the full line list in one call - simpler and safer than a line-by-line patch API
    for what's normally edited as one budget sheet, same shape as the import-wizard's row-commit
    pattern elsewhere in this product."""
    plot, estate = _plot_and_estate(db, plot_id)
    access = require_estate_access_for_estate(db, request, estate.id, permission=WRITE)
    budget = db.query(EstatePlotConstructionBudget).filter(EstatePlotConstructionBudget.plot_id == plot_id).one_or_none()
    is_new = budget is None
    if budget is None:
        budget = EstatePlotConstructionBudget(
            plot_id=plot_id, organization_id=estate.organization_id,
            created_by_subject_type=access.principal.subject_type, created_by_subject_id=access.principal.subject_id,
        )
        db.add(budget)
        db.flush()
    budget.total_budget = payload.total_budget
    budget.currency = payload.currency.upper()

    existing = {line.id: line for line in db.query(EstateConstructionBudgetLine).filter(EstateConstructionBudgetLine.budget_id == budget.id).all()}
    kept_ids: set[int] = set()
    for index, item in enumerate(payload.lines):
        if item.id is not None and item.id in existing:
            line = existing[item.id]
            line.category, line.budgeted_amount, line.notes, line.sort_order = item.category, item.budgeted_amount, item.notes, index
            kept_ids.add(item.id)
        else:
            line = EstateConstructionBudgetLine(budget_id=budget.id, category=item.category, budgeted_amount=item.budgeted_amount, notes=item.notes, sort_order=index)
            db.add(line)
    for line_id, line in existing.items():
        if line_id not in kept_ids:
            db.delete(line)

    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="construction_budget.created" if is_new else "construction_budget.updated", entity_type="estate_plot_construction_budget", entity_id=budget.id, after_data={"total_budget": payload.total_budget, "line_count": len(payload.lines)})
    db.commit()
    lines = db.query(EstateConstructionBudgetLine).filter(EstateConstructionBudgetLine.budget_id == budget.id).all()
    return _budget_payload(budget, lines)


# ── Bill of Quantities ───────────────────────────────────────────────────────────────────────
def _boq_payload(boq: EstateBillOfQuantities, lines: list[EstateBOQLineItem]) -> dict:
    return {
        "id": boq.id, "plot_id": boq.plot_id, "title": boq.title,
        "lines": [
            {"id": line.id, "category": line.category, "description": line.description, "unit": line.unit, "quantity": line.quantity, "rate": line.rate, "amount": line.amount}
            for line in sorted(lines, key=lambda item: (item.sort_order, item.id))
        ],
        "total": sum((line.amount or 0) for line in lines),
    }


@router.get("/plots/{plot_id}/boq")
def get_boq(plot_id: int, request: Request, db: Session = Depends(get_db)):
    plot, estate = _plot_and_estate(db, plot_id)
    require_estate_access_for_estate(db, request, estate.id, permission=READ)
    boq = db.query(EstateBillOfQuantities).filter(EstateBillOfQuantities.plot_id == plot_id).one_or_none()
    if boq is None:
        return {"id": None, "plot_id": plot_id, "title": None, "lines": [], "total": 0}
    lines = db.query(EstateBOQLineItem).filter(EstateBOQLineItem.boq_id == boq.id).all()
    return _boq_payload(boq, lines)


@router.put("/plots/{plot_id}/boq")
def upsert_boq(plot_id: int, payload: BOQUpsert, request: Request, db: Session = Depends(get_db)):
    plot, estate = _plot_and_estate(db, plot_id)
    access = require_estate_access_for_estate(db, request, estate.id, permission=WRITE)
    boq = db.query(EstateBillOfQuantities).filter(EstateBillOfQuantities.plot_id == plot_id).one_or_none()
    is_new = boq is None
    if boq is None:
        boq = EstateBillOfQuantities(plot_id=plot_id, organization_id=estate.organization_id, created_by_subject_type=access.principal.subject_type, created_by_subject_id=access.principal.subject_id)
        db.add(boq)
        db.flush()
    boq.title = payload.title

    existing = {line.id: line for line in db.query(EstateBOQLineItem).filter(EstateBOQLineItem.boq_id == boq.id).all()}
    kept_ids: set[int] = set()
    for index, item in enumerate(payload.lines):
        amount = round(item.quantity * item.rate, 2)
        if item.id is not None and item.id in existing:
            line = existing[item.id]
            line.category, line.description, line.unit, line.quantity, line.rate, line.amount, line.sort_order = item.category, item.description, item.unit, item.quantity, item.rate, amount, index
            kept_ids.add(item.id)
        else:
            db.add(EstateBOQLineItem(boq_id=boq.id, category=item.category, description=item.description, unit=item.unit, quantity=item.quantity, rate=item.rate, amount=amount, sort_order=index))
    for line_id, line in existing.items():
        if line_id not in kept_ids:
            db.delete(line)

    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="boq.created" if is_new else "boq.updated", entity_type="estate_bill_of_quantities", entity_id=boq.id, after_data={"line_count": len(payload.lines)})
    db.commit()
    lines = db.query(EstateBOQLineItem).filter(EstateBOQLineItem.boq_id == boq.id).all()
    return _boq_payload(boq, lines)


@router.post("/plots/{plot_id}/boq/apply-to-budget")
def apply_boq_to_budget(plot_id: int, request: Request, db: Session = Depends(get_db)):
    """Seeds (or replaces) the plot's construction budget lines from its current BOQ, grouped by
    category - the gap this feature exists to close: BillMate's offline BOQ tool has no connection
    to payments at all, so a BOQ here stays disconnected from spend unless something does this."""
    plot, estate = _plot_and_estate(db, plot_id)
    access = require_estate_access_for_estate(db, request, estate.id, permission=WRITE)
    boq = db.query(EstateBillOfQuantities).filter(EstateBillOfQuantities.plot_id == plot_id).one_or_none()
    if boq is None:
        raise HTTPException(404, "No BOQ exists for this plot yet")
    boq_lines = db.query(EstateBOQLineItem).filter(EstateBOQLineItem.boq_id == boq.id).all()
    if not boq_lines:
        raise HTTPException(422, "This BOQ has no line items yet")

    totals_by_category: dict[str, float] = {}
    for line in boq_lines:
        key = (line.category or "Uncategorised").strip() or "Uncategorised"
        totals_by_category[key] = totals_by_category.get(key, 0) + float(line.amount or 0)

    budget = db.query(EstatePlotConstructionBudget).filter(EstatePlotConstructionBudget.plot_id == plot_id).one_or_none()
    if budget is None:
        budget = EstatePlotConstructionBudget(plot_id=plot_id, organization_id=estate.organization_id, created_by_subject_type=access.principal.subject_type, created_by_subject_id=access.principal.subject_id)
        db.add(budget)
        db.flush()
    budget.total_budget = sum(totals_by_category.values())

    existing_lines = {line.category: line for line in db.query(EstateConstructionBudgetLine).filter(EstateConstructionBudgetLine.budget_id == budget.id).all()}
    for index, (category, amount) in enumerate(totals_by_category.items()):
        if category in existing_lines:
            existing_lines[category].budgeted_amount = amount
        else:
            db.add(EstateConstructionBudgetLine(budget_id=budget.id, category=category, budgeted_amount=amount, sort_order=index))

    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="boq.applied_to_budget", entity_type="estate_plot_construction_budget", entity_id=budget.id, after_data={"category_count": len(totals_by_category), "total": budget.total_budget})
    db.commit()
    lines = db.query(EstateConstructionBudgetLine).filter(EstateConstructionBudgetLine.budget_id == budget.id).all()
    return _budget_payload(budget, lines)


# ── Contractors ──────────────────────────────────────────────────────────────────────────────
@router.get("/organizations/{organization_id}/contractors")
def list_contractors(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission=READ)
    rows = db.query(EstateContractor).filter(EstateContractor.organization_id == organization_id, EstateContractor.is_active.is_(True)).order_by(EstateContractor.name.asc()).all()
    return [{"id": row.id, "name": row.name, "trade": row.trade, "contact_phone": row.contact_phone, "contact_email": row.contact_email, "payment_terms": row.payment_terms} for row in rows]


@router.post("/organizations/{organization_id}/contractors", status_code=201)
def create_contractor(organization_id: int, payload: ContractorCreate, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission=WRITE)
    contractor = EstateContractor(
        organization_id=organization_id, name=payload.name.strip(), trade=payload.trade, contact_phone=payload.contact_phone,
        contact_email=payload.contact_email, payment_terms=payload.payment_terms,
        created_by_subject_type=access.principal.subject_type, created_by_subject_id=access.principal.subject_id,
    )
    db.add(contractor)
    db.flush()
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="contractor.created", entity_type="estate_contractor", entity_id=contractor.id, after_data={"name": contractor.name, "trade": contractor.trade})
    db.commit()
    return {"id": contractor.id, "name": contractor.name}


@router.delete("/contractors/{contractor_id}")
def deactivate_contractor(contractor_id: int, request: Request, db: Session = Depends(get_db)):
    contractor = db.get(EstateContractor, contractor_id)
    if contractor is None:
        raise HTTPException(404, "Contractor not found")
    access = require_estate_access(db, request, contractor.organization_id, permission=WRITE)
    contractor.is_active = False
    append_estate_audit_event(db, organization_id=contractor.organization_id, actor=access.principal, action="contractor.deactivated", entity_type="estate_contractor", entity_id=contractor.id)
    db.commit()
    return {"id": contractor.id, "is_active": False}


# ── Draw requests ────────────────────────────────────────────────────────────────────────────
def _draw_payload(draw: EstateConstructionDrawRequest) -> dict:
    return {
        "id": draw.id, "request_uid": draw.request_uid, "budget_line_id": draw.budget_line_id, "contractor_id": draw.contractor_id,
        "amount_requested": draw.amount_requested, "percent_complete_claimed": draw.percent_complete_claimed, "retention_amount": draw.retention_amount,
        "note": draw.note, "status": draw.status, "created_at": draw.created_at, "reviewed_at": draw.reviewed_at, "paid_at": draw.paid_at,
    }


@router.get("/organizations/{organization_id}/construction-draw-requests")
def list_draw_requests(organization_id: int, request: Request, status: str | None = None, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission=READ)
    query = db.query(EstateConstructionDrawRequest).filter(EstateConstructionDrawRequest.organization_id == organization_id)
    if status:
        if status not in DRAW_STATUSES:
            raise HTTPException(422, "Unknown status")
        query = query.filter(EstateConstructionDrawRequest.status == status)
    rows = query.order_by(EstateConstructionDrawRequest.created_at.desc()).limit(200).all()
    return [_draw_payload(row) for row in rows]


@router.post("/organizations/{organization_id}/construction-draw-requests", status_code=201)
def create_draw_request(organization_id: int, payload: DrawRequestCreate, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission=READ)
    line = db.get(EstateConstructionBudgetLine, payload.budget_line_id)
    budget = db.get(EstatePlotConstructionBudget, line.budget_id) if line else None
    if line is None or budget is None or budget.organization_id != organization_id:
        raise HTTPException(404, "Budget line not found")
    contractor = db.get(EstateContractor, payload.contractor_id)
    if contractor is None or contractor.organization_id != organization_id:
        raise HTTPException(404, "Contractor not found")
    draw = EstateConstructionDrawRequest(
        organization_id=organization_id, budget_line_id=payload.budget_line_id, contractor_id=payload.contractor_id,
        amount_requested=payload.amount_requested, percent_complete_claimed=payload.percent_complete_claimed,
        retention_amount=payload.retention_amount, note=payload.note,
        requested_by_subject_type=access.principal.subject_type, requested_by_subject_id=access.principal.subject_id,
    )
    db.add(draw)
    db.flush()
    append_estate_audit_event(db, organization_id=organization_id, actor=access.principal, action="construction_draw_request.created", entity_type="estate_construction_draw_request", entity_id=draw.id, after_data={"amount_requested": payload.amount_requested, "contractor_id": payload.contractor_id})
    db.commit()
    return _draw_payload(draw)


@router.patch("/construction-draw-requests/{draw_id}")
def review_draw_request(draw_id: int, payload: DrawRequestReview, request: Request, db: Session = Depends(get_db)):
    draw = db.get(EstateConstructionDrawRequest, draw_id)
    if draw is None:
        raise HTTPException(404, "Draw request not found")
    access = require_estate_access(db, request, draw.organization_id, permission=WRITE)
    if draw.status not in ("requested", "in_review"):
        raise HTTPException(409, "This draw request has already been actioned")
    before = draw.status
    draw.status = payload.status
    draw.reviewed_at = datetime.now(timezone.utc)
    draw.reviewed_by_subject_type = access.principal.subject_type
    draw.reviewed_by_subject_id = access.principal.subject_id
    if payload.note:
        draw.note = f"{draw.note}\n\n[Review] {payload.note}" if draw.note else f"[Review] {payload.note}"
    append_estate_audit_event(db, organization_id=draw.organization_id, actor=access.principal, action="construction_draw_request.reviewed", entity_type="estate_construction_draw_request", entity_id=draw.id, before_data={"status": before}, after_data={"status": draw.status})
    db.commit()
    return _draw_payload(draw)


@router.post("/construction-draw-requests/{draw_id}/pay")
def pay_draw_request(draw_id: int, payload: DrawRequestPay, request: Request, db: Session = Depends(get_db)):
    """Records a real contractor payment against an approved draw - this is the only path that
    moves a draw to 'paid'. Contractor payments are typically made by bank transfer outside the
    app, so this records what happened rather than triggering a transfer the way the Flutterwave
    settlement flow does."""
    draw = db.get(EstateConstructionDrawRequest, draw_id)
    if draw is None:
        raise HTTPException(404, "Draw request not found")
    access = require_estate_access(db, request, draw.organization_id, permission=WRITE)
    if draw.status not in ("requested", "in_review"):
        raise HTTPException(409, "This draw request cannot be paid from its current status")
    payment = EstateContractorPayment(
        organization_id=draw.organization_id, draw_request_id=draw.id, contractor_id=draw.contractor_id,
        amount=payload.amount, payment_date=payload.payment_date, payment_method=payload.payment_method,
        reference_no=payload.reference_no, notes=payload.notes,
        recorded_by_subject_type=access.principal.subject_type, recorded_by_subject_id=access.principal.subject_id,
    )
    db.add(payment)
    draw.status = "paid"
    draw.paid_at = datetime.now(timezone.utc)
    draw.reviewed_at = draw.reviewed_at or draw.paid_at
    draw.reviewed_by_subject_type = draw.reviewed_by_subject_type or access.principal.subject_type
    draw.reviewed_by_subject_id = draw.reviewed_by_subject_id or access.principal.subject_id
    line = db.get(EstateConstructionBudgetLine, draw.budget_line_id)
    if line is not None:
        line.actual_amount = (line.actual_amount or 0) + payload.amount
    db.flush()
    append_estate_audit_event(db, organization_id=draw.organization_id, actor=access.principal, action="construction_draw_request.paid", entity_type="estate_construction_draw_request", entity_id=draw.id, after_data={"amount": payload.amount, "payment_id": payment.id})
    db.commit()
    return _draw_payload(draw)


# ── Snag list ────────────────────────────────────────────────────────────────────────────────
def _snag_payload(snag: EstateSnagItem) -> dict:
    return {
        "id": snag.id, "plot_id": snag.plot_id, "description": snag.description, "status": snag.status,
        "assigned_to_contractor_id": snag.assigned_to_contractor_id, "created_at": snag.created_at,
        "fixed_at": snag.fixed_at, "verified_at": snag.verified_at,
    }


@router.get("/plots/{plot_id}/snag-items")
def list_snag_items(plot_id: int, request: Request, db: Session = Depends(get_db)):
    plot, estate = _plot_and_estate(db, plot_id)
    require_estate_access_for_estate(db, request, estate.id, permission=READ)
    rows = db.query(EstateSnagItem).filter(EstateSnagItem.plot_id == plot_id).order_by(EstateSnagItem.created_at.desc()).all()
    return [_snag_payload(row) for row in rows]


@router.post("/plots/{plot_id}/snag-items", status_code=201)
def create_snag_item(plot_id: int, payload: SnagItemCreate, request: Request, db: Session = Depends(get_db)):
    plot, estate = _plot_and_estate(db, plot_id)
    access = require_estate_access_for_estate(db, request, estate.id, permission=WRITE)
    snag = EstateSnagItem(
        organization_id=estate.organization_id, plot_id=plot_id, description=payload.description.strip(),
        assigned_to_contractor_id=payload.assigned_to_contractor_id,
        raised_by_subject_type=access.principal.subject_type, raised_by_subject_id=access.principal.subject_id,
    )
    db.add(snag)
    db.flush()
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="snag_item.created", entity_type="estate_snag_item", entity_id=snag.id, after_data={"description": snag.description})
    db.commit()
    return _snag_payload(snag)


@router.post("/snag-items/{snag_id}/mark-fixed")
def mark_snag_fixed(snag_id: int, request: Request, db: Session = Depends(get_db)):
    """Anyone with construction.manage can mark a snag fixed (usually a contractor's own report
    relayed by staff). Verifying it closed is a separate, stricter step - see Phase 11's plan for
    restricting that to the Architect role once it exists."""
    snag = db.get(EstateSnagItem, snag_id)
    if snag is None:
        raise HTTPException(404, "Snag item not found")
    access = require_estate_access(db, request, snag.organization_id, permission=WRITE)
    if snag.status != "open":
        raise HTTPException(409, "This snag item isn't open")
    snag.status = "fixed"
    snag.fixed_at = datetime.now(timezone.utc)
    append_estate_audit_event(db, organization_id=snag.organization_id, actor=access.principal, action="snag_item.marked_fixed", entity_type="estate_snag_item", entity_id=snag.id)
    db.commit()
    return _snag_payload(snag)


@router.post("/snag-items/{snag_id}/verify")
def verify_snag_item(snag_id: int, request: Request, db: Session = Depends(get_db)):
    snag = db.get(EstateSnagItem, snag_id)
    if snag is None:
        raise HTTPException(404, "Snag item not found")
    access = require_estate_access(db, request, snag.organization_id, permission=WRITE)
    if snag.status != "fixed":
        raise HTTPException(409, "Only a fixed snag item can be verified")
    snag.status = "verified"
    snag.verified_at = datetime.now(timezone.utc)
    snag.verified_by_subject_type = access.principal.subject_type
    snag.verified_by_subject_id = access.principal.subject_id
    append_estate_audit_event(db, organization_id=snag.organization_id, actor=access.principal, action="snag_item.verified", entity_type="estate_snag_item", entity_id=snag.id)
    db.commit()
    return _snag_payload(snag)


# ── Drawing approvals ────────────────────────────────────────────────────────────────────────
DRAWING_TYPES = ("building_plan", "structural_drawing", "mep_drawing")


def _drawing_payload(document: EstateDocument, approval: EstateDrawingApproval | None) -> dict:
    return {
        "document_id": document.id, "filename": document.original_filename, "document_type": document.document_type,
        "supersedes_document_id": document.supersedes_document_id, "created_at": document.created_at,
        "approval_status": approval.status if approval else None, "approval_note": approval.note if approval else None,
        "reviewed_at": approval.reviewed_at if approval else None,
    }


@router.get("/plots/{plot_id}/drawings")
def list_plot_drawings(plot_id: int, request: Request, db: Session = Depends(get_db)):
    """Every document of a drawing type linked to this plot, with its approval state if any -
    uploading itself still goes through the existing generic POST /estates/documents endpoint
    (entity_type="plot", document_type one of DRAWING_TYPES); this just surfaces them together."""
    plot, estate = _plot_and_estate(db, plot_id)
    require_estate_access_for_estate(db, request, estate.id, permission=READ)
    document_ids = [row.document_id for row in db.query(EstateDocumentLink).filter(EstateDocumentLink.entity_type == "plot", EstateDocumentLink.entity_id == str(plot_id)).all()]
    if not document_ids:
        return []
    documents = db.query(EstateDocument).filter(EstateDocument.id.in_(document_ids), EstateDocument.document_type.in_(DRAWING_TYPES)).order_by(EstateDocument.created_at.desc()).all()
    approvals = {row.document_id: row for row in db.query(EstateDrawingApproval).filter(EstateDrawingApproval.document_id.in_([doc.id for doc in documents])).all()}
    return [_drawing_payload(doc, approvals.get(doc.id)) for doc in documents]


@router.post("/documents/{document_id}/submit-for-approval")
def submit_drawing_for_approval(document_id: int, request: Request, db: Session = Depends(get_db)):
    document = db.get(EstateDocument, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    if document.document_type not in DRAWING_TYPES:
        raise HTTPException(422, "Only drawing-type documents (building plan, structural, MEP) go through approval")
    access = require_estate_access(db, request, document.organization_id, permission=WRITE)
    existing = db.query(EstateDrawingApproval).filter(EstateDrawingApproval.document_id == document_id).one_or_none()
    if existing is not None:
        raise HTTPException(409, "This drawing already has an approval record")
    approval = EstateDrawingApproval(document_id=document_id, organization_id=document.organization_id, submitted_by_subject_type=access.principal.subject_type, submitted_by_subject_id=access.principal.subject_id)
    db.add(approval)
    db.flush()
    append_estate_audit_event(db, organization_id=document.organization_id, actor=access.principal, action="drawing_approval.submitted", entity_type="estate_drawing_approval", entity_id=approval.id, after_data={"document_id": document_id})
    db.commit()
    return _drawing_payload(document, approval)


@router.post("/documents/{document_id}/review-approval")
def review_drawing_approval(document_id: int, payload: DrawingApprovalReview, request: Request, db: Session = Depends(get_db)):
    document = db.get(EstateDocument, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    approval = db.query(EstateDrawingApproval).filter(EstateDrawingApproval.document_id == document_id).one_or_none()
    if approval is None:
        raise HTTPException(404, "This drawing hasn't been submitted for approval yet")
    access = require_estate_access(db, request, document.organization_id, permission=WRITE)
    if approval.status != "submitted":
        raise HTTPException(409, "This drawing has already been reviewed")
    approval.status = payload.status
    approval.note = payload.note
    approval.reviewed_at = datetime.now(timezone.utc)
    approval.reviewed_by_subject_type = access.principal.subject_type
    approval.reviewed_by_subject_id = access.principal.subject_id
    append_estate_audit_event(db, organization_id=document.organization_id, actor=access.principal, action="drawing_approval.reviewed", entity_type="estate_drawing_approval", entity_id=approval.id, after_data={"status": payload.status})
    db.commit()
    return _drawing_payload(document, approval)


# ── Drawing annotations (live calibration, measurement, markup) ────────────────────────────────
def _annotation_payload(row: EstateDrawingAnnotation) -> dict:
    return {"id": row.id, "document_id": row.document_id, "kind": row.kind, "data": row.data, "label": row.label, "created_at": row.created_at}


@router.get("/documents/{document_id}/annotations")
def list_annotations(document_id: int, request: Request, db: Session = Depends(get_db)):
    document = db.get(EstateDocument, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    require_estate_access(db, request, document.organization_id, permission=READ)
    rows = db.query(EstateDrawingAnnotation).filter(EstateDrawingAnnotation.document_id == document_id).order_by(EstateDrawingAnnotation.created_at.asc()).all()
    return [_annotation_payload(row) for row in rows]


@router.post("/documents/{document_id}/annotations", status_code=201)
def create_annotation(document_id: int, payload: AnnotationCreate, request: Request, db: Session = Depends(get_db)):
    """Saves one calibration line, one measurement, or one markup pin. The live measurement tool
    reads the document's most recent 'calibration' row (by created_at) to convert pixel distances
    into real-world units before saving a 'measurement' row - calibrating again doesn't rewrite
    earlier measurements, it just changes which scale future ones use."""
    document = db.get(EstateDocument, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    access = require_estate_access(db, request, document.organization_id, permission=WRITE)
    row = EstateDrawingAnnotation(
        document_id=document_id, organization_id=document.organization_id, kind=payload.kind, data=payload.data, label=payload.label,
        created_by_subject_type=access.principal.subject_type, created_by_subject_id=access.principal.subject_id,
    )
    db.add(row)
    db.flush()
    append_estate_audit_event(db, organization_id=document.organization_id, actor=access.principal, action="drawing_annotation.created", entity_type="estate_drawing_annotation", entity_id=row.id, after_data={"kind": payload.kind})
    db.commit()
    return _annotation_payload(row)


@router.delete("/annotations/{annotation_id}")
def delete_annotation(annotation_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(EstateDrawingAnnotation, annotation_id)
    if row is None:
        raise HTTPException(404, "Annotation not found")
    access = require_estate_access(db, request, row.organization_id, permission=WRITE)
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="drawing_annotation.deleted", entity_type="estate_drawing_annotation", entity_id=row.id)
    db.delete(row)
    db.commit()
    return {"deleted": True}


# ── RFIs ─────────────────────────────────────────────────────────────────────────────────────
def _rfi_payload(rfi: EstateRFI) -> dict:
    return {
        "id": rfi.id, "plot_id": rfi.plot_id, "question": rfi.question, "status": rfi.status,
        "created_at": rfi.created_at, "answer": rfi.answer, "answered_at": rfi.answered_at, "closed_at": rfi.closed_at,
    }


@router.get("/plots/{plot_id}/rfis")
def list_rfis(plot_id: int, request: Request, db: Session = Depends(get_db)):
    plot, estate = _plot_and_estate(db, plot_id)
    require_estate_access_for_estate(db, request, estate.id, permission=READ)
    rows = db.query(EstateRFI).filter(EstateRFI.plot_id == plot_id).order_by(EstateRFI.created_at.desc()).all()
    return [_rfi_payload(row) for row in rows]


@router.post("/plots/{plot_id}/rfis", status_code=201)
def create_rfi(plot_id: int, payload: RFICreate, request: Request, db: Session = Depends(get_db)):
    plot, estate = _plot_and_estate(db, plot_id)
    access = require_estate_access_for_estate(db, request, estate.id, permission=READ)
    rfi = EstateRFI(organization_id=estate.organization_id, plot_id=plot_id, question=payload.question.strip(), raised_by_subject_type=access.principal.subject_type, raised_by_subject_id=access.principal.subject_id)
    db.add(rfi)
    db.flush()
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="rfi.created", entity_type="estate_rfi", entity_id=rfi.id, after_data={"question": rfi.question})
    db.commit()
    return _rfi_payload(rfi)


@router.post("/rfis/{rfi_id}/answer")
def answer_rfi(rfi_id: int, payload: RFIAnswer, request: Request, db: Session = Depends(get_db)):
    rfi = db.get(EstateRFI, rfi_id)
    if rfi is None:
        raise HTTPException(404, "RFI not found")
    access = require_estate_access(db, request, rfi.organization_id, permission=WRITE)
    if rfi.status == "closed":
        raise HTTPException(409, "This RFI is closed")
    rfi.answer = payload.answer.strip()
    rfi.status = "answered"
    rfi.answered_at = datetime.now(timezone.utc)
    rfi.answered_by_subject_type = access.principal.subject_type
    rfi.answered_by_subject_id = access.principal.subject_id
    append_estate_audit_event(db, organization_id=rfi.organization_id, actor=access.principal, action="rfi.answered", entity_type="estate_rfi", entity_id=rfi.id)
    db.commit()
    return _rfi_payload(rfi)


@router.post("/rfis/{rfi_id}/close")
def close_rfi(rfi_id: int, request: Request, db: Session = Depends(get_db)):
    rfi = db.get(EstateRFI, rfi_id)
    if rfi is None:
        raise HTTPException(404, "RFI not found")
    access = require_estate_access(db, request, rfi.organization_id, permission=WRITE)
    rfi.status = "closed"
    rfi.closed_at = datetime.now(timezone.utc)
    append_estate_audit_event(db, organization_id=rfi.organization_id, actor=access.principal, action="rfi.closed", entity_type="estate_rfi", entity_id=rfi.id)
    db.commit()
    return _rfi_payload(rfi)


# ── Site inspections ─────────────────────────────────────────────────────────────────────────
def _inspection_payload(inspection: EstateSiteInspection) -> dict:
    return {
        "id": inspection.id, "plot_id": inspection.plot_id, "visit_date": inspection.visit_date, "notes": inspection.notes,
        "phase_at_inspection": inspection.phase_at_inspection, "signed_off": inspection.signed_off, "created_at": inspection.created_at,
    }


@router.get("/plots/{plot_id}/site-inspections")
def list_site_inspections(plot_id: int, request: Request, db: Session = Depends(get_db)):
    plot, estate = _plot_and_estate(db, plot_id)
    require_estate_access_for_estate(db, request, estate.id, permission=READ)
    rows = db.query(EstateSiteInspection).filter(EstateSiteInspection.plot_id == plot_id).order_by(EstateSiteInspection.visit_date.desc()).all()
    return [_inspection_payload(row) for row in rows]


@router.post("/plots/{plot_id}/site-inspections", status_code=201)
def create_site_inspection(plot_id: int, payload: SiteInspectionCreate, request: Request, db: Session = Depends(get_db)):
    plot, estate = _plot_and_estate(db, plot_id)
    access = require_estate_access_for_estate(db, request, estate.id, permission=WRITE)
    inspection = EstateSiteInspection(
        organization_id=estate.organization_id, plot_id=plot_id, visit_date=payload.visit_date, notes=payload.notes,
        phase_at_inspection=payload.phase_at_inspection, signed_off=payload.signed_off,
        inspector_subject_type=access.principal.subject_type, inspector_subject_id=access.principal.subject_id,
    )
    db.add(inspection)
    db.flush()
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="site_inspection.created", entity_type="estate_site_inspection", entity_id=inspection.id, after_data={"phase_at_inspection": inspection.phase_at_inspection, "signed_off": inspection.signed_off})
    db.commit()
    return _inspection_payload(inspection)
