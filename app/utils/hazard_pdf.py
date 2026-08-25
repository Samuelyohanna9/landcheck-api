import io
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

from app.utils.green_pdf import _draw_rounded_box, _draw_stat_card, _draw_bar_chart

BRAND_BAR_COLOR = "#050b24"
BRAND_ACCENT = "#fb923c"


def _draw_wrapped(c: canvas.Canvas, text: str, x: float, y: float, max_width: float, line_height: float) -> float:
    """Same word-wrap helper as before, but now returns the y-position after the last line so
    callers can stack content below it without guessing how many lines it took.
    """
    words = text.replace("\n", " ").split()
    line = ""
    cursor_y = y
    for word in words:
        test_line = f"{line} {word}".strip()
        if stringWidth(test_line, c._fontname, c._fontsize) <= max_width:
            line = test_line
        else:
            c.drawString(x, cursor_y, line)
            cursor_y -= line_height
            line = word
    if line:
        c.drawString(x, cursor_y, line)
        cursor_y -= line_height
    return cursor_y


def _draw_hazard_header(c: canvas.Canvas, width: float, height: float, *, report_label: str, subtitle: str) -> float:
    bar_height = 76
    c.setFillColor(HexColor(BRAND_BAR_COLOR))
    c.rect(0, height - bar_height, width, bar_height, fill=1, stroke=0)
    c.setStrokeColor(HexColor(BRAND_ACCENT))
    c.setLineWidth(2)
    c.line(0, height - bar_height, width, height - bar_height)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(36, height - 34, "LandCheck")
    c.setFillColor(HexColor(BRAND_ACCENT))
    c.setFont("Helvetica-Bold", 20)
    c.drawString(36 + stringWidth("LandCheck ", "Helvetica-Bold", 20), height - 34, report_label)

    c.setFillColor(HexColor("#cbd5e1"))
    c.setFont("Helvetica", 9.5)
    c.drawString(36, height - 52, subtitle)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    c.setFont("Helvetica", 8)
    c.setFillColor(HexColor("#94a3b8"))
    c.drawRightString(width - 36, height - 30, f"Generated {generated}")
    c.setFont("Helvetica-Oblique", 7.5)
    c.drawRightString(width - 36, height - 44, "Screening-level assessment - not a substitute for a licensed site survey")

    return height - bar_height


def _draw_risk_badge(c: canvas.Canvas, x: float, y: float, risk_class: str, risk_score: str, class_color: str) -> None:
    badge_w, badge_h = 150, 54
    _draw_rounded_box(c, x, y, badge_w, badge_h, 10, fill_color=HexColor(class_color))
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(x + 16, y + badge_h - 28, f"{risk_score}%")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x + 16, y + 12, risk_class.upper())


