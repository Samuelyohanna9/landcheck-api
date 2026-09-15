from __future__ import annotations

"""Generates the Estate Performance Report - a premium, print-ready PDF pulling together the
figures already shown on the Reports page (inventory, financial, geometry/hazard, activity) plus a
snapshot of the actual layout, into one branded document. This is deliberately a generated PDF
(not "print the web page"), so it reads like something you would hand to an investor or a board,
not a screenshot of a dashboard."""

import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.estates.layout_export import render_estate_layout_thumbnail_png

INK = colors.HexColor("#101827")
MUTED = colors.HexColor("#6b7685")
ACCENT = colors.HexColor("#d1332b")
FAINT_LINE = colors.HexColor("#e3e6ea")
TILE_BG = colors.HexColor("#f6f7f9")
GOOD = colors.HexColor("#1f9d63")
WARN = colors.HexColor("#b8860b")
BAD = colors.HexColor("#c23b3b")

STATUS_LABELS = {"available": "Available", "reserved": "Reserved", "allocated": "Allocated", "on_hold": "On hold"}
DEVELOPMENT_LABELS = {"not_started": "Not started", "site_cleared": "Site cleared", "foundation": "Foundation", "under_construction": "Under construction", "developed": "Developed"}


def _naira(value: Any) -> str:
    # The base-14 PDF fonts reportlab ships with have no glyph for the Naira sign (U+20A6) - it
    # renders as a black tofu box in every viewer instead, so an "NGN" prefix is used instead of
    # the symbol to stay reliably legible without embedding a custom font just for this.
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        amount = Decimal(0)
    return f"NGN {amount:,.0f}"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "cover_org": ParagraphStyle("cover_org", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=10.5, textColor=colors.white, spaceAfter=2),
        "cover_title": ParagraphStyle("cover_title", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=22, textColor=colors.white, leading=26),
        "cover_sub": ParagraphStyle("cover_sub", parent=base["Normal"], fontName="Helvetica", fontSize=10, textColor=colors.HexColor("#d7dbe1")),
        "cover_meta": ParagraphStyle("cover_meta", parent=base["Normal"], fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#d7dbe1"), alignment=TA_RIGHT),
        "section": ParagraphStyle("section", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=13, textColor=INK, spaceBefore=4, spaceAfter=2),
        "section_note": ParagraphStyle("section_note", parent=base["Normal"], fontName="Helvetica", fontSize=8.5, textColor=MUTED, spaceAfter=8),
        "tile_label": ParagraphStyle("tile_label", parent=base["Normal"], fontName="Helvetica", fontSize=8, textColor=MUTED, alignment=TA_CENTER),
        "tile_value": ParagraphStyle("tile_value", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=15, textColor=INK, alignment=TA_CENTER, spaceBefore=2),
        "tile_value_sm": ParagraphStyle("tile_value_sm", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=11.5, textColor=INK, alignment=TA_CENTER, spaceBefore=2),
        "body": ParagraphStyle("body", parent=base["Normal"], fontName="Helvetica", fontSize=9, textColor=INK, leading=13),
        "body_muted": ParagraphStyle("body_muted", parent=base["Normal"], fontName="Helvetica", fontSize=8.5, textColor=MUTED, leading=12),
        "th": ParagraphStyle("th", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8.5, textColor=colors.white),
        "td": ParagraphStyle("td", parent=base["Normal"], fontName="Helvetica", fontSize=8.7, textColor=INK),
        "td_muted": ParagraphStyle("td_muted", parent=base["Normal"], fontName="Helvetica", fontSize=8.2, textColor=MUTED),
    }


def _tile(label: str, value: str, styles: dict, *, value_style: str = "tile_value") -> Table:
    inner = Table([[Paragraph(value, styles[value_style])], [Paragraph(label.upper(), styles["tile_label"])]], colWidths=[None])
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TILE_BG),
        ("BOX", (0, 0), (-1, -1), 0.6, FAINT_LINE),
        ("LINEABOVE", (0, 0), (-1, 0), 2.2, ACCENT),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
    ]))
    return inner


def _tile_row(items: list[tuple[str, str]], styles: dict, col_width: float, *, value_style: str = "tile_value") -> Table:
    row = [_tile(label, value, styles, value_style=value_style) for label, value in items]
    table = Table([row], colWidths=[col_width] * len(row))
    table.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return table


def _section(title: str, styles: dict, note: str | None = None) -> list:
    flow: list = [Paragraph(title, styles["section"])]
    flow.append(HRFlowable(width="100%", thickness=1.4, color=ACCENT, spaceAfter=6))
    if note:
        flow.append(Paragraph(note, styles["section_note"]))
    return flow


def _data_table(header: list[str], rows: list[list[str]], styles: dict, col_widths: list[float] | None = None) -> Table:
    head = [Paragraph(text, styles["th"]) for text in header]
    body_rows = [[Paragraph(str(cell), styles["td"]) for cell in row] for row in rows]
    table = Table([head] + body_rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, FAINT_LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbfc")]),
    ]
    table.setStyle(TableStyle(style))
    return table


