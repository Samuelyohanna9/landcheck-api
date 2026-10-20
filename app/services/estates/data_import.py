from __future__ import annotations

"""The "Connect your tools" Excel/CSV import feature (Integrations). Customers, plots, sales/
allocations and payments staged for review before anything touches the database, then committed
through the EXACT same service functions staff already use by hand - reserve_or_allocate(),
record_payment(), confirm_payment() - so an import can never bypass the rules those already
enforce (plot must be geometry-approved and available, a payment can't exceed the agreed price,
etc). Google Sheets (live OAuth sync) is a deliberately separate, later phase - this module only
handles a one-time file upload.

Plots are only ever MATCHED against existing EstatePlot rows, never created from a spreadsheet
row - per the product spec, inventing plot geometry from a row of text would be unsafe."""

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from dateutil import parser as dateutil_parser
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.estate_foundation import (
    Estate,
    EstateAllocation,
    EstateCustomer,
    EstateImportBatch,
    EstateImportRow,
    EstatePayment,
    EstatePlot,
)
from app.services.estates.allocations import release_allocation, reserve_or_allocate
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import EstatePrincipal
from app.services.estates.payments import confirm_payment, financial_summary, record_payment, void_payment

logger = logging.getLogger(__name__)

# ─── Column-mapping suggestions ──────────────────────────────────────────────────────────────

CUSTOMER_FIELD_SYNONYMS: dict[str, list[str]] = {
    "external_id": ["customer id", "client id", "cust id", "buyer id", "id"],
    "full_name": ["name", "client name", "customer name", "buyer", "buyer name", "full name"],
    "phone": ["phone", "phone number", "mobile", "mobile number", "tel", "telephone", "contact"],
    "email": ["email", "email address", "e mail"],
    "address": ["address", "home address", "residential address"],
    "notes": ["notes", "remarks", "comment", "comments"],
}
PLOT_FIELD_SYNONYMS: dict[str, list[str]] = {
    "block": ["block", "phase", "section"],
    "plot_number": ["plot no", "plot number", "plot", "unit no", "unit number", "lot no", "lot number"],
}
SALE_FIELD_SYNONYMS: dict[str, list[str]] = {
    "external_reference": ["contract no", "contract number", "sale ref", "sale reference", "agreement no", "agreement number"],
    "agreed_price": ["agreed price", "sale price", "price", "contract value", "total price"],
    "currency": ["currency"],
    "sale_date": ["sale date", "date of sale", "agreement date", "contract date"],
    "agent": ["agent", "sales agent", "sold by"],
    "status": ["status", "sale status", "allocation status"],
}
PAYMENT_FIELD_SYNONYMS: dict[str, list[str]] = {
    "reference_no": ["reference", "ref", "transaction id", "txn id", "receipt no", "receipt number"],
    "amount": ["amount paid", "amount", "payment amount", "amount received"],
    "payment_date": ["payment date", "date paid", "date of payment", "transaction date"],
    "method": ["method", "payment method", "mode of payment"],
    "outstanding": ["balance", "outstanding", "amount owed", "balance due"],
    "total_paid": ["total paid", "amount paid so far", "cumulative paid"],
}
FIELD_SYNONYMS_BY_ENTITY: dict[str, dict[str, list[str]]] = {
    "customer": CUSTOMER_FIELD_SYNONYMS,
    "plot": PLOT_FIELD_SYNONYMS,
    "sale": SALE_FIELD_SYNONYMS,
    "payment": PAYMENT_FIELD_SYNONYMS,
}


def _normalize_header(value: str) -> str:
    text = re.sub(r"[.\-_/]+", " ", str(value or "").strip().lower())
    return re.sub(r"\s+", " ", text).strip()


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def suggest_column_mapping(headers: list[str], entity_type: str) -> dict[str, str | None]:
    """Returns {source_header: suggested_canonical_field_or_None}. Nothing here is forced - the
    caller (the mapping-step UI) lets the user override every suggestion."""
    synonyms = FIELD_SYNONYMS_BY_ENTITY.get(entity_type, {})
    lookup: dict[str, str] = {}
    for field_name, aliases in synonyms.items():
        lookup[_normalize_header(field_name)] = field_name
        for alias in aliases:
            lookup[_normalize_header(alias)] = field_name
    return {header: lookup.get(_normalize_header(header)) for header in headers}


