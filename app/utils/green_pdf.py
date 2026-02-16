from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from datetime import datetime


def _draw_rounded_box(c, x, y, w, h, r, fill_color=None, stroke_color=None):
    """Draw a rounded rectangle."""
    c.saveState()
    if fill_color:
        c.setFillColor(fill_color)
    if stroke_color:
        c.setStrokeColor(stroke_color)
    else:
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.setLineWidth(0.5)
    c.roundRect(x, y, w, h, r, stroke=1, fill=1 if fill_color else 0)
    c.restoreState()


def _draw_stat_card(c, x, y, w, h, label, value, sub=None, color=None):
    """Draw a metric card with large value and label."""
    bg = HexColor("#f8faf9") if not color else color
    _draw_rounded_box(c, x, y, w, h, 4, fill_color=bg)
    c.setFillColorRGB(0.15, 0.15, 0.15)
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(x + w / 2, y + h - 28, str(value))
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawCentredString(x + w / 2, y + h - 40, label)
    if sub:
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.55, 0.55, 0.55)
        c.drawCentredString(x + w / 2, y + 6, sub)


def _draw_bar_chart(c, x, y, w, h, data, title=""):
    """Draw a simple horizontal bar chart. data = list of (label, value, color_hex)."""
    if not data:
        return
    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.15, 0.15, 0.15)
    c.drawString(x, y + h + 4, title)
    max_val = max((d[1] for d in data), default=1) or 1
    bar_h = min(14, (h - 4) / max(len(data), 1))
    gap = 3
    cy = y + h - bar_h - 2
    for label, value, color_hex in data:
        bar_w = max((value / max_val) * (w - 100), 2)
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.3, 0.3, 0.3)
        c.drawRightString(x + 88, cy + 3, str(label)[:14])
        c.setFillColor(HexColor(color_hex))
        c.rect(x + 92, cy, bar_w, bar_h - 2, stroke=0, fill=1)
        c.setFillColorRGB(0.2, 0.2, 0.2)
        c.setFont("Helvetica", 6.5)
        c.drawString(x + 94 + bar_w, cy + 2, f"{value:.1f}")
        cy -= (bar_h + gap)
        if cy < y:
            break


def _draw_mini_line_chart(c, x, y, w, h, points, title="", y_label="", line_color="#16a34a"):
    """Draw a mini line chart for trending. points = list of (x_val, y_val)."""
    if not points or len(points) < 2:
        return
    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.15, 0.15, 0.15)
    c.drawString(x, y + h + 4, title)

    # Chart area
    cx = x + 30
    cy = y + 14
    cw = w - 40
    ch = h - 24

    # Border
    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.setLineWidth(0.5)
    c.rect(cx, cy, cw, ch, stroke=1, fill=0)

    x_vals = [p[0] for p in points]
    y_vals = [p[1] for p in points]
    x_min, x_max = min(x_vals), max(x_vals)
    y_min = 0
    y_max = max(y_vals) * 1.1 if max(y_vals) > 0 else 1

    def to_px(xv, yv):
        px = cx + ((xv - x_min) / max(x_max - x_min, 1)) * cw
        py = cy + ((yv - y_min) / max(y_max - y_min, 1)) * ch
        return px, py

    # Grid lines
    c.setStrokeColorRGB(0.92, 0.92, 0.92)
    c.setLineWidth(0.3)
    for i in range(1, 4):
        gy = cy + (ch * i / 4)
        c.line(cx, gy, cx + cw, gy)
        c.setFont("Helvetica", 5.5)
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.drawRightString(cx - 2, gy - 2, f"{y_min + (y_max - y_min) * i / 4:.0f}")

    # Draw line
    c.setStrokeColor(HexColor(line_color))
    c.setLineWidth(1.5)
    path = c.beginPath()
    px0, py0 = to_px(points[0][0], points[0][1])
    path.moveTo(px0, py0)
    for xv, yv in points[1:]:
        px, py = to_px(xv, yv)
        path.lineTo(px, py)
    c.drawPath(path, stroke=1, fill=0)

    # Draw dots
    c.setFillColor(HexColor(line_color))
    for xv, yv in points:
        px, py = to_px(xv, yv)
        c.circle(px, py, 2, stroke=0, fill=1)

    # Y-axis label
    if y_label:
        c.setFont("Helvetica", 6)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawString(x, y + h - 6, y_label)


def _format_delay_label(delay_days, delay_context=None):
    """Human-readable delay string. delay is measured in days."""
    try:
        if delay_days is None or delay_days == "":
            return "-"
        days = int(delay_days)
    except Exception:
        return str(delay_days)

    if delay_context == "completion":
        if days > 0:
            return f"{days}d late"
        if days < 0:
            return f"{abs(days)}d early"
        return "on time"

    if delay_context == "schedule":
        if days > 0:
            return f"{days}d overdue"
        if days < 0:
            return f"due in {abs(days)}d"
        return "due today"

    if days > 0:
        return f"{days}d"
    if days < 0:
        return f"-{abs(days)}d"
    return "0d"