def _risk_color(risk_class: str | None) -> colors.Color:
    key = str(risk_class or "").lower()
    if key in {"high", "severe", "critical"}:
        return BAD
    if key in {"moderate", "medium"}:
        return WARN
    if key in {"low", "minimal", "none"}:
        return GOOD
    return MUTED


def _footer(canvas, doc, *, estate_name: str, organization_name: str) -> None:
    canvas.saveState()
    canvas.setStrokeColor(FAINT_LINE)
    canvas.setLineWidth(0.6)
    canvas.line(18 * mm, 14 * mm, doc.pagesize[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 10 * mm, f"{organization_name or 'LandCheck Estates'} · {estate_name}")
    canvas.drawRightString(doc.pagesize[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def render_estate_report_pdf(
    *,
    estate: Any,
    organization_name: str,
    dashboard: dict,
    quality: dict | None,
    hazards: dict | None,
    activity: list[dict],
    plots: list[Any],
    features: list[Any],
    to_shape_fn,
    output_path: str,
) -> dict:
    styles = _styles()
    doc = SimpleDocTemplate(output_path, pagesize=A4, topMargin=0, bottomMargin=20 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    usable_width = A4[0] - 36 * mm
    story: list = []

    # --- Cover band -------------------------------------------------------------------------
    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    cover_inner = Table(
        [[Paragraph((organization_name or "LandCheck Estates").upper(), styles["cover_org"]), Paragraph(f"Generated {generated_at}", styles["cover_meta"])],
         [Paragraph(f"{estate.name or 'Estate'}", styles["cover_title"]), ""],
         [Paragraph("Estate Performance Report", styles["cover_sub"]), ""]],
        colWidths=[usable_width * 0.7, usable_width * 0.3],
    )
    cover_inner.setStyle(TableStyle([("SPAN", (0, 1), (1, 1)), ("SPAN", (0, 2), (1, 2)), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, 0), 0), ("BOTTOMPADDING", (0, 2), (-1, 2), 0)]))
    cover = Table([[cover_inner]], colWidths=[usable_width + 36 * mm])
    cover.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, -1), 3, ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 22),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ("LEFTPADDING", (0, 0), (-1, -1), 18 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 18 * mm),
    ]))
    story.append(cover)
    story.append(Spacer(1, 14))

    # --- Executive summary --------------------------------------------------------------------
    total_plots = int(dashboard.get("total_plots") or 0)
    statuses = dashboard.get("statuses") or {}
    financial = dashboard.get("financial") or {}
    contracted = Decimal(str(financial.get("contracted_sales_value") or 0))
    confirmed = Decimal(str(financial.get("confirmed_collections") or 0))
    pending = Decimal(str(financial.get("pending_collections") or 0))
    outstanding = Decimal(str(financial.get("outstanding_balance") or 0))
    collection_rate = f"{(confirmed / contracted * 100):.0f}%" if contracted > 0 else "–"

    story += _section("Executive summary", styles)
    story.append(_tile_row([
        ("Total plots", f"{total_plots:,}"),
        ("Available", f"{int(statuses.get('available') or 0):,}"),
        ("Allocated", f"{int(statuses.get('allocated') or 0):,}"),
        ("Reserved", f"{int(statuses.get('reserved') or 0):,}"),
    ], styles, usable_width / 4))
    story.append(Spacer(1, 8))
    story.append(_tile_row([
        ("Contracted value", _naira(contracted)),
        ("Confirmed collected", _naira(confirmed)),
        ("Outstanding", _naira(outstanding)),
        ("Collection rate", collection_rate),
    ], styles, usable_width / 4, value_style="tile_value_sm"))
    story.append(Spacer(1, 16))

    # --- Layout snapshot ----------------------------------------------------------------------
    story += _section("Layout snapshot", styles, note="A live render of the estate's current plots, roads and reserves - not a stored image.")
    plotted_plots = [plot for plot in plots if plot.geometry]
    if plotted_plots or estate.boundary is not None:
        buffer = io.BytesIO()
        try:
            render_estate_layout_thumbnail_png(estate=estate, plots=plotted_plots, features=features, to_shape_fn=to_shape_fn, output_path=buffer)
            buffer.seek(0)
            image_width = usable_width
            story.append(Image(buffer, width=image_width, height=image_width * 0.62))
            legend_items = [
                ("Available", colors.HexColor("#fdfdfb")), ("Reserved", colors.HexColor("#fff4cc")),
                ("Allocated", colors.HexColor("#d9f2e3")), ("On hold", colors.HexColor("#eeeeee")),
                ("Road", colors.HexColor("#c9cdd2")), ("Open space", colors.HexColor("#cdeedb")),
                ("Drainage", colors.HexColor("#bcdcee")),
            ]
            legend_row = []
            for label, swatch_color in legend_items:
                swatch = Table([[""]], colWidths=[8], rowHeights=[8])
                swatch.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), swatch_color), ("BOX", (0, 0), (-1, -1), 0.5, MUTED)]))
                legend_row.append(swatch)
                legend_row.append(Paragraph(label, styles["td_muted"]))
            legend_table = Table([legend_row], colWidths=None)
            legend_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
            story.append(Spacer(1, 4))
            story.append(legend_table)
        except ValueError:
            story.append(Paragraph("No plottable geometry available yet.", styles["body_muted"]))
    else:
        story.append(Paragraph("No plottable geometry available yet.", styles["body_muted"]))
    story.append(Spacer(1, 16))

    # --- Inventory breakdown ------------------------------------------------------------------
    story += _section("Inventory breakdown", styles)
    status_rows = [[STATUS_LABELS.get(key, key.title()), f"{count:,}", f"{(count / total_plots * 100):.0f}%" if total_plots else "0%"] for key, count in statuses.items() if key != "sold"]
    story.append(_data_table(["Commercial status", "Plots", "Share"], status_rows, styles, [usable_width * 0.55, usable_width * 0.22, usable_width * 0.23]))
    story.append(Spacer(1, 10))
    development = dashboard.get("development") or {}
    if any(development.values()):
        dev_rows = [[DEVELOPMENT_LABELS.get(key, key.title()), f"{count:,}"] for key, count in development.items() if count]
        story.append(_data_table(["Development stage", "Plots"], dev_rows, styles, [usable_width * 0.75, usable_width * 0.25]))
        story.append(Spacer(1, 10))
    story.append(Paragraph(f"Mapped area: {float(dashboard.get('mapped_area_sqm') or 0):,.0f} m² · Staked: {dashboard.get('staked_plots', 0)} · Awaiting survey: {dashboard.get('awaiting_survey', 0)} · Survey completed: {dashboard.get('survey_completed', 0)}", styles["body_muted"]))
    story.append(Spacer(1, 16))

    # --- Financial detail -----------------------------------------------------------------------
    story += _section("Financial detail", styles)
    story.append(_data_table(
        ["Line", "Amount"],
        [["Contracted sales value", _naira(contracted)], ["Confirmed collections", _naira(confirmed)], ["Pending collections", _naira(pending)], ["Outstanding balance", _naira(outstanding)]],
        styles, [usable_width * 0.7, usable_width * 0.3],
    ))
    story.append(Spacer(1, 16))

    # --- Geometry & hazard ------------------------------------------------------------------
    story += _section("Geometry & hazard", styles)
    if quality:
        issue_count = len(quality.get("issues") or [])
        review_line = f"Review required – {issue_count} issue(s) across {quality.get('plot_count', 0)} plots." if quality.get("review_required") else f"Geometry clean across {quality.get('plot_count', 0)} plots."
        story.append(Paragraph(review_line, styles["body"]))
        top_issues = (quality.get("issues") or [])[:12]
        if top_issues:
            story.append(Spacer(1, 6))
            items = [ListItem(Paragraph(f"<b>{issue.get('code', '').replace('_', ' ').title()}</b> – {issue.get('message', '')}", styles["td"]), bulletColor=_risk_color(issue.get("severity"))) for issue in top_issues]
            story.append(ListFlowable(items, bulletType="bullet", start="circle", leftIndent=12))
    else:
        story.append(Paragraph("No geometry quality data available.", styles["body_muted"]))
    story.append(Spacer(1, 10))
    if hazards and hazards.get("summary"):
        hazard_rows = []
        for hazard_type, bucket in hazards["summary"].items():
            classes = ", ".join(f"{cls} ({count})" for cls, count in (bucket.get("classes") or {}).items()) or "–"
            hazard_rows.append([hazard_type.title(), f"{bucket.get('assessed', 0):,}", classes])
        story.append(_data_table(["Hazard type", "Plots assessed", "Risk classes"], hazard_rows, styles, [usable_width * 0.25, usable_width * 0.25, usable_width * 0.5]))
    else:
        story.append(Paragraph("No hazard assessments recorded yet.", styles["body_muted"]))
    story.append(Spacer(1, 16))

    # --- Recent activity ------------------------------------------------------------------------
    if activity:
        story += _section("Recent activity", styles)
        activity_rows = []
        for event in activity[:10]:
            created_at = event.get("created_at")
            when = created_at.strftime("%d %b %Y %H:%M") if hasattr(created_at, "strftime") else str(created_at or "")
            activity_rows.append([when, str(event.get("action") or "").replace("_", " ").replace(".", " ").title()])
        story.append(_data_table(["When", "Action"], activity_rows, styles, [usable_width * 0.3, usable_width * 0.7]))

    def _on_page(canvas, doc_):
        _footer(canvas, doc_, estate_name=estate.name or "Estate", organization_name=organization_name)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return {"plot_count": len(plots), "feature_count": len(features), "generated_at": generated_at}