def build_normalized_records(sheet_rows: list[dict[str, Any]], mapping: dict[str, str | None], entity_types: list[str], *, is_opening_balance: bool = False) -> list[dict[str, Any]]:
    """Converts one sheet's raw rows + its {source_header: "entity.field"} mapping into the
    normalized {customer, plot, sale, payment} shape every downstream function works with -
    regardless of whether the source was one combined sheet or a worksheet dedicated to just one
    entity type (e.g. a payment-only sheet linked to its sale by an external_reference column)."""
    records: list[dict[str, Any]] = []
    for raw_row in sheet_rows:
        buckets: dict[str, dict[str, Any]] = {entity: {} for entity in entity_types}
        for header, target in mapping.items():
            if not target or "." not in target:
                continue
            entity, field_name = target.split(".", 1)
            if entity in buckets:
                buckets[entity][field_name] = raw_row.get(header, "")
        record: dict[str, Any] = {"customer": None, "plot": None, "sale": None, "payment": None}
        for entity, values in buckets.items():
            if any(str(v).strip() for v in values.values()):
                record[entity] = values
        if record["payment"] is not None:
            record["payment"]["is_opening_balance"] = is_opening_balance
        if any(record.values()):
            records.append(record)
    return records


# ─── Spreadsheet parsing ──────────────────────────────────────────────────────────────────────

def parse_spreadsheet(data: bytes, filename: str) -> dict[str, list[dict[str, Any]]]:
    """Returns {sheet_name: [row_dict, ...]} - every value kept as a plain string (or empty
    string) so date/amount/phone parsing happens explicitly and predictably downstream, instead
    of relying on pandas' own type inference."""
    ext = str(filename or "").strip().lower().rsplit(".", 1)[-1] if "." in str(filename or "") else ""
    try:
        if ext in {"xlsx", "xls"}:
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str, keep_default_na=False, engine="openpyxl" if ext == "xlsx" else None)
        elif ext == "csv":
            sheets = {"Sheet1": pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="utf-8-sig")}
        else:
            raise ValueError("Unsupported file type - upload a .xlsx or .csv file")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not read this file: {exc}") from exc

    result: dict[str, list[dict[str, Any]]] = {}
    for name, frame in sheets.items():
        frame = frame.dropna(how="all")
        rows = [{str(key): str(value).strip() for key, value in record.items()} for record in frame.to_dict(orient="records")]
        result[str(name)] = rows
    if not any(result.values()):
        raise ValueError("No rows were found in this file")
    return result


# ─── Value parsing (Nigerian formats) ────────────────────────────────────────────────────────

def parse_amount(raw: Any) -> Decimal | None:
    text = str(raw or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"[₦$,\s]", "", text)
    cleaned = re.sub(r"(?i)ngn", "", cleaned)
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_date(raw: Any, *, day_first: bool = True) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = dateutil_parser.parse(text, dayfirst=day_first, fuzzy=False)
    except (ValueError, OverflowError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_nigerian_phone(raw: Any) -> str | None:
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return None
    if digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]
    elif len(digits) == 10:
        digits = "234" + digits
    return digits


# ─── Matching & validation ───────────────────────────────────────────────────────────────────

@dataclass
class RowResult:
    row_index: int
    source_data: dict[str, Any]
    match_status: str = "new"
    matched_entity_type: str | None = None
    matched_entity_id: int | None = None
    resolution: str = "create"
    issues: list[str] = field(default_factory=list)