def _render_executive_summary(c, width, height, project, kpi_snapshot, carbon_data, kpi_trend):
    """Render the Executive Summary page - page 1 of the report."""
    # Header bar
    c.setFillColor(HexColor("#0b3d24"))
    c.rect(0, height - 80, width, 80, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(40, height - 42, "LandCheck Green")
    c.setFont("Helvetica", 11)
    c.drawString(40, height - 60, "Executive Donor Report")
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.8, 0.95, 0.85)
    c.drawRightString(width - 40, height - 42, f"Generated: {datetime.utcnow().strftime('%d %B %Y')}")

    # Project info
    y = height - 110
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, y, project.get("name", "Project"))
    y -= 16
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    info_parts = []
    if project.get("location_text"):
        info_parts.append(f"Location: {project['location_text']}")
    if project.get("sponsor"):
        info_parts.append(f"Sponsor: {project['sponsor']}")
    c.drawString(40, y, " | ".join(info_parts))

    # Key metrics cards - row 1
    stats = kpi_snapshot or {}
    y -= 30
    card_w = 122
    card_h = 52
    gap = 10
    start_x = 40

    total = stats.get("trees_total", 0)
    healthy = stats.get("trees_healthy", 0)
    survival = stats.get("survival_rate", 0)
    dead = stats.get("trees_dead_or_removed", 0)
    attention = stats.get("trees_attention", 0)

    _draw_stat_card(c, start_x, y - card_h, card_w, card_h, "Trees Planted", f"{total:,}", color=HexColor("#e8f5e9"))
    _draw_stat_card(c, start_x + card_w + gap, y - card_h, card_w, card_h, "Healthy / Alive", f"{healthy:,}", color=HexColor("#e8f5e9"))
    _draw_stat_card(c, start_x + 2 * (card_w + gap), y - card_h, card_w, card_h, "Survival Rate", f"{survival}%", color=HexColor("#e8f5e9"))
    _draw_stat_card(c, start_x + 3 * (card_w + gap), y - card_h, card_w, card_h, "Needs Attention", f"{attention:,}", color=HexColor("#fff8e1"))

    # Carbon impact cards - row 2
    y -= card_h + 16
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(40, y, "Carbon Impact (IPCC Tier 1 + Chave et al. 2014)")
    y -= 8

    co2_current = carbon_data.get("current_co2_tonnes", 0) if carbon_data else 0
    co2_annual = carbon_data.get("annual_co2_tonnes", 0) if carbon_data else 0
    co2_projected = carbon_data.get("projected_lifetime_co2_tonnes", 0) if carbon_data else 0
    co2_per_tree = carbon_data.get("co2_per_tree_avg_kg", 0) if carbon_data else 0

    _draw_stat_card(c, start_x, y - card_h, card_w, card_h, "CO2 Sequestered", f"{co2_current:.1f} t", sub="tonnes to date", color=HexColor("#e0f2f1"))
    _draw_stat_card(c, start_x + card_w + gap, y - card_h, card_w, card_h, "Annual Rate", f"{co2_annual:.1f} t/yr", sub="tonnes CO2 / year", color=HexColor("#e0f2f1"))
    _draw_stat_card(c, start_x + 2 * (card_w + gap), y - card_h, card_w, card_h, "40-Year Projection", f"{co2_projected:.0f} t", sub="lifetime tonnes", color=HexColor("#e0f2f1"))
    _draw_stat_card(c, start_x + 3 * (card_w + gap), y - card_h, card_w, card_h, "Avg per Tree", f"{co2_per_tree:.1f} kg", sub="current CO2 / tree", color=HexColor("#e0f2f1"))

    missing_age = int(carbon_data.get("trees_missing_age_data", 0)) if carbon_data else 0
    fallback_age = int(carbon_data.get("trees_with_fallback_age", 0)) if carbon_data else 0
    pending_review = int(carbon_data.get("trees_pending_review", 0)) if carbon_data else 0
    warning_extra = 0
    if co2_current <= 0 or co2_projected <= 0:
        warning_text = (
            f"CO2 is low/zero. Missing age data: {missing_age} | "
            f"Fallback age used: {fallback_age} | Pending review trees: {pending_review}"
        )
        c.setFillColor(HexColor("#fff3cd"))
        c.setStrokeColor(HexColor("#f0ad4e"))
        c.roundRect(40, y - card_h - 18, width - 80, 12, 3, fill=1, stroke=1)
        c.setFillColor(HexColor("#7a4b00"))
        c.setFont("Helvetica", 7.5)
        c.drawString(44, y - card_h - 14, warning_text[:170])
        warning_extra = 18

    # Task & operations summary row
    y -= card_h + 20 + warning_extra
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(40, y, "Operations Summary")
    y -= 8

    tasks_total = stats.get("tasks_total", 0)
    tasks_approved = stats.get("tasks_approved", 0)
    tasks_overdue = stats.get("tasks_overdue", 0)
    evidence_rate = stats.get("evidence_complete_rate", 0)
    evidence_required = stats.get("evidence_required_tasks", 0)
    evidence_complete = stats.get("evidence_complete_tasks", 0)

    _draw_stat_card(c, start_x, y - card_h, card_w, card_h, "Total Tasks", f"{tasks_total:,}")
    _draw_stat_card(c, start_x + card_w + gap, y - card_h, card_w, card_h, "Approved", f"{tasks_approved:,}", color=HexColor("#e8f5e9"))
    _draw_stat_card(c, start_x + 2 * (card_w + gap), y - card_h, card_w, card_h, "Overdue", f"{tasks_overdue:,}", color=HexColor("#ffebee") if tasks_overdue > 0 else None)
    evidence_card_color = (
        HexColor("#e8f5e9")
        if evidence_rate >= 85
        else HexColor("#fff8e1")
        if evidence_rate >= 60
        else HexColor("#ffebee")
    )
    evidence_sub = (
        f"{evidence_complete}/{evidence_required} required-proof tasks"
        if evidence_required
        else "No required-proof tasks yet"
    )
    _draw_stat_card(
        c,
        start_x + 3 * (card_w + gap),
        y - card_h,
        card_w,
        card_h,
        "Evidence Rate",
        f"{evidence_rate}%",
        sub=evidence_sub,
        color=evidence_card_color,
    )

    age_survival = stats.get("age_survival", {}) if isinstance(stats.get("age_survival"), dict) else {}
    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    y -= card_h + 14
    c.drawString(40, y, "Age-Based Survival Checkpoints (from planting date)")
    y -= 8
    age_card_w = (width - 80 - (2 * gap)) / 3
    age_card_h = 40
    for idx, checkpoint in enumerate((30, 90, 180)):
        bucket = age_survival.get(f"day_{checkpoint}", {}) if isinstance(age_survival, dict) else {}
        eligible = int(bucket.get("eligible_trees", 0) or 0)
        survived = int(bucket.get("survived_trees", 0) or 0)
        rate = float(bucket.get("survival_rate", 0) or 0)
        value_text = f"{rate:.1f}%" if eligible > 0 else "n/a"
        sub_text = f"{survived}/{eligible} surviving"
        _draw_stat_card(
            c,
            40 + idx * (age_card_w + gap),
            y - age_card_h,
            age_card_w,
            age_card_h,
            f"{checkpoint}-Day Survival",
            value_text,
            sub=sub_text,
            color=HexColor("#f2fbf5"),
        )
    missing_planting = int(age_survival.get("trees_missing_planting_date", 0) or 0) if isinstance(age_survival, dict) else 0
    if missing_planting > 0:
        c.setFont("Helvetica", 6.5)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(40, y - age_card_h - 10, f"Note: {missing_planting} tree(s) excluded from age-based cohorts due to missing planting date.")

    # Charts area - bottom half
    y -= age_card_h + (28 if missing_planting > 0 else 18)
    chart_w = (width - 100) / 2

    # Survival + evidence trend charts (left)
    survival_points = []
    evidence_points = []
    trend_first_label = ""
    trend_last_label = ""

    def _trend_label(raw_value):
        raw = str(raw_value or "").strip()
        if not raw:
            return ""
        parsed = None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
            except Exception:
                parsed = None
        if not parsed:
            return raw[:10]
        return parsed.strftime("%b %Y")

    if kpi_trend and len(kpi_trend) >= 2:
        trend_first_label = _trend_label(kpi_trend[0].get("snapshot_at"))
        trend_last_label = _trend_label(kpi_trend[-1].get("snapshot_at"))
        for i, snap in enumerate(kpi_trend):
            metrics = snap.get("metrics", {})
            survival_points.append((i, float(metrics.get("survival_rate", 0) or 0)))
            evidence_points.append((i, float(metrics.get("evidence_complete_rate", 0) or 0)))
    else:
        current_survival = float(stats.get("survival_rate", 0) or 0)
        current_evidence = float(stats.get("evidence_complete_rate", 0) or 0)
        survival_points = [(0, current_survival), (1, current_survival)]
        evidence_points = [(0, current_evidence), (1, current_evidence)]

    left_base_y = y - 148
    mini_h = 62
    gap_h = 34
    top_chart_y = left_base_y + mini_h + gap_h
    _draw_mini_line_chart(
        c,
        40,
        top_chart_y,
        chart_w,
        mini_h,
        survival_points,
        title="Survival Trend (Planting Cohorts)",
        y_label="%",
        line_color="#16a34a",
    )
    c.setFont("Helvetica", 6.5)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.drawString(40, top_chart_y - 10, "Context: monthly cumulative healthy share across planting cohorts from first planting date.")
    if trend_first_label or trend_last_label:
        c.drawString(40, top_chart_y - 18, f"Period: {trend_first_label} to {trend_last_label}".strip())

    _draw_mini_line_chart(
        c,
        40,
        left_base_y,
        chart_w,
        mini_h,
        evidence_points,
        title="Evidence Trend (Task Activity)",
        y_label="%",
        line_color="#9a5800",
    )
    c.setFont("Helvetica", 6.5)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.drawString(40, left_base_y - 12, "Context: monthly cumulative completion of required-proof task evidence.")
    if trend_first_label or trend_last_label:
        c.drawString(40, left_base_y - 20, f"Period: {trend_first_label} to {trend_last_label}".strip())

    # Top species by CO2 (right)
    top_species = carbon_data.get("top_species", []) if carbon_data else []
    if top_species:
        bar_data = []
        colors = ["#2e7d32", "#43a047", "#66bb6a", "#81c784", "#a5d6a7",
                  "#c8e6c9", "#e8f5e9", "#b9f6ca", "#69f0ae", "#00e676"]
        for i, sp in enumerate(top_species[:7]):
            bar_data.append((sp["species"][:14], sp["co2_kg"], colors[i % len(colors)]))
        _draw_bar_chart(c, 40 + chart_w + 20, y - 140, chart_w, 130, bar_data,
                        title="Top Species by CO2 (kg)")
        c.setFont("Helvetica", 6.5)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(40 + chart_w + 20, y - 148, "Context: estimated current stock by species group.")

    # CO2 projection chart (bottom, full width)
    co2_projection = carbon_data.get("projection", []) if carbon_data else []
    if co2_projection and len(co2_projection) >= 2:
        proj_points = [(p["year_offset"], p["cumulative_co2_tonnes"]) for p in co2_projection]
        _draw_mini_line_chart(c, 40, y - 310, width - 80, 140, proj_points,
                              title="CO2 Projection (tonnes, cumulative over 30 years)", y_label="tonnes")
        c.setFont("Helvetica", 6.5)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(40, y - 318, "Context: projection assumes current living trees continue modeled growth.")

    # Footer
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.drawString(40, 24, "Methodology: IPCC Tier 1 defaults + Chave et al. (2014) pantropical allometric equation")
    c.drawRightString(width - 40, 24, f"LandCheck Green | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")


