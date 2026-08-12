import io
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

from app.utils.green_pdf import _draw_rounded_box, _draw_stat_card, _draw_bar_chart

BRAND_BAR_COLOR = "#0b1120"
BRAND_ACCENT = "#38bdf8"


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
) -> None:
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    content_top = _draw_hazard_header(c, width, height, report_label=report_label, subtitle=subtitle)

    y = content_top - 26
    _draw_risk_badge(c, 36, y - 40, risk_class, risk_score, class_color)

    c.setFillColor(HexColor("#111827"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(200, y - 6, headline)
    content_bottom = y - 22
    if note:
        c.setFont("Helvetica", 8.5)
        c.setFillColor(HexColor("#4b5563"))
        content_bottom = _draw_wrapped(c, note, 200, y - 22, width - 236, 11)

    if insight:
        insight_box_y = content_bottom - 4
        _draw_rounded_box(c, 200, insight_box_y - 18, width - 236, 22, 5, fill_color=HexColor("#eff6ff"), stroke_color=HexColor("#bfdbfe"))
        c.setFillColor(HexColor("#1d4ed8"))
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

    # Footer.
    footer_y = 46
    c.setStrokeColor(HexColor("#e5e7eb"))
    c.setLineWidth(0.5)
    c.line(36, footer_y + 12, width - 36, footer_y + 12)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(HexColor("#6b7280"))
    for i, line in enumerate(footnotes):
        c.drawString(36, footer_y - (i * 10), line)
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(HexColor("#111827"))
    c.drawRightString(width - 36, footer_y, "LandCheck")

    c.showPage()
    c.save()


def render_flood_report_pdf(output_path: str, overlay_png: bytes, summary: Dict[str, object]) -> None:
    risk_class = str(summary.get("risk_class", "Low"))
    class_color = str(summary.get("class_color") or "#22c55e")
    return_period = summary.get("return_period", "100")
    buildings_total = int(summary.get("buildings_total", 0) or 0)
    buildings_threatened = int(summary.get("buildings_threatened", 0) or 0)
    is_proxy = str(summary.get("flood_data_source") or "glofas") == "local_terrain_proxy"

    if is_proxy:
        headline = f"{risk_class} local flood/ponding susceptibility (terrain-based estimate)"
        if buildings_total > 0:
            headline = f"{buildings_threatened} of {buildings_total} buildings sit on susceptible ground (terrain-based estimate)"
    else:
        headline = f"{risk_class} flood risk at the {return_period}-year return period"
        if buildings_total > 0:
            headline = f"{buildings_threatened} of {buildings_total} buildings sit in the flood zone at the {return_period}-year return period"

    insight = ""
    if summary.get("local_elevation_used") and summary.get("relative_elevation_m") is not None:
        rel = float(summary["relative_elevation_m"])
        if rel < -0.3:
            insight = (
                f"Site elevation note: your surveyed points average {abs(rel):.1f} m BELOW the "
                "surrounding terrain - low-lying sites are more prone to ponding and slow drainage "
                "during heavy rainfall, independent of the river-based score above."
            )
        elif rel > 0.3:
            insight = (
                f"Site elevation note: your surveyed points average {rel:.1f} m ABOVE the "
                "surrounding terrain, which is generally favorable for drainage."
            )
        else:
            insight = "Site elevation note: your surveyed points are close to the surrounding terrain average."

    if is_proxy:
        stat_cards = [
            ("Slope (deg)", str(summary.get("terrain_slope_deg", "-"))),
            ("Rel. Elevation (m)", str(summary.get("terrain_depression_m", "-"))),
            ("Dist. to Drainage (m)", str(summary.get("distance_to_river_m", "-"))),
            ("Buildings Flagged", f"{buildings_threatened} / {buildings_total}" if buildings_total else "-"),
        ]
        component_bars = [
            ("Low-lying terrain", float(summary.get("terrain_depression_score", 0) or 0), "#b45309"),
            ("Flatness", float(summary.get("terrain_flatness_score", 0) or 0), "#f59e0b"),
            ("Drainage prox.", float(summary.get("terrain_drainage_score", 0) or 0), "#fbbf24"),
        ]
        method = (
            "GloFAS has no modeled river-flood extent at this location, so this is a local "
            "terrain-based susceptibility estimate instead - NOT official GloFAS river flood "
            "modeling. Buildings are real OpenStreetMap footprints, flagged where the local "
            "susceptibility surface exceeds 60%.\n"
            "Score = 40% low-lying terrain (elevation relative to the surrounding 300m) "
            "+ 35% flatness (slope) + 25% proximity to the nearest natural drainage line."
        )
    else:
        stat_cards = [
            ("Mean Depth (m)", str(summary.get("mean_depth_m", "-"))),
            ("Max Depth (m)", str(summary.get("max_depth_m", "-"))),
            ("Inundation (%)", str(summary.get("inundation_percent", "-"))),
            ("Buildings Threatened", f"{buildings_threatened} / {buildings_total}" if buildings_total else "-"),
        ]
        component_bars = [
            ("Depth", float(summary.get("depth_score", 0) or 0), "#1d4ed8"),
            ("Inundation", float(summary.get("inundation_score", 0) or 0), "#0ea5e9"),
            ("River prox.", float(summary.get("river_proximity_score", 0) or 0), "#38bdf8"),
        ]
        method = (
            "Flood depth is sampled from the JRC/CEMS GloFAS global hazard model at the chosen "
            "return period, inside a 1km buffer around the plot. Buildings are real OpenStreetMap "
            "footprints, flagged as threatened where the interpolated depth surface exceeds 5cm.\n"
            "Score = 60% mean depth (normalized to 3m) + 25% inundated area fraction "
            "+ 15% proximity to a major river channel."
        )

    _render_hazard_report_pdf(
        output_path,
        overlay_png,
        report_label="Flood Risk Report",
        subtitle="River & rainfall flood exposure screening",
        risk_score=str(summary.get("risk_score", "0")),
        risk_class=risk_class,
        class_color=class_color,
        headline=headline,
        stat_cards=stat_cards,
        component_bars=component_bars,
        method=method,
        footnotes=[
            f"Return period: {return_period} years - analysis buffer: {summary.get('buffer_m', '1000')} m around plot.",
            "Source: JRC/CEMS GloFAS Flood Hazard v2.1, WWF HydroSHEDS, OpenStreetMap. For screening only, not a legal flood determination.",
        ],
        legend=summary.get("legend") or [],
        note=str(summary.get("note", "")),
        insight=insight,
        map_has_own_legend=True,
    )


def render_erosion_report_pdf(output_path: str, overlay_png: bytes, summary: Dict[str, object]) -> None:
    risk_class = str(summary.get("risk_class", "Low"))
    class_color = str(summary.get("class_color") or "#22c55e")
    slope_source = str(summary.get("slope_source") or "unavailable")
    buildings_total = int(summary.get("buildings_total", 0) or 0)
    buildings_threatened = int(summary.get("buildings_threatened", 0) or 0)

    insight = ""
    if slope_source == "local_survey":
        insight = "Slope is computed directly from your uploaded survey points, not the global 30m elevation model - a more accurate local measurement."
    elif slope_source == "global_dem":
        insight = "Slope is estimated from a global 30m elevation model. Upload your own surveyed elevation points for a more precise local measurement."

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
            ("Mean Slope (deg)", str(summary.get("mean_slope_deg", "-"))),
            ("Max Slope (deg)", str(summary.get("max_slope_deg", "-"))),
            ("Vegetation (NDVI)", str(summary.get("mean_ndvi", "-"))),
            ("Buildings At Risk", f"{buildings_threatened} / {buildings_total}" if buildings_total else "-"),
        ],
        component_bars=[
            ("Slope", float(summary.get("slope_score", 0) or 0), "#f97316"),
            ("Bare ground", float(summary.get("vegetation_score", 0) or 0), "#eab308"),
            ("Drainage conc.", float(summary.get("drainage_score", 0) or 0), "#dc2626"),
        ],
        method=(
            "A susceptibility index (not a full RUSLE soil-loss estimate): slope is sampled from "
            "a global 30m DEM, vegetation cover from a recent cloud-free Sentinel-2 NDVI "
            "composite, and drainage concentration from HydroSHEDS flow accumulation.\n"
            "Score = 50% slope (normalized to 25deg) + 30% bare-ground exposure (low NDVI) "
            "+ 20% proximity to a natural drainage channel."
        ),
        footnotes=[
            "Analysis buffer: 500 m around plot. Vegetation sampled from imagery in the last 180 days.",
            "Source: Copernicus DEM GLO-30, Sentinel-2 SR, WWF HydroSHEDS. Screening level only, not a geotechnical survey.",
        ],
        legend=summary.get("legend") or [],
        note=str(summary.get("note", "")),
        insight=insight,
        map_has_own_legend=True,
    )