def _resolve_customer(db: Session, *, organization_id: int, customer_data: dict[str, Any] | None) -> tuple[str, int | None, list[str]]:
    """Returns (match_status, matched_customer_id_or_None, issues)."""
    if not customer_data:
        return "error", None, ["No customer information on this row"]
    issues: list[str] = []
    external_id = str(customer_data.get("external_id") or "").strip()
    if external_id:
        existing = db.query(EstateCustomer).filter(EstateCustomer.organization_id == organization_id, EstateCustomer.external_id == external_id).one_or_none()
        if existing:
            return "matched", existing.id, issues

    email = str(customer_data.get("email") or "").strip().lower()
    phone = normalize_nigerian_phone(customer_data.get("phone"))
    if email or phone:
        query = db.query(EstateCustomer).filter(EstateCustomer.organization_id == organization_id)
        if email and phone:
            query = query.filter((func.lower(EstateCustomer.email) == email) | (EstateCustomer.phone == phone))
        elif email:
            query = query.filter(func.lower(EstateCustomer.email) == email)
        else:
            query = query.filter(EstateCustomer.phone == phone)
        matches = query.limit(2).all()
        if len(matches) == 1:
            issues.append("Matched by email/phone - please confirm this is the right customer")
            return "matched", matches[0].id, issues
        if len(matches) > 1:
            issues.append("More than one existing customer shares this email/phone")
            return "ambiguous", None, issues

    full_name = str(customer_data.get("full_name") or "").strip()
    if not full_name:
        return "error", None, ["Customer has no name, email, phone, or ID to identify them"]
    normalized_name = _normalized_text(full_name)
    name_matches = db.query(EstateCustomer).filter(EstateCustomer.organization_id == organization_id, EstateCustomer.full_name_normalized == normalized_name).limit(2).all()
    if name_matches:
        issues.append("A customer with this exact name already exists - name alone is not enough to auto-match; confirm manually")
        return "ambiguous", name_matches[0].id if len(name_matches) == 1 else None, issues
    return "new", None, issues


def _resolve_plot(db: Session, *, estate_id: int, plot_data: dict[str, Any] | None) -> tuple[str, EstatePlot | None, list[str]]:
    if not plot_data or not str(plot_data.get("plot_number") or "").strip():
        return "new", None, []
    plot_number_normalized = _normalized_text(plot_data.get("plot_number"))
    plot = db.query(EstatePlot).filter(EstatePlot.estate_id == estate_id, EstatePlot.plot_number_normalized == plot_number_normalized).one_or_none()
    if not plot:
        return "error", None, [f"Unknown plot \"{plot_data.get('plot_number')}\" - plots must already exist in LandCheck before they can be imported against"]
    return "matched", plot, []


def _resolve_allocation(db: Session, *, organization_id: int, estate_id: int, plot: EstatePlot | None, customer_id: int | None, sale_data: dict[str, Any] | None) -> tuple[str, EstateAllocation | None, list[str]]:
    """`plot` is optional - a payment-only row from a separate payment worksheet has no plot
    column of its own and must link to an already-committed sale purely by
    sale.external_reference. A brand NEW allocation can only ever be created when a real plot is
    known (never from a payment-only row with no plot reference)."""
    issues: list[str] = []
    external_reference = str((sale_data or {}).get("external_reference") or "").strip()
    if external_reference:
        existing = db.query(EstateAllocation).filter(EstateAllocation.organization_id == organization_id, EstateAllocation.external_reference == external_reference).one_or_none()
        if existing:
            return "matched", existing, issues
        if plot is None:
            return "error", None, [f"References sale \"{external_reference}\" which doesn't exist yet in this import or in LandCheck"]

    if plot is None:
        return "error", None, ["No plot or existing sale reference to attach this record to"]

    active = db.query(EstateAllocation).filter(EstateAllocation.plot_id == plot.id, EstateAllocation.status.in_(("reserved", "allocated"))).one_or_none()
    if active:
        if customer_id is not None and active.customer_id != customer_id:
            issues.append("This plot already has an active allocation for a different customer")
            return "conflict", active, issues
        return "matched", active, issues
    return "new", None, issues