def render_green_report_pdf(
    output_path: str,
    project: dict,
    rows: list[dict],
    map_png: bytes | None = None,
    map_rows: list[dict] | None = None,
    map_view: dict | None = None,
    maintenance_rows: list[dict] | None = None,
    donor_rows: list[dict] | None = None,
    kpi_snapshot: dict | None = None,
    carbon_data: dict | None = None,
    kpi_trend: list[dict] | None = None,
):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    # =====================================================================
    # PAGE 1: Executive Summary
    # =====================================================================
    _render_executive_summary(c, width, height, project, kpi_snapshot, carbon_data, kpi_trend)

    # =====================================================================
    # PAGE 2: Tree Records + Maintenance
    # =====================================================================
    c.showPage()
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, height - 50, "LandCheck Green Report")

    c.setFont("Helvetica", 11)
    c.drawString(40, height - 80, f"Project: {project.get('name', '')}")
    c.drawString(40, height - 98, f"Location: {project.get('location_text', '')}")
    c.drawString(40, height - 116, f"Sponsor: {project.get('sponsor', '')}")
    c.drawString(40, height - 134, f"Created: {project.get('created_at', '')}")

    stats = project.get("stats", {})
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 170, "Summary")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 188, f"Total: {stats.get('total', 0)}")
    c.drawString(120, height - 188, f"Alive: {stats.get('alive', 0)}")
    c.drawString(200, height - 188, f"Dead: {stats.get('dead', 0)}")
    c.drawString(280, height - 188, f"Needs Attention: {stats.get('needs_attention', 0)}")
    c.drawString(420, height - 188, f"Survival: {stats.get('survival_rate', 0)}%")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 220, "Tree Records + Maintenance (latest 200)")

    def draw_tree_header(y_pos: float):
        c.setFont("Helvetica-Bold", 8)
        c.drawString(40, y_pos, "Tree ID")
        c.drawString(84, y_pos, "Species")
        c.drawString(160, y_pos, "Status")
        c.drawString(222, y_pos, "Planting Date")
        c.drawString(300, y_pos, "Maint #")
        c.drawString(344, y_pos, "Maint Type(s)")
        c.drawString(474, y_pos, "Last Maint Date")

    y = height - 240
    draw_tree_header(y)
    y -= 12
    c.setFont("Helvetica", 7.5)

    for r in rows:
        if y < 60:
            c.showPage()
            y = height - 60
            draw_tree_header(y)
            y -= 12
            c.setFont("Helvetica", 7.5)

        c.drawString(40, y, str(r.get("id", "")))
        c.drawString(84, y, (str(r.get("species", "")) or "-")[:16])
        c.drawString(160, y, (str(r.get("status", "")) or "-")[:13])
        c.drawString(222, y, str(r.get("planting_date", "") or "-")[:14])
        c.drawString(300, y, str(r.get("maintenance_count", 0) or 0))
        c.drawString(344, y, (str(r.get("maintenance_types", "")) or "-")[:30])
        c.drawString(474, y, str(r.get("last_maintenance_date", "") or "-")[:16])
        y -= 11

    # =====================================================================
    # PAGE: Tree Map Snapshot
    # =====================================================================
    c.showPage()
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 50, "Tree Map Snapshot")

    # Legend (compact row)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, height - 72, "Legend")
    c.setFont("Helvetica", 9)
    legend_items = [
        ("Alive", (0.13, 0.77, 0.37)),
        ("Needs Attention", (0.96, 0.62, 0.04)),
        ("Dead", (0.94, 0.27, 0.27)),
        ("Pending Planting", (0.23, 0.51, 0.96)),
    ]
    legend_x = 90
    legend_y = height - 74
    for label, color in legend_items:
        c.setFillColorRGB(*color)
        c.rect(legend_x, legend_y - 6, 10, 10, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(legend_x + 14, legend_y - 4, label)
        legend_x += 95

    map_x = 40
    map_top = height - 92
    map_y = 60
    map_w = width - 80
    map_h = max(map_top - map_y, 200)
    c.setStrokeColorRGB(0, 0, 0)

    map_rows = map_rows or rows

    img_x = map_x
    img_y = map_y
    img_w = map_w
    img_h = map_h
    img = None
    if map_png:
        try:
            from reportlab.lib.utils import ImageReader
            import io

            img = ImageReader(io.BytesIO(map_png))
            src_w, src_h = img.getSize()
            if src_w and src_h:
                scale = min(map_w / src_w, map_h / src_h)
                img_w = src_w * scale
                img_h = src_h * scale
                img_x = map_x
                img_y = map_y + (map_h - img_h)
            c.drawImage(img, img_x, img_y, img_w, img_h, preserveAspectRatio=True, anchor="sw")
        except Exception:
            img = None
            map_png = None

    c.setStrokeColorRGB(0, 0, 0)
    c.rect(img_x, img_y, img_w, img_h, stroke=1, fill=0)

    lats = [r.get("lat") for r in map_rows if r.get("lat") is not None]
    lngs = [r.get("lng") for r in map_rows if r.get("lng") is not None]
    if not map_png and lats and lngs:
        status_colors = {
            "alive": (0.13, 0.77, 0.37),
            "needs_attention": (0.96, 0.62, 0.04),
            "dead": (0.94, 0.27, 0.27),
            "pending_planting": (0.23, 0.51, 0.96),
        }

        def mercator_px(lng_v: float, lat_v: float, world_size: float):
            import math
            x_v = (lng_v + 180.0) / 360.0 * world_size
            siny = math.sin(math.radians(lat_v))
            siny = min(max(siny, -0.9999), 0.9999)
            y_v = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * world_size
            return x_v, y_v

        if map_png and img is not None and map_view and map_view.get("zoom") is not None:
            try:
                import math
                zoom = float(map_view.get("zoom"))
                center_lng = float(map_view.get("lng"))
                center_lat = float(map_view.get("lat"))
                world_size = 512 * (2 ** zoom)
                cx, cy_m = mercator_px(center_lng, center_lat, world_size)
                src_w, src_h = img.getSize()
                scale = img_w / src_w if src_w else 1
                for r in map_rows:
                    if r.get("lat") is None or r.get("lng") is None:
                        continue
                    px_w, py_w = mercator_px(float(r["lng"]), float(r["lat"]), world_size)
                    dx = px_w - cx
                    dy = py_w - cy_m
                    x_px = (src_w / 2) + dx
                    y_px = (src_h / 2) + dy
                    px = img_x + x_px * scale
                    py = img_y + (src_h - y_px) * scale
                    color = status_colors.get(str(r.get("status", "")).lower(), (0.13, 0.77, 0.37))
                    c.setFillColorRGB(*color)
                    c.setStrokeColorRGB(0, 0, 0)
                    c.circle(px, py, 3.4, stroke=1, fill=1)
                lats = []
            except Exception:
                pass

        if lats and lngs:
            min_lat, max_lat = min(lats), max(lats)
            min_lng, max_lng = min(lngs), max(lngs)
            lat_span = max(max_lat - min_lat, 1e-6)
            lng_span = max(max_lng - min_lng, 1e-6)
            pad_lat = lat_span * 0.05
            pad_lng = lng_span * 0.05
            min_lat -= pad_lat
            max_lat += pad_lat
            min_lng -= pad_lng
            max_lng += pad_lng
            lat_span = max(max_lat - min_lat, 1e-6)
            lng_span = max(max_lng - min_lng, 1e-6)

            for r in map_rows:
                if r.get("lat") is None or r.get("lng") is None:
                    continue
                px = img_x + ((r["lng"] - min_lng) / lng_span) * img_w
                py = img_y + ((r["lat"] - min_lat) / lat_span) * img_h
                color = status_colors.get(str(r.get("status", "")).lower(), (0.13, 0.77, 0.37))
                c.setFillColorRGB(*color)
                c.setStrokeColorRGB(0, 0, 0)
                c.circle(px, py, 3.4, stroke=1, fill=1)

    # =====================================================================
    # PAGE: Maintenance Summary
    # =====================================================================
    if maintenance_rows:
        c.showPage()
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, height - 50, "Maintenance Summary By Tree")
        c.setFont("Helvetica", 9)
        c.drawString(40, height - 68, "Columns: maintenance type, last date, and number of times.")

        y = height - 92
        c.setFont("Helvetica-Bold", 8)
        c.drawString(40, y, "Tree ID")
        c.drawString(92, y, "Times")
        c.drawString(134, y, "Done")
        c.drawString(172, y, "Pending")
        c.drawString(226, y, "Overdue")
        c.drawString(282, y, "Last Type")
        c.drawString(376, y, "Type(s)")
        c.drawString(494, y, "Last Date")
        y -= 12

        c.setFont("Helvetica", 7.5)
        for row in maintenance_rows:
            if y < 52:
                c.showPage()
                y = height - 60
                c.setFont("Helvetica-Bold", 8)
                c.drawString(40, y, "Tree ID")
                c.drawString(92, y, "Times")
                c.drawString(134, y, "Done")
                c.drawString(172, y, "Pending")
                c.drawString(226, y, "Overdue")
                c.drawString(282, y, "Last Type")
                c.drawString(376, y, "Type(s)")
                c.drawString(494, y, "Last Date")
                y -= 12
                c.setFont("Helvetica", 7.5)

            c.drawString(40, y, str(row.get("tree_id", "")))
            c.drawString(92, y, str(row.get("maintenance_count", 0) or 0))
            c.drawString(134, y, str(row.get("maintenance_done", 0) or 0))
            c.drawString(172, y, str(row.get("maintenance_pending", 0) or 0))
            c.drawString(226, y, str(row.get("maintenance_overdue", 0) or 0))
            c.drawString(282, y, (str(row.get("last_maintenance_type", "")) or "-")[:16])
            c.drawString(376, y, (str(row.get("maintenance_types", "")) or "-")[:24])
            c.drawString(494, y, str(row.get("last_maintenance_date", "") or "-")[:16])
            y -= 11

    # =====================================================================
    # PAGE: Donor + Review Intelligence
    # =====================================================================
    if kpi_snapshot or donor_rows:
        c.showPage()
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, height - 50, "Donor + Review Intelligence")
        y = height - 74
        if kpi_snapshot:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(40, y, "KPI Snapshot")
            y -= 14
            c.setFont("Helvetica", 8.5)
            c.drawString(
                40,
                y,
                (
                    f"Trees: {kpi_snapshot.get('trees_total', 0)} | "
                    f"Healthy: {kpi_snapshot.get('trees_healthy', 0)} | "
                    f"Attention: {kpi_snapshot.get('trees_attention', 0)} | "
                    f"Pending Planting: {kpi_snapshot.get('trees_pending_planting', 0)} | "
                    f"Survival: {kpi_snapshot.get('survival_rate', 0)}%"
                ),
            )
            y -= 12
            c.drawString(
                40,
                y,
                (
                    f"Tasks: {kpi_snapshot.get('tasks_total', 0)} | "
                    f"Open: {kpi_snapshot.get('tasks_open', 0)} | "
                    f"Submitted: {kpi_snapshot.get('tasks_submitted', 0)} | "
                    f"Approved: {kpi_snapshot.get('tasks_approved', 0)} | "
                    f"Rejected: {kpi_snapshot.get('tasks_rejected', 0)} | "
                    f"Overdue: {kpi_snapshot.get('tasks_overdue', 0)}"
                ),
            )
            y -= 12
            evidence_rate = kpi_snapshot.get("evidence_complete_rate", 0)
            evidence_complete = kpi_snapshot.get("evidence_complete_tasks", 0)
            evidence_required = kpi_snapshot.get("evidence_required_tasks", 0)
            if evidence_required:
                c.drawString(40, y, f"Evidence completeness: {evidence_rate}% ({evidence_complete}/{evidence_required} required-proof tasks)")
            else:
                c.drawString(40, y, f"Evidence completeness: {evidence_rate}% (no required-proof tasks)")
            y -= 12
            # CO2 KPI line
            co2_t = kpi_snapshot.get("co2_current_tonnes", 0)
            co2_a = kpi_snapshot.get("co2_annual_tonnes", 0)
            co2_p = kpi_snapshot.get("co2_projected_lifetime_tonnes", 0)
            if co2_t or co2_a or co2_p:
                c.drawString(
                    40, y,
                    f"CO2 Sequestered: {co2_t} tonnes | Annual: {co2_a} t/yr | 40-Year Projection: {co2_p} tonnes"
                )
                y -= 12
            y -= 4

        if donor_rows:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(40, y, "Recent Review Timeline")
            y -= 12
            c.setFont("Helvetica-Bold", 7.5)
            c.drawString(40, y, "Task")
            c.drawString(78, y, "Tree")
            c.drawString(110, y, "Assignee")
            c.drawString(186, y, "Type")
            c.drawString(248, y, "Status/Review")
            c.drawString(332, y, "Due")
            c.drawString(378, y, "Submitted")
            c.drawString(436, y, "Reviewed")
            c.drawString(494, y, "Delay (days)")
            y -= 11
            c.setFont("Helvetica-Oblique", 6.8)
            c.setFillColorRGB(0.45, 0.45, 0.45)
            c.drawString(40, y, "Delay legend: positive = late/overdue, negative = early/due in.")
            y -= 10
            c.setFont("Helvetica", 7.2)
            c.setFillColorRGB(0.1, 0.1, 0.1)
            for row in donor_rows[:170]:
                if y < 50:
                    c.showPage()
                    y = height - 50
                    c.setFont("Helvetica-Bold", 7.5)
                    c.drawString(40, y, "Task")
                    c.drawString(78, y, "Tree")
                    c.drawString(110, y, "Assignee")
                    c.drawString(186, y, "Type")
                    c.drawString(248, y, "Status/Review")
                    c.drawString(332, y, "Due")
                    c.drawString(378, y, "Submitted")
                    c.drawString(436, y, "Reviewed")
                    c.drawString(494, y, "Delay (days)")
                    y -= 11
                    c.setFont("Helvetica-Oblique", 6.8)
                    c.setFillColorRGB(0.45, 0.45, 0.45)
                    c.drawString(40, y, "Delay legend: positive = late/overdue, negative = early/due in.")
                    y -= 10
                    c.setFont("Helvetica", 7.2)
                    c.setFillColorRGB(0.1, 0.1, 0.1)
                c.drawString(40, y, f"#{row.get('task_id', '-')}")
                c.drawString(78, y, f"#{row.get('tree_id', '-')}")
                c.drawString(110, y, str(row.get("assignee_name", "-"))[:16])
                c.drawString(186, y, str(row.get("task_type", "-"))[:12])
                c.drawString(248, y, f"{str(row.get('status', '-'))[:7]}/{str(row.get('review_state', '-'))[:8]}")
                c.drawString(332, y, str(row.get("due_date", "") or "-")[:10])
                c.drawString(378, y, str(row.get("submitted_at", "") or "-")[:10])
                c.drawString(436, y, str(row.get("reviewed_at", "") or "-")[:10])
                c.drawString(494, y, _format_delay_label(row.get("delay_days"), row.get("delay_context")))
                y -= 10

    c.save()