def _render_hazard_report_pdf(
    output_path: str,
    overlay_png: bytes,
    *,
    report_label: str,
    subtitle: str,
    risk_score: str,
    risk_class: str,
    class_color: str,
    headline: str,
    stat_cards: List[Tuple[str, str]],
    component_bars: List[Tuple[str, float, str]],
    method: str,
    footnotes: List[str],
    legend: List[Dict[str, str]],
    note: str = "",
    insight: str = "",
    map_has_own_legend: bool = False,
    references: List[Dict[str, str]] = None,
    show_risk_badge: bool = True,
    c: Optional[canvas.Canvas] = None,
) -> canvas.Canvas:
    """Renders one hazard-report page. Normally self-contained (creates its own single-page PDF and
    saves it) - erosion and land-cover reports both still call it this way, unchanged. Pass an
    existing `c` to instead render onto that canvas as one page of a larger multi-page document
    (used by flood's 3-page River/Rainfall/Overall report) - the caller is then responsible for its
    own page breaks (c.showPage()) and final c.save(). Returns the canvas either way, so a caller
    building a multi-page document can keep chaining calls onto the same one.
    """
    owns_canvas = c is None
    if c is None:
        c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    content_top = _draw_hazard_header(c, width, height, report_label=report_label, subtitle=subtitle)

    y = content_top - 26
    # Land cover has no risk score/tier - showing an empty/meaningless badge would look like a real
    # result. headline_x shifts left to fill the space the badge would otherwise occupy.
    headline_x = 36
    if show_risk_badge:
        _draw_risk_badge(c, 36, y - 40, risk_class, risk_score, class_color)
        headline_x = 200

    c.setFillColor(HexColor("#111827"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(headline_x, y - 6, headline)
    content_bottom = y - 22
    if note:
        c.setFont("Helvetica", 8.5)
        c.setFillColor(HexColor("#4b5563"))
        content_bottom = _draw_wrapped(c, note, headline_x, y - 22, width - 36 - headline_x, 11)

    if insight:
        insight_box_y = content_bottom - 4
        _draw_rounded_box(c, 200, insight_box_y - 18, width - 236, 22, 5, fill_color=HexColor("#fff7ed"), stroke_color=HexColor("#fdba74"))
        c.setFillColor(HexColor("#c2410c"))
        c.setFont("Helvetica-Bold", 7.8)
        _draw_wrapped(c, insight, 208, insight_box_y - 6, width - 252, 9)
        content_bottom = insight_box_y - 22

    # Stat card row - up to 4 cards, evenly spaced.
    cards_top = min(y - 60, content_bottom - 8)
    card_h = 52
    card_gap = 10
    card_w = (width - 72 - card_gap * (len(stat_cards) - 1)) / max(len(stat_cards), 1)
    cx = 36
    for label, value in stat_cards:
        _draw_stat_card(c, cx, cards_top - card_h, card_w, card_h, label, value)
        cx += card_w + card_gap

    # Component bar chart - shows exactly which factors drove the score, in place of a wall of
    # method prose as the only explanation.
    chart_top = cards_top - card_h - 26
    chart_h = 92
    if risk_class == "No Data":
        # Individual component values are meaningless (often defaulted) when there was no real
        # hazard data for this location - showing a chart of them would look like a real result.
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(HexColor("#111827"))
        c.drawString(36, chart_top, "Score components")
        c.setFont("Helvetica", 8.5)
        c.setFillColor(HexColor("#6b7280"))
        c.drawString(36, chart_top - 16, "Not shown - no underlying data was available for this location.")
    else:
        bar_data = [(label, round(score * 100, 1), color) for label, score, color in component_bars]
        _draw_bar_chart(
            c, 36, chart_top - chart_h, width - 72, chart_h, bar_data,
            title="Score components (% contribution to risk)",
        )

    # Legend, right-aligned above the map - skipped when the map image already has its own
    # legend/scale bar/north arrow baked in (the local matplotlib-rendered flood map), so the
    # report doesn't show two conflicting legends for the same image.
    legend_top = chart_top - chart_h - 20
    map_top = legend_top - 14
    legend_x = width - 190
    ly = map_top
    if not map_has_own_legend:
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(HexColor("#111827"))
        c.drawString(legend_x, ly, "Risk scale")
        ly -= 14
        c.setFont("Helvetica", 8)
        for item in legend:
            try:
                swatch = colors.HexColor(item["color"])
            except Exception:
                swatch = colors.black
            c.setFillColor(swatch)
            c.rect(legend_x, ly - 7, 9, 9, fill=1, stroke=0)
            c.setFillColor(HexColor("#374151"))
            c.drawString(legend_x + 13, ly - 6, str(item.get("label", "")))
            ly -= 13

    # Map image.
    img = ImageReader(io.BytesIO(overlay_png))
    img_x = 36
    img_w = width - 72 - 210
    img_y = 96
    img_h = map_top - img_y
    c.setStrokeColor(HexColor("#d1d5db"))
    c.setLineWidth(0.75)
    c.rect(img_x, img_y, img_w, img_h, fill=0, stroke=1)
    c.drawImage(img, img_x + 2, img_y + 2, img_w - 4, img_h - 4, preserveAspectRatio=True, anchor="c")

    if not map_has_own_legend:
        arrow_x = img_x + img_w - 20
        arrow_y = img_y + img_h - 20
        c.setFillColor(colors.black)
        path = c.beginPath()
        path.moveTo(arrow_x, arrow_y)
        path.lineTo(arrow_x - 5, arrow_y - 10)
        path.lineTo(arrow_x + 5, arrow_y - 10)
        path.close()
        c.drawPath(path, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(arrow_x, arrow_y - 20, "N")

    # Method box, right column beneath legend.
    method_x = width - 190
    method_y = ly - 10
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(HexColor("#111827"))
    c.drawString(method_x, method_y, "How this is scored")
    method_y -= 13
    c.setFont("Helvetica", 7.2)
    c.setFillColor(HexColor("#4b5563"))
    for line in method.split("\n"):
        method_y = _draw_wrapped(c, line, method_x, method_y, 154, 9)

    # Footer - footnotes, then a compact one-line "References" citation strip so the methodology
    # is independently verifiable, not just asserted.
    footer_y = 46
    c.setStrokeColor(HexColor("#e5e7eb"))
    c.setLineWidth(0.5)
    c.line(36, footer_y + 12, width - 36, footer_y + 12)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(HexColor("#6b7280"))
    line_i = 0
    for line in footnotes:
        c.drawString(36, footer_y - (line_i * 10), line)
        line_i += 1
    if references:
        short_names = [ref.get("short") for ref in references if ref.get("short")]
        if short_names:
            c.setFont("Helvetica-Oblique", 7.5)
            c.drawString(36, footer_y - (line_i * 10), f"References: {', '.join(short_names)}.")
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(HexColor("#111827"))
    c.drawRightString(width - 36, footer_y, "LandCheck")

    if owns_canvas:
        c.showPage()
        c.save()
    return c


def _draw_flood_screening_summary_page(c: canvas.Canvas, width: float, height: float, summary: Dict[str, object]) -> None:
    """The 4-page flood report's first page: three independent evidence lines (River, Floodplain,
    Rainfall) plus a plain-text recommendation - deliberately NOT a single composite risk badge/
    score. A blind validation (Phase 4, hashed and permanently recorded) found the previous max()
    combination actively destroyed specificity, and that the Rainfall/pluvial branch does not
    reliably discriminate on its own - so this page never blends the three into one number a
    reader might act on. River and Floodplain keep their own risk-tier language (they remain
    customer-facing evidence); Rainfall is explicitly labeled experimental. Pages 2-4 carry the
    full detail behind each of the three engines.
    """
    river = summary.get("river") or {}
    floodplain = summary.get("floodplain") or {}
    screening = summary.get("summary") or {}

    content_top = _draw_hazard_header(
        c, width, height, report_label="Flood Screening Summary", subtitle="River, floodplain & rainfall flood exposure screening",
    )
    y = content_top - 30

    river_class = str(screening.get("river_class", "Low"))
    river_available = screening.get("river_available", True)
    floodplain_class = str(screening.get("floodplain_class", "Low"))

    lines = [
        ("River Inundation", river_class if river_available else "No direct modelled coverage",
         str(river.get("class_color") or "#94a3b8") if river_available else "#94a3b8"),
        ("Floodplain Susceptibility", floodplain_class, str(floodplain.get("class_color") or "#94a3b8")),
        ("Rainfall / Surface-Water Susceptibility", "Experimental - insufficient validated local drainage data", "#94a3b8"),
    ]

    row_h = 50
    stacked_row_h = 62  # taller - label and value stack vertically instead of sharing one line
    label_font, label_size = "Helvetica-Bold", 11
    value_font, value_size = "Helvetica-Bold", 12
    row_padding = 16  # minimum gap to keep label and value from ever touching
    available_w = (width - 52) - 52  # same left/right inset the strings are drawn at

    ry = y
    for label, value, color in lines:
        display_value = value.upper() if value in ("Low", "Moderate", "High", "Severe") else value
        label_w = c.stringWidth(label, label_font, label_size)
        value_w = c.stringWidth(display_value, value_font, value_size)
        # Side-by-side (label left, value right on one line) only when both actually fit with
        # room to spare - measured, not assumed, so a long status phrase (e.g. the Rainfall row's
        # "Experimental - insufficient validated local drainage data") can never overlap a long
        # label instead of silently colliding in the middle of the box.
        fits_side_by_side = (label_w + value_w + row_padding) <= available_w
        this_row_h = row_h if fits_side_by_side else stacked_row_h

        _draw_rounded_box(c, 36, ry - this_row_h, width - 72, this_row_h, 8, fill_color=HexColor("#f8fafc"), stroke_color=HexColor("#e2e8f0"))
        c.setFillColor(HexColor("#111827"))
        c.setFont(label_font, label_size)
        c.drawString(52, ry - 20, label)
        c.setFillColor(HexColor(color))
        c.setFont(value_font, value_size)
        if fits_side_by_side:
            c.drawRightString(width - 52, ry - 20, display_value)
        else:
            c.drawString(52, ry - 40, display_value)
        ry -= this_row_h + 14

    c.setFillColor(HexColor("#111827"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(36, ry - 8, "Recommendation")
    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#374151"))
    _draw_wrapped(c, str(screening.get("recommendation", "")), 36, ry - 24, width - 72, 12)

    footer_y = 60
    c.setStrokeColor(HexColor("#e5e7eb"))
    c.setLineWidth(0.5)
    c.line(36, footer_y + 12, width - 36, footer_y + 12)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(HexColor("#6b7280"))
    c.drawString(36, footer_y, "See following pages for full River, Floodplain, and Rainfall detail, including method and confidence.")
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(HexColor("#111827"))
    c.drawRightString(width - 36, footer_y, "LandCheck")


def render_flood_report_pdf(output_path: str, river_png: bytes, floodplain_png: bytes, pluvial_png: bytes, summary: Dict[str, object]) -> None:
    """4-page report: an Overall Screening Risk summary, then full River Flood Risk detail, then
    full Floodplain Susceptibility detail, then full Surface-Water/Rainfall Flood Risk detail - each
    engine gets its own page rather than being collapsed into one shared, ambiguous score (per the
    reviewer feedback that motivated the original river/pluvial split, extended to the floodplain
    branch added in V2). Page order follows the physical-evidence hierarchy: direct modelled river
    depth, then elevation-relative-to-drainage evidence, then rainfall-driven susceptibility.
    """
    river = summary.get("river") or {}
    floodplain = summary.get("floodplain") or {}
    rainfall = summary.get("rainfall") or {}
    legend = summary.get("legend") or []

    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4
    _draw_flood_screening_summary_page(c, width, height, summary)
    c.showPage()

    # --- River detail page ---
    river_class = str(river.get("risk_class", "Low"))
    river_available = river.get("data_available", True)
    return_period = river.get("return_period", "100")
    buildings_total = int(river.get("buildings_total", 0) or 0)
    buildings_threatened = int(river.get("buildings_threatened", 0) or 0)

    if not river_available:
        river_headline = "No modelled GloFAS river-flood inundation detected at this location"
    else:
        river_headline = f"{river_class} river flood risk at the {return_period}-year return period"
        if buildings_total > 0:
            river_headline = f"{buildings_threatened} of {buildings_total} buildings sit in the river flood zone at the {return_period}-year return period"

    river_insight = ""
    if summary.get("local_elevation_used") and summary.get("relative_elevation_m") is not None:
        rel = float(summary["relative_elevation_m"])
        if rel < -0.3:
            river_insight = (
                f"Site elevation note: your surveyed points average {abs(rel):.1f} m BELOW the "
                "surrounding terrain - low-lying sites are more prone to ponding and slow drainage "
                "during heavy rainfall (see the Surface-Water/Rainfall page), independent of the "
                "river-based score above."
            )
        elif rel > 0.3:
            river_insight = (
                f"Site elevation note: your surveyed points average {rel:.1f} m ABOVE the "
                "surrounding terrain, which is generally favorable for drainage."
            )
        else:
            river_insight = "Site elevation note: your surveyed points are close to the surrounding terrain average."

    c = _render_hazard_report_pdf(
        output_path,
        river_png,
        report_label="River Flood Risk",
        subtitle="Fluvial exposure - JRC/Copernicus GloFAS river-flood modeling",
        risk_score=str(river.get("risk_score", "0")),
        risk_class=river_class,
        class_color=str(river.get("class_color") or "#94a3b8"),
        headline=river_headline,
        stat_cards=[
            ("Mean Depth (m)", str(river.get("mean_depth_m", "-"))),
            ("Max Depth (m)", str(river.get("max_depth_m", "-"))),
            ("Inundation (%)", str(river.get("inundation_percent", "-"))),
            ("Buildings Threatened", f"{buildings_threatened} / {buildings_total}" if buildings_total else "-"),
        ],
        component_bars=[
            ("Depth", float(river.get("depth_score", 0) or 0), "#1d4ed8"),
            ("Inundation", float(river.get("inundation_score", 0) or 0), "#0ea5e9"),
            ("River prox.", float(river.get("river_proximity_score", 0) or 0), "#38bdf8"),
        ],
        method=str(river.get("method", "")),
        footnotes=[
            f"Return period: {return_period} years  |  analysis buffer: {summary.get('buffer_m', '1000')} m around the plot.",
            "Screening-level assessment only - not a legal flood determination or substitute for a licensed hydrological survey.",
        ],
        legend=legend,
        note=str(river.get("note", "")),
        insight=river_insight,
        map_has_own_legend=True,
        references=river.get("references") or [],
        c=c,
    )
    c.showPage()

    # --- Floodplain Susceptibility detail page ---
    floodplain_class = str(floodplain.get("risk_class", "Low"))
    fp_buildings_total = int(floodplain.get("buildings_total", 0) or 0)
    fp_buildings_threatened = int(floodplain.get("buildings_threatened", 0) or 0)
    floodplain_headline = f"{floodplain_class} floodplain susceptibility for this site"
    if fp_buildings_total > 0:
        floodplain_headline = f"{fp_buildings_threatened} of {fp_buildings_total} buildings sit on susceptible ground"

    c = _render_hazard_report_pdf(
        output_path,
        floodplain_png,
        report_label="Floodplain Susceptibility",
        subtitle="Elevation relative to the surrounding drainage network - MERIT Hydro HAND",
        risk_score=str(floodplain.get("risk_score", "0")),
        risk_class=floodplain_class,
        class_color=str(floodplain.get("class_color") or "#94a3b8"),
        headline=floodplain_headline,
        stat_cards=[
            ("Median HAND (m)", str(floodplain.get("hand_median_m", "-"))),
            ("HAND P10 (m)", str(floodplain.get("hand_p10_m", "-"))),
            ("Dist. to major river (m)", str(floodplain.get("distance_to_major_river_m", "-"))),
            ("Buildings Flagged", f"{fp_buildings_threatened} / {fp_buildings_total}" if fp_buildings_total else "-"),
        ],
        component_bars=[],
        method=str(floodplain.get("method", "")),
        footnotes=[
            "Height Above Nearest Drainage (HAND) is a provisional, pre-validation modelling signal "
            "- a documented judgment call, not a peer-reviewed universal threshold.",
            "Distance to major river and upstream contributing area are supporting context only and "
            "do not affect the score shown here.",
        ],
        legend=legend,
        note=str(floodplain.get("note", "")),
        map_has_own_legend=True,
        references=floodplain.get("references") or [],
        c=c,
    )
    c.showPage()

    # --- Surface-Water/Rainfall detail page ---
    rainfall_class = str(rainfall.get("risk_class", "Low"))
    rf_buildings_total = int(rainfall.get("buildings_total", 0) or 0)
    rf_buildings_threatened = int(rainfall.get("buildings_threatened", 0) or 0)
    rainfall_headline = "Experimental surface-water/rainfall susceptibility estimate for this site"
    if rf_buildings_total > 0:
        rainfall_headline = f"{rf_buildings_threatened} of {rf_buildings_total} buildings sit on susceptible ground (experimental estimate)"

    c = _render_hazard_report_pdf(
        output_path,
        pluvial_png,
        report_label="Surface-Water / Rainfall Flood Risk",
        subtitle="Pluvial susceptibility - terrain, rainfall, soil & built-surface modeling (EXPERIMENTAL)",
        # Neutral grey "Experimental" tag, not a colored risk tier - a blind validation found this
        # branch does not reliably discriminate (see the note text below), so it must never look
        # like validated risk-tier evidence in a colored badge the way River/Floodplain do.
        risk_score="",
        risk_class="Experimental",
        class_color="#94a3b8",
        headline=rainfall_headline,
        stat_cards=[
            ("Design Rainfall (mm)", str(rainfall.get("design_rainfall_mm", "-"))),
            ("Soil Group", str(rainfall.get("hydrologic_soil_group", "-"))),
            ("Impervious Surface (%)", str(rainfall.get("impervious_fraction_pct", "-"))),
            ("Buildings Flagged", f"{rf_buildings_threatened} / {rf_buildings_total}" if rf_buildings_total else "-"),
        ],
        component_bars=[
            ("Terrain", float(rainfall.get("terrain_score", 0) or 0), "#b45309"),
            ("Runoff", float(rainfall.get("runoff_score", 0) or 0), "#f59e0b"),
        ],
        method=str(rainfall.get("method", "")),
        footnotes=[
            f"Analysis buffer: {summary.get('buffer_m', '1000')} m around the plot.",
            "Susceptibility assessment based on terrain, land cover, soil, and historical extreme-rainfall "
            "characteristics - not a prediction that any specific future storm will flood the property.",
        ],
        legend=legend,
        note=str(rainfall.get("note", "")),
        map_has_own_legend=True,
        references=rainfall.get("references") or [],
        c=c,
    )
    c.showPage()
    c.save()


def render_erosion_report_pdf(output_path: str, overlay_png: bytes, summary: Dict[str, object]) -> None:
    risk_class = str(summary.get("risk_class", "Low"))
    class_color = str(summary.get("class_color") or "#22c55e")
    slope_source = str(summary.get("slope_source") or "unavailable")
    buildings_total = int(summary.get("buildings_total", 0) or 0)
    buildings_threatened = int(summary.get("buildings_threatened", 0) or 0)

    insight = ""
    if slope_source == "local_survey":
        insight = "Slope is computed directly from your uploaded survey points, not the global 30 m elevation model - a more accurate local measurement."
    elif slope_source == "global_dem":
        insight = "Slope is estimated from a global 30 m elevation model. Upload your own surveyed elevation points for a more precise local measurement."

    headline = f"{risk_class} erosion susceptibility for this site"
    if buildings_total > 0:
        headline = f"{buildings_threatened} of {buildings_total} buildings sit on erosion-prone slopes"

    _render_hazard_report_pdf(
        output_path,
        overlay_png,
        report_label="Erosion Risk Report",
        subtitle="Soil erosion & slope stability screening",
        risk_score=str(summary.get("risk_score", "0")),
        risk_class=risk_class,
        class_color=class_color,
        headline=headline,
        stat_cards=[
            ("Mean Slope (°)", str(summary.get("mean_slope_deg", "-"))),
            ("Max Slope (°)", str(summary.get("max_slope_deg", "-"))),
            ("Vegetation (NDVI)", str(summary.get("mean_ndvi", "-"))),
            ("Buildings At Risk", f"{buildings_threatened} / {buildings_total}" if buildings_total else "-"),
        ],
        component_bars=[
            ("Slope", float(summary.get("slope_score", 0) or 0), "#f97316"),
            ("Bare ground", float(summary.get("vegetation_score", 0) or 0), "#eab308"),
            ("Drainage conc.", float(summary.get("drainage_score", 0) or 0), "#dc2626"),
        ],
        method=(
            "A susceptibility index adapted from the RUSLE erosion-factor framework, not a full "
            "soil-loss estimate: slope is sampled from a global 30 m DEM, vegetation cover from a "
            "recent cloud-free Sentinel-2 NDVI composite, and drainage concentration from the "
            "HydroSHEDS flow-accumulation network.\n"
            "Score = 50% slope (normalized to 25°) + 30% bare-ground exposure (low NDVI) "
            "+ 20% proximity to a natural drainage channel."
        ),
        footnotes=[
            "Analysis buffer: 500 m around the plot  |  vegetation sampled from imagery in the last 180 days.",
            "Screening-level assessment only - not a substitute for a licensed geotechnical survey.",
        ],
        legend=summary.get("legend") or [],
        note=str(summary.get("note", "")),
        insight=insight,
        map_has_own_legend=True,
        references=summary.get("references") or [],
    )


def render_lulc_report_pdf(output_path: str, overlay_png: bytes, summary: Dict[str, object]) -> None:
    class_areas = summary.get("class_areas") or []
    # component_bars values are FRACTIONS (0-1), not percentages - _render_hazard_report_pdf's
    # shared bar-chart block multiplies by 100 itself (matching flood/erosion's own component
    # scores, which are already 0-1). Passing already-0-100 percentages here would double them.
    bar_data = [(c["label"], float(c.get("pct", 0)) / 100.0, c["color"]) for c in class_areas]

    dominant_label = summary.get("dominant_class") or "mixed cover"
    dominant_pct = summary.get("dominant_pct")
    headline = (
        f"Land cover composition — {dominant_label} dominant ({dominant_pct}% of site)"
        if dominant_pct is not None
        else f"Land cover composition — {dominant_label} dominant"
    )

    _render_hazard_report_pdf(
        output_path,
        overlay_png,
        report_label="Land Cover Report",
        subtitle="Land use / land cover composition",
        risk_score="",
        risk_class="",
        class_color="#000000",
        headline=headline,
        stat_cards=[
            ("Classes Present", str(summary.get("class_count", "-"))),
            ("Dominant Class", str(dominant_label)),
            ("Total Area (ha)", str(summary.get("total_area_ha", "-"))),
        ],
        component_bars=bar_data,
        method=(
            "Land cover is classified from Esri's 10m resolution Sentinel-2-derived Annual Land "
            "Cover dataset (Karra et al., 2021 - Impact Observatory / Esri, via Google Earth "
            "Engine). Per-class area is a pixel-count histogram over the site boundary at native "
            "10m resolution.\n"
            "The map's hillshade terrain background is derived from the Copernicus GLO-30 global DEM."
        ),
        footnotes=[
            f"Analysis buffer: {summary.get('buffer_m', '500')} m around the plot (map context only "
            "- land cover % is measured over the plot boundary itself).",
            "Datasource: Esri Land Cover, Copernicus GLO-30 DEM.",
            "Informational land cover summary only - not a risk score or hazard determination.",
        ],
        legend=summary.get("legend") or [],
        note=str(summary.get("note", "")),
        map_has_own_legend=True,
        references=summary.get("references") or [],
        show_risk_badge=False,
    )