def match_and_validate_rows(db: Session, *, organization_id: int, estate: Estate, normalized_records: list[dict[str, Any]], day_first: bool = True) -> list[RowResult]:
    """One RowResult per normalized record - see data_import.py's module docstring for the
    {customer, plot, sale, payment} normalized shape. Never writes to the database."""
    results: list[RowResult] = []
    for index, record in enumerate(normalized_records):
        issues: list[str] = []
        # Customer resolution is only attempted when this row actually carries customer columns -
        # a payment-only row from a separate payment worksheet (linked purely by a sale
        # reference) has none, and that's fine: record_payment() attaches a payment to whichever
        # customer the matched allocation already belongs to, no separate customer lookup needed.
        customer_status = "new"
        customer_id: int | None = None
        if record.get("customer"):
            customer_status, customer_id, customer_issues = _resolve_customer(db, organization_id=organization_id, customer_data=record.get("customer"))
            issues.extend(customer_issues)

        plot_status, plot, plot_issues = _resolve_plot(db, estate_id=estate.id, plot_data=record.get("plot"))
        issues.extend(plot_issues)

        allocation_status = "new"
        allocation = None
        if record.get("sale") is not None or record.get("payment") is not None:
            allocation_status, allocation, allocation_issues = _resolve_allocation(db, organization_id=organization_id, estate_id=estate.id, plot=plot, customer_id=customer_id, sale_data=record.get("sale"))
            issues.extend(allocation_issues)

        payment_data = record.get("payment")
        if payment_data:
            amount = parse_amount(payment_data.get("amount"))
            if amount is None and not payment_data.get("total_paid"):
                issues.append("Payment amount could not be read")
            reference_no = str(payment_data.get("reference_no") or "").strip()
            if reference_no and allocation is not None:
                duplicate = db.query(EstatePayment).filter(EstatePayment.allocation_id == allocation.id, EstatePayment.reference_no == reference_no).one_or_none()
                if duplicate:
                    issues.append(f"Payment reference \"{reference_no}\" already recorded on this allocation - will be skipped")

        sale_data = record.get("sale") or {}
        payment_outstanding = payment_data.get("outstanding") if payment_data else None
        supplied_outstanding = parse_amount(sale_data.get("outstanding") or payment_outstanding)
        if allocation is not None and supplied_outstanding is not None:
            summary = financial_summary(db, allocation)
            if abs(summary.outstanding - supplied_outstanding) > Decimal("1"):
                issues.append(f"Spreadsheet balance (NGN {supplied_outstanding}) does not match LandCheck's calculated balance (NGN {summary.outstanding}) - will not overwrite either number")

        statuses = [customer_status, plot_status if record.get("plot") else "new", allocation_status if (record.get("sale") or record.get("payment")) else "new"]
        if "error" in statuses:
            overall = "error"
        elif "conflict" in statuses:
            overall = "conflict"
        elif "ambiguous" in statuses:
            overall = "ambiguous"
        elif "matched" in statuses:
            overall = "matched"
        else:
            overall = "new"
        resolution = "skip" if overall in ("error", "conflict") else "create"

        results.append(RowResult(
            row_index=index,
            source_data=record,
            match_status=overall,
            matched_entity_type="allocation" if allocation is not None else ("customer" if customer_id else None),
            matched_entity_id=allocation.id if allocation is not None else customer_id,
            resolution=resolution,
            issues=issues,
        ))
    return results


# ─── Commit ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class CommitOutcome:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