def render_green_work_report_pdf(output_path: str, project: dict, stats: dict):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, height - 50, "LandCheck Work Report")

    c.setFont("Helvetica", 11)
    c.drawString(40, height - 80, f"Project: {project.get('name', '')}")
    c.drawString(40, height - 98, f"Location: {project.get('location_text', '')}")
    c.drawString(40, height - 116, f"Sponsor: {project.get('sponsor', '')}")

    maintenance_by_assignee = {
        str(r.get("assignee_name", "")): dict(r)
        for r in stats.get("maintenance_by_assignee", [])
    }
    assignee_rows = [dict(r) for r in stats.get("orders", [])]

    seen_names = {str(r.get("assignee_name", "")) for r in assignee_rows}
    for assignee_name, maintenance_row in maintenance_by_assignee.items():
        if assignee_name in seen_names:
            continue
        assignee_rows.append({
            "assignee_name": assignee_name,
            "orders": 0,
            "target_trees": 0,
            "planted_count": 0,
            "maintenance_total": maintenance_row.get("maintenance_total", 0),
            "maintenance_done": maintenance_row.get("maintenance_done", 0),
            "maintenance_pending": maintenance_row.get("maintenance_pending", 0),
            "maintenance_overdue": maintenance_row.get("maintenance_overdue", 0),
            "maintenance_types": maintenance_row.get("maintenance_types", ""),
            "last_maintenance_date": maintenance_row.get("last_maintenance_date"),
        })

    for row in assignee_rows:
        extra = maintenance_by_assignee.get(str(row.get("assignee_name", "")), {})
        row["maintenance_total"] = int(extra.get("maintenance_total", row.get("maintenance_total", 0)) or 0)
        row["maintenance_done"] = int(extra.get("maintenance_done", row.get("maintenance_done", 0)) or 0)
        row["maintenance_pending"] = int(extra.get("maintenance_pending", row.get("maintenance_pending", 0)) or 0)
        row["maintenance_overdue"] = int(extra.get("maintenance_overdue", row.get("maintenance_overdue", 0)) or 0)
        row["maintenance_types"] = extra.get("maintenance_types", row.get("maintenance_types", "")) or ""
        row["last_maintenance_date"] = extra.get("last_maintenance_date", row.get("last_maintenance_date"))

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 150, "Assignee Summary + Maintenance")

    y = height - 170
    c.setFont("Helvetica-Bold", 8)
    c.drawString(40, y, "Assignee")
    c.drawString(130, y, "Orders")
    c.drawString(168, y, "Target")
    c.drawString(206, y, "Planted")
    c.drawString(252, y, "Maint #")
    c.drawString(296, y, "Done/Pend/Over")
    c.drawString(396, y, "Type(s)")
    c.drawString(502, y, "Last Date")
    y -= 12
    c.setFont("Helvetica", 7.3)

    for r in assignee_rows:
        if y < 60:
            c.showPage()
            y = height - 60
            c.setFont("Helvetica-Bold", 8)
            c.drawString(40, y, "Assignee")
            c.drawString(130, y, "Orders")
            c.drawString(168, y, "Target")
            c.drawString(206, y, "Planted")
            c.drawString(252, y, "Maint #")
            c.drawString(296, y, "Done/Pend/Over")
            c.drawString(396, y, "Type(s)")
            c.drawString(502, y, "Last Date")
            y -= 12
            c.setFont("Helvetica", 7.3)

        c.drawString(40, y, str(r.get("assignee_name", ""))[:15])
        c.drawString(130, y, str(r.get("orders", 0)))
        c.drawString(168, y, str(r.get("target_trees", 0)))
        c.drawString(206, y, str(r.get("planted_count", 0)))
        c.drawString(252, y, str(r.get("maintenance_total", 0)))
        c.drawString(
            296,
            y,
            f"{r.get('maintenance_done', 0)}/{r.get('maintenance_pending', 0)}/{r.get('maintenance_overdue', 0)}",
        )
        c.drawString(396, y, str(r.get("maintenance_types", "") or "-")[:23])
        c.drawString(502, y, str(r.get("last_maintenance_date", "") or "-")[:12])
        y -= 11

    maintenance_by_type = stats.get("maintenance_by_type", [])
    if maintenance_by_type:
        c.showPage()
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, height - 50, "Maintenance Type Activity")
        c.setFont("Helvetica", 9)
        c.drawString(40, height - 68, "Columns: type, date, and number of times by assignee.")

        y = height - 90
        c.setFont("Helvetica-Bold", 8)
        c.drawString(40, y, "Assignee")
        c.drawString(170, y, "Maintenance Type")
        c.drawString(330, y, "Number of Times")
        c.drawString(450, y, "Last Date")
        y -= 12
        c.setFont("Helvetica", 8)

        for row in maintenance_by_type:
            if y < 55:
                c.showPage()
                y = height - 60
                c.setFont("Helvetica-Bold", 8)
                c.drawString(40, y, "Assignee")
                c.drawString(170, y, "Maintenance Type")
                c.drawString(330, y, "Number of Times")
                c.drawString(450, y, "Last Date")
                y -= 12
                c.setFont("Helvetica", 8)

            c.drawString(40, y, str(row.get("assignee_name", ""))[:22])
            c.drawString(170, y, str(row.get("task_type", ""))[:24])
            c.drawString(330, y, str(row.get("maintenance_times", 0)))
            c.drawString(450, y, str(row.get("last_maintenance_date", "") or "-")[:16])
            y -= 11

    c.save()