def commit_import_rows(db: Session, *, batch: EstateImportBatch, rows: list[EstateImportRow], actor: EstatePrincipal) -> CommitOutcome:
    """Re-resolves and re-validates every row immediately before writing it (plot availability
    rechecked right before committing, per the product spec), then creates/updates records
    through the exact same service functions staff use by hand."""
    estate = db.get(Estate, batch.estate_id)
    outcome = CommitOutcome()
    for row in rows:
        if row.resolution == "skip":
            outcome.skipped += 1
            continue
        try:
            record = row.source_data or {}
            customer_data = record.get("customer")
            customer = None
            if customer_data:
                customer_status, matched_customer_id, _ = _resolve_customer(db, organization_id=batch.organization_id, customer_data=customer_data)
                if customer_status == "matched" and matched_customer_id:
                    customer = db.get(EstateCustomer, matched_customer_id)
                elif customer_status == "new":
                    customer = EstateCustomer(
                        organization_id=batch.organization_id,
                        external_id=str(customer_data.get("external_id") or "").strip() or None,
                        full_name=str(customer_data.get("full_name") or "").strip(),
                        full_name_normalized=_normalized_text(customer_data.get("full_name")),
                        phone=normalize_nigerian_phone(customer_data.get("phone")) or str(customer_data.get("phone") or "").strip() or None,
                        email=str(customer_data.get("email") or "").strip() or None,
                        address=str(customer_data.get("address") or "").strip() or None,
                        notes=str(customer_data.get("notes") or "").strip() or None,
                    )
                    db.add(customer)
                    db.flush()
                    append_estate_audit_event(db, organization_id=batch.organization_id, actor=actor, action="customer.created", entity_type="estate_customer", entity_id=customer.id, after_data={"full_name": customer.full_name, "source": "import"}, correlation_id=f"import-{batch.batch_uid}")
                    row.created_new_customer = True
                else:
                    outcome.errors.append({"row": row.row_index, "reason": "Customer could not be resolved at commit time - may have changed since preview"})
                    row.resolution = "skip"
                    continue
                row.committed_customer_id = customer.id

            allocation = None
            plot_data = record.get("plot")
            sale_data = record.get("sale")
            plot = None
            if row.manual_plot_id:
                # Staff manually linked this row to an existing plot (see the review step's
                # plot picker) - takes priority over whatever plot text the sheet itself had,
                # since that text is exactly what failed to auto-match.
                plot = db.get(EstatePlot, row.manual_plot_id)
                if not plot or plot.estate_id != batch.estate_id:
                    outcome.errors.append({"row": row.row_index, "reason": "The manually linked plot could not be found at commit time"})
                    row.resolution = "skip"
                    continue
            elif plot_data and str(plot_data.get("plot_number") or "").strip():
                plot_status, plot, _ = _resolve_plot(db, estate_id=batch.estate_id, plot_data=plot_data)
                if plot_status != "matched" or plot is None:
                    outcome.errors.append({"row": row.row_index, "reason": f"Plot \"{plot_data.get('plot_number')}\" not found at commit time"})
                    row.resolution = "skip"
                    continue

            if sale_data is not None or record.get("payment") is not None:
                allocation_status, allocation, _ = _resolve_allocation(db, organization_id=batch.organization_id, estate_id=batch.estate_id, plot=plot, customer_id=customer.id if customer else None, sale_data=sale_data)
                if allocation_status in ("conflict", "error"):
                    outcome.errors.append({"row": row.row_index, "reason": "Plot/sale could not be resolved at commit time (conflict or missing reference) - skipped"})
                    row.resolution = "skip"
                    continue
                if allocation is None and plot is not None and customer is not None:
                    agreed_price = parse_amount((sale_data or {}).get("agreed_price"))
                    allocation = reserve_or_allocate(
                        db,
                        plot=plot,
                        customer=customer,
                        actor=actor,
                        allocate=False,
                        agreed_price=agreed_price,
                        notes="Imported from spreadsheet" if not (sale_data or {}).get("notes") else str(sale_data.get("notes")),
                    )
                    allocation.external_reference = str((sale_data or {}).get("external_reference") or "").strip() or None
                    row.created_new_allocation = True
                    outcome.created += 1
                elif allocation is not None:
                    outcome.updated += 1
                if allocation is not None:
                    row.committed_allocation_id = allocation.id
                    customer = customer or db.get(EstateCustomer, allocation.customer_id)
                    plot = plot or db.get(EstatePlot, allocation.plot_id)

            payment_data = record.get("payment")
            if payment_data and allocation is not None:
                is_opening_balance = bool(payment_data.get("is_opening_balance"))
                if is_opening_balance:
                    amount = parse_amount(payment_data.get("total_paid") or payment_data.get("amount"))
                    method = "migration_opening_balance"
                    payment_date = batch.opening_balance_cutoff or datetime.now(timezone.utc)
                    notes = f"Opening balance imported from {batch.source_filename}, as of {payment_date.date().isoformat()}."
                    reference = None
                else:
                    amount = parse_amount(payment_data.get("amount"))
                    method = str(payment_data.get("method") or "bank_transfer").strip() or "bank_transfer"
                    payment_date = parse_date(payment_data.get("payment_date")) or datetime.now(timezone.utc)
                    notes = "Imported from spreadsheet"
                    reference = str(payment_data.get("reference_no") or "").strip() or None
                if reference:
                    duplicate = db.query(EstatePayment).filter(EstatePayment.allocation_id == allocation.id, EstatePayment.reference_no == reference).one_or_none()
                    if duplicate:
                        outcome.skipped += 1
                        amount = None
                if amount is not None and amount > 0:
                    summary = financial_summary(db, allocation)
                    if allocation.agreed_price and summary.confirmed_paid + amount > Decimal(str(allocation.agreed_price)):
                        outcome.errors.append({"row": row.row_index, "reason": "Payment would exceed the agreed price - skipped, review manually"})
                    else:
                        payment = record_payment(db, allocation=allocation, amount=amount, payment_date=payment_date, method=method, reference=reference, notes=notes, actor=actor, confirmation_required=False)
                        confirm_payment(db, payment=payment, actor=actor)
                        append_estate_audit_event(db, organization_id=batch.organization_id, actor=actor, action="payment.recorded", entity_type="estate_payment", entity_id=payment.id, after_data={"amount": str(amount), "source": "import"}, correlation_id=f"import-{batch.batch_uid}")
                        row.committed_payment_id = payment.id
                        # Mirrors the manual-confirm route's own auto-promotion rule: a reservation
                        # becomes a full Allocation once fully paid, never before.
                        refreshed = financial_summary(db, allocation)
                        if refreshed.agreed_price > 0 and refreshed.outstanding <= 0 and allocation.status == "reserved":
                            reserve_or_allocate(db, plot=plot, customer=customer, actor=actor, allocate=True)

            row.match_status = "matched"
            db.flush()
        except Exception as exc:
            logger.exception("Import row commit failed (batch=%s, row=%s)", batch.id, row.row_index)
            outcome.errors.append({"row": row.row_index, "reason": str(exc)})
            row.resolution = "skip"
    return outcome


def check_reversal_blockers(db: Session, *, batch: EstateImportBatch) -> list[str]:
    """Returns a list of reasons reversal is blocked (empty list = safe to reverse) WITHOUT
    changing anything - call this before reverse_import_batch() so the caller can show the
    blockers instead of a committed/rolled-back write."""
    blockers: list[str] = []
    rows = db.query(EstateImportRow).filter(EstateImportRow.batch_id == batch.id).all()
    for row in rows:
        if row.committed_payment_id:
            payment = db.get(EstatePayment, row.committed_payment_id)
            if payment and payment.status != "confirmed":
                blockers.append(f"Payment on row {row.row_index} has been changed since import (status: {payment.status})")
        if row.created_new_allocation and row.committed_allocation_id:
            allocation = db.get(EstateAllocation, row.committed_allocation_id)
            if allocation:
                other_payments = db.query(EstatePayment).filter(EstatePayment.allocation_id == allocation.id, EstatePayment.id != (row.committed_payment_id or -1)).count()
                if other_payments:
                    blockers.append(f"Allocation on row {row.row_index} has payments recorded since the import - cannot reverse")
                if allocation.status not in ("reserved", "allocated"):
                    blockers.append(f"Allocation on row {row.row_index} is no longer active (status: {allocation.status})")
    return blockers


def reverse_import_batch(db: Session, *, batch: EstateImportBatch, actor: EstatePrincipal) -> list[str]:
    """Actually undoes what this batch created - voids its payments and releases any allocation
    it newly created (never one it only matched to a pre-existing record). Deliberately does NOT
    delete customers this batch created: there's no delete/archive concept for customers anywhere
    else in this codebase to hook into safely, so a reversed batch leaves its customer records in
    place - surfaced here, not silently glossed over. Call check_reversal_blockers() first; this
    function assumes it's already been confirmed safe and will raise if something has changed
    since that check (a race between the two calls)."""
    blockers = check_reversal_blockers(db, batch=batch)
    if blockers:
        return blockers
    rows = db.query(EstateImportRow).filter(EstateImportRow.batch_id == batch.id).all()
    for row in rows:
        if row.committed_payment_id:
            payment = db.get(EstatePayment, row.committed_payment_id)
            if payment and payment.status == "confirmed":
                void_payment(db, payment=payment, actor=actor, reason=f"Import batch {batch.batch_uid} reversed")
        if row.created_new_allocation and row.committed_allocation_id:
            allocation = db.get(EstateAllocation, row.committed_allocation_id)
            if allocation and allocation.status in ("reserved", "allocated"):
                release_allocation(db, allocation=allocation, actor=actor, reason=f"Import batch {batch.batch_uid} reversed")
    return []
