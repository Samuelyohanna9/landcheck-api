from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from datetime import datetime
import io
import os
import ssl
from urllib.parse import urlparse
from urllib.request import Request, urlopen


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


def _draw_species_daily_line_chart(c, x, y, w, h, species_daily, title=""):
    """Draw multi-species daily survival lines on a shared day axis."""
    rows = species_daily.get("species", []) if isinstance(species_daily, dict) else []
    if not isinstance(rows, list) or len(rows) == 0:
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(x, y + h * 0.5, "No species daily survival timeline yet.")
        return

    selected_rows = sorted(
        rows,
        key=lambda row: int(row.get("trees_with_planting_date", 0) or 0),
        reverse=True,
    )[:8]

    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.15, 0.15, 0.15)
    c.drawString(x, y + h + 4, title)

    cx = x + 28
    cy = y + 14
    cw = w - 36
    ch = h - 24
    if cw <= 20 or ch <= 20:
        return

    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.setLineWidth(0.5)
    c.rect(cx, cy, cw, ch, stroke=1, fill=0)

    # Horizontal grid and y labels.
    for tick in (0, 20, 40, 60, 80, 100):
        py = cy + (tick / 100.0) * ch
        c.setStrokeColorRGB(0.92, 0.92, 0.92)
        c.setLineWidth(0.3)
        c.line(cx, py, cx + cw, py)
        c.setFont("Helvetica", 5.8)
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.drawRightString(cx - 3, py - 2, str(tick))

    processed = []
    max_day = 0
    for row in selected_rows:
        point_map = {}
        for point in row.get("points", []) or []:
            try:
                day = int(point.get("day_since_species_start", point.get("day", 0)) or 0)
                rate = float(point.get("survival_rate", point.get("value", 0)) or 0)
            except Exception:
                continue
            if day < 0:
                continue
            rate = min(max(rate, 0.0), 100.0)
            point_map[day] = rate
        if not point_map:
            continue
        line_points = sorted(point_map.items(), key=lambda item: item[0])
        if len(line_points) > 120:
            step = max(int(len(line_points) / 120), 1)
            sampled = [line_points[i] for i in range(0, len(line_points), step)]
            if sampled[-1][0] != line_points[-1][0]:
                sampled.append(line_points[-1])
            line_points = sampled
        max_day = max(max_day, int(line_points[-1][0]))
        processed.append((row, line_points))

    if not processed:
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(x, y + h * 0.5, "No valid species points to render.")
        return

    day_domain = max(max_day, 30)
    x_ticks = sorted(
        set(([0, 7, 14, 21, 30] if day_domain <= 30 else [0, 30, 60, 90, 120, 150, 180]) + [max_day, day_domain])
    )
    x_ticks = [tick for tick in x_ticks if 0 <= tick <= day_domain]

    for tick in x_ticks:
        px = cx + (tick / max(day_domain, 1)) * cw
        c.setStrokeColorRGB(0.90, 0.93, 0.91)
        c.setLineWidth(0.25)
        c.line(px, cy, px, cy + ch)
        c.setFont("Helvetica", 5.6)
        c.setFillColorRGB(0.46, 0.46, 0.46)
        c.drawCentredString(px, y + 2, f"d{tick}")

    colors = [
        "#16a34a", "#0ea5e9", "#f97316", "#8b5cf6",
        "#dc2626", "#0891b2", "#7c3aed", "#15803d",
    ]
    legend_items = []
    for idx, (row, points) in enumerate(processed):
        color = colors[idx % len(colors)]
        c.setStrokeColor(HexColor(color))
        c.setLineWidth(1.2)
        path = c.beginPath()
        first_px = cx + (points[0][0] / max(day_domain, 1)) * cw
        first_py = cy + (points[0][1] / 100.0) * ch
        path.moveTo(first_px, first_py)
        for day, rate in points[1:]:
            px = cx + (day / max(day_domain, 1)) * cw
            py = cy + (rate / 100.0) * ch
            path.lineTo(px, py)
        c.drawPath(path, stroke=1, fill=0)

        c.setFillColor(HexColor(color))
        c.circle(first_px, first_py, 1.5, stroke=0, fill=1)
        last_px = cx + (points[-1][0] / max(day_domain, 1)) * cw
        last_py = cy + (points[-1][1] / 100.0) * ch
        c.circle(last_px, last_py, 1.6, stroke=0, fill=1)

        label = str(row.get("species_label") or row.get("species_key") or "Unknown")[:14]
        trees = int(row.get("trees_with_planting_date", 0) or 0)
        legend_items.append((label, trees, color))

    # Compact legend below the chart.
    legend_y = y - 8
    legend_x = x
    c.setFont("Helvetica", 6.2)
    for label, trees, color in legend_items:
        text = f"{label} ({trees})"
        text_w = c.stringWidth(text, "Helvetica", 6.2)
        block_w = 12 + text_w + 8
        if legend_x + block_w > x + w:
            legend_y -= 8
            legend_x = x
        if legend_y < y - 22:
            break
        c.setFillColor(HexColor(color))
        c.circle(legend_x + 3, legend_y + 2, 1.8, stroke=0, fill=1)
        c.setFillColorRGB(0.26, 0.35, 0.30)
        c.drawString(legend_x + 8, legend_y, text)
        legend_x += block_w


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


def _load_photo_reader(photo_url, image_cache):
    raw = str(photo_url or "").strip()
    if not raw:
        return None
    if raw.startswith("//"):
        raw = f"https:{raw}"
    if raw in image_cache:
        return image_cache[raw]

    data = None
    reader = None
    parsed = urlparse(raw)
    try:
        if parsed.scheme in {"http", "https"}:
            req = Request(raw, headers={"User-Agent": "LandCheck-Green-Report/1.0"})
            try:
                with urlopen(req, timeout=8) as response:
                    data = response.read()
            except Exception:
                if parsed.scheme == "https":
                    context = ssl._create_unverified_context()
                    with urlopen(req, timeout=8, context=context) as response:
                        data = response.read()
        else:
            if raw.startswith("/"):
                base_url = (os.getenv("GREEN_REPORT_PHOTO_BASE_URL") or os.getenv("BACKEND_URL") or "").strip().rstrip("/")
                if base_url:
                    remote_url = f"{base_url}{raw}"
                    req = Request(remote_url, headers={"User-Agent": "LandCheck-Green-Report/1.0"})
                    try:
                        with urlopen(req, timeout=8) as response:
                            data = response.read()
                    except Exception:
                        pass
            candidate_paths = []
            if raw.startswith("file://"):
                candidate_paths.append(raw.replace("file://", "", 1))
            candidate_paths.append(raw)
            if raw.startswith("/"):
                candidate_paths.append(os.path.join("/app", raw.lstrip("/")))
            else:
                candidate_paths.append(os.path.join("/app", raw))
            for path in candidate_paths:
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        data = f.read()
                    break
        if data:
            reader = ImageReader(io.BytesIO(data))
    except Exception:
        reader = None

    image_cache[raw] = reader
    return reader


def _draw_photo_card(c, x, y, w, h, row, image_cache):
    _draw_rounded_box(c, x, y, w, h, 5, fill_color=HexColor("#f8faf9"), stroke_color=HexColor("#d7e5db"))

    pad = 6
    meta_h = 48
    img_x = x + pad
    img_y = y + meta_h + pad
    img_w = max(w - (pad * 2), 20)
    img_h = max(h - meta_h - (pad * 2), 20)

    reader = _load_photo_reader(row.get("photo_url"), image_cache)
    if reader is not None:
        try:
            src_w, src_h = reader.getSize()
            if src_w and src_h:
                scale = min(img_w / src_w, img_h / src_h)
                draw_w = max(src_w * scale, 2)
                draw_h = max(src_h * scale, 2)
                draw_x = img_x + (img_w - draw_w) / 2
                draw_y = img_y + (img_h - draw_h) / 2
                c.drawImage(reader, draw_x, draw_y, draw_w, draw_h, preserveAspectRatio=True, anchor="c")
            else:
                raise ValueError("Invalid source image dimensions")
        except Exception:
            c.setStrokeColorRGB(0.8, 0.8, 0.8)
            c.rect(img_x, img_y, img_w, img_h, stroke=1, fill=0)
            c.setFont("Helvetica-Oblique", 7)
            c.setFillColorRGB(0.45, 0.45, 0.45)
            c.drawCentredString(img_x + img_w / 2, img_y + img_h / 2, "Photo unavailable")
    else:
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.rect(img_x, img_y, img_w, img_h, stroke=1, fill=0)
        c.setFont("Helvetica-Oblique", 7)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawCentredString(img_x + img_w / 2, img_y + img_h / 2, "Photo unavailable")

    tree_id = row.get("id", "-")
    species = str(row.get("species") or "-")[:22]
    status = str(row.get("status") or "-")[:14]
    planting_date = str(row.get("planting_date") or "-")[:10]
    owner = str(row.get("created_by") or "-")[:16]
    custodian_name = str(row.get("custodian_name") or "-")[:16]
    visit_label = str(row.get("visit_label") or "").strip()[:26]
    task_id = str(row.get("task_id") or "").strip()

    text_y = y + 26
    c.setFont("Helvetica-Bold", 7.4)
    c.setFillColorRGB(0.16, 0.16, 0.16)
    c.drawString(x + pad, text_y, f"Tree #{tree_id} | {species}")
    c.setFont("Helvetica", 6.8)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawString(x + pad, text_y - 10, f"Status: {status} | Date: {planting_date}")
    c.drawString(x + pad, text_y - 19, f"By: {owner} | Custodian: {custodian_name}")
    if visit_label:
        task_suffix = f" | Task #{task_id}" if task_id else ""
        c.drawString(x + pad, text_y - 28, f"{visit_label}{task_suffix}"[:44])


def _render_photo_appendix_pages(c, width, height, project, photo_rows, assignee_name=None):
    if not photo_rows:
        c.showPage()
        c.setFont("Helvetica-Bold", 15)
        c.drawString(40, height - 50, "Tree Photo Appendix")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawString(40, height - 70, "No tree photos available for the selected report scope.")
        return

    image_cache = {}
    per_page = 6
    card_cols = 2
    card_rows = 3
    margin_x = 36
    top_y = height - 82
    bottom_y = 44
    col_gap = 12
    row_gap = 12
    card_w = (width - (margin_x * 2) - col_gap) / card_cols
    card_h = (top_y - bottom_y - (row_gap * (card_rows - 1))) / card_rows
    total_pages = (len(photo_rows) + per_page - 1) // per_page

    for page_index, start in enumerate(range(0, len(photo_rows), per_page), start=1):
        c.showPage()
        c.setFont("Helvetica-Bold", 15)
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.drawString(40, height - 50, "Tree Photo Appendix")
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        scope_text = f"Project: {project.get('name', '-')}"
        if assignee_name:
            scope_text += f" | Assignee: {assignee_name}"
        c.drawString(40, height - 66, scope_text[:100])
        c.drawRightString(width - 40, height - 66, f"Page {page_index}/{total_pages}")

        page_rows = photo_rows[start : start + per_page]
        for idx, row in enumerate(page_rows):
            col = idx % card_cols
            row_idx = idx // card_cols
            x = margin_x + col * (card_w + col_gap)
            card_top = top_y - row_idx * (card_h + row_gap)
            y = card_top - card_h
            _draw_photo_card(c, x, y, card_w, card_h, row, image_cache)


def _render_executive_summary(c, width, height, project, kpi_snapshot, carbon_data, kpi_trend, species_daily_survival=None):
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

    c.setFont("Helvetica-Oblique", 7.3)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.drawString(
        40,
        82,
        "Detailed trend analytics (survival, species daily timeline, and carbon charts) continue on the next pages.",
    )

    # Footer
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.drawString(40, 24, "Methodology: IPCC Tier 1 defaults + Chave et al. (2014) pantropical allometric equation")
    c.drawRightString(width - 40, 24, f"LandCheck Green | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")


def _render_trend_analytics_pages(c, width, height, kpi_snapshot, carbon_data, kpi_trend, species_daily_survival=None):
    stats = kpi_snapshot or {}
    age_survival = stats.get("age_survival", {}) if isinstance(stats.get("age_survival"), dict) else {}
    species_breakdown = age_survival.get("species_breakdown", []) if isinstance(age_survival, dict) else []

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

    # -----------------------------------------------------------------
    # PAGE: Trend Analytics
    # -----------------------------------------------------------------
    c.showPage()
    c.setFont("Helvetica-Bold", 16)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(40, height - 50, "Trend Analytics")
    c.setFont("Helvetica", 8.2)
    c.setFillColorRGB(0.42, 0.42, 0.42)
    c.drawString(40, height - 66, "Daily species timeline and cohort survival displayed with dedicated spacing.")

    survival_points = []
    trend_first_label = ""
    trend_last_label = ""
    if kpi_trend and len(kpi_trend) >= 2:
        trend_first_label = _trend_label(kpi_trend[0].get("snapshot_at"))
        trend_last_label = _trend_label(kpi_trend[-1].get("snapshot_at"))
        for i, snap in enumerate(kpi_trend):
            metrics = snap.get("metrics", {})
            survival_points.append((i, float(metrics.get("survival_rate", 0) or 0)))
    else:
        current_survival = float(stats.get("survival_rate", 0) or 0)
        survival_points = [(0, current_survival), (1, current_survival)]

    survival_y = height - 275
    survival_h = 160
    _draw_mini_line_chart(
        c,
        40,
        survival_y,
        width - 80,
        survival_h,
        survival_points,
        title="Survival Trend (Planting Cohorts)",
        y_label="%",
        line_color="#16a34a",
    )
    c.setFont("Helvetica", 6.8)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.drawString(40, survival_y - 10, "Context: monthly cumulative healthy share across planting cohorts from first planting date.")
    if trend_first_label or trend_last_label:
        c.drawString(40, survival_y - 18, f"Period: {trend_first_label} to {trend_last_label}".strip())

    has_daily_species = (
        isinstance(species_daily_survival, dict)
        and isinstance(species_daily_survival.get("species"), list)
        and len(species_daily_survival.get("species") or []) > 0
    )
    if has_daily_species:
        _draw_species_daily_line_chart(
            c,
            40,
            220,
            width - 80,
            220,
            species_daily_survival,
            title="Species Survival Trend (Daily)",
        )
        c.setFont("Helvetica", 6.8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(40, 206, "Context: daily species survival from planting date using maintenance/task-review status history.")
    elif isinstance(species_breakdown, list) and len(species_breakdown) > 0:
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.15, 0.15, 0.15)
        c.drawString(40, 430, "Species Survival (30/90/180 days)")
        c.setFont("Helvetica-Bold", 7.2)
        c.drawString(40, 415, "Species")
        c.drawString(210, 415, "30d")
        c.drawString(260, 415, "90d")
        c.drawString(310, 415, "180d")
        c.drawString(370, 415, "Trees")
        c.setStrokeColorRGB(0.82, 0.88, 0.84)
        c.setLineWidth(0.5)
        c.line(40, 410, width - 40, 410)

        def _fmt_rate(bucket):
            eligible = int((bucket or {}).get("eligible_trees", 0) or 0)
            rate = float((bucket or {}).get("survival_rate", 0) or 0)
            return f"{rate:.0f}%" if eligible > 0 else "-"

        row_y = 396
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.22, 0.33, 0.27)
        for row in species_breakdown[:16]:
            c.drawString(40, row_y, str(row.get("species_label") or row.get("species_key") or "Unknown")[:26])
            c.drawString(210, row_y, _fmt_rate(row.get("day_30")))
            c.drawString(260, row_y, _fmt_rate(row.get("day_90")))
            c.drawString(310, row_y, _fmt_rate(row.get("day_180")))
            c.drawString(370, row_y, str(int(row.get("trees_with_planting_date", 0) or 0)))
            row_y -= 11
            if row_y < 120:
                break
    else:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(40, 300, "No species trend data available yet.")

    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.drawString(40, 24, "LandCheck Green | Trend Analytics")
    c.drawRightString(width - 40, 24, datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

    # -----------------------------------------------------------------
    # PAGE: Carbon Analytics
    # -----------------------------------------------------------------
    c.showPage()
    c.setFont("Helvetica-Bold", 16)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(40, height - 50, "Carbon Analytics")
    c.setFont("Helvetica", 8.2)
    c.setFillColorRGB(0.42, 0.42, 0.42)
    c.drawString(40, height - 66, "Species stock and projection charts on dedicated space to avoid overlaps.")

    top_species = carbon_data.get("top_species", []) if carbon_data else []
    if top_species:
        bar_data = []
        colors = ["#2e7d32", "#43a047", "#66bb6a", "#81c784", "#a5d6a7",
                  "#c8e6c9", "#e8f5e9", "#b9f6ca", "#69f0ae", "#00e676"]
        for i, sp in enumerate(top_species[:10]):
            bar_data.append((sp["species"][:18], sp["co2_kg"], colors[i % len(colors)]))
        _draw_bar_chart(c, 40, height - 360, width - 80, 190, bar_data, title="Top Species by CO2 (kg)")
        c.setFont("Helvetica", 6.8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(40, height - 368, "Context: estimated current stock by species group.")
    else:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(40, height - 240, "No species CO2 distribution available.")

    co2_projection = carbon_data.get("projection", []) if carbon_data else []
    if co2_projection and len(co2_projection) >= 2:
        proj_points = [(p["year_offset"], p["cumulative_co2_tonnes"]) for p in co2_projection]
        _draw_mini_line_chart(
            c,
            40,
            160,
            width - 80,
            230,
            proj_points,
            title="CO2 Projection (tonnes, cumulative over 30 years)",
            y_label="tonnes",
            line_color="#16a34a",
        )
        c.setFont("Helvetica", 6.8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(40, 148, "Context: projection assumes current living trees continue modeled growth.")
    else:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(40, 250, "No CO2 projection points available.")

    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.drawString(40, 24, "LandCheck Green | Carbon Analytics")
    c.drawRightString(width - 40, 24, datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))


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
    species_daily_survival: dict | None = None,
    photo_rows: list[dict] | None = None,
    include_photos: bool = False,
):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    # =====================================================================
    # PAGE 1: Executive Summary
    # =====================================================================
    _render_executive_summary(
        c,
        width,
        height,
        project,
        kpi_snapshot,
        carbon_data,
        kpi_trend,
        species_daily_survival=species_daily_survival,
    )

    # =====================================================================
    # PAGE 2-3: Trend + Carbon Analytics (dedicated pages for clean layout)
    # =====================================================================
    _render_trend_analytics_pages(
        c,
        width,
        height,
        kpi_snapshot,
        carbon_data,
        kpi_trend,
        species_daily_survival=species_daily_survival,
    )

    # =====================================================================
    # NEXT PAGE: Tree Records + Maintenance
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

    if include_photos:
        scoped_photo_rows = [dict(row) for row in (photo_rows or rows) if str(row.get("photo_url") or "").strip()]
        _render_photo_appendix_pages(
            c,
            width,
            height,
            project,
            scoped_photo_rows,
            assignee_name=project.get("report_assignee"),
        )

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


def render_green_custodian_report_pdf(
    output_path: str,
    project: dict,
    summary: dict,
    custodians: list[dict],
    distribution_events: list[dict],
    existing_trees: list[dict],
    supervision_photo_rows: list[dict] | None = None,
):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    def draw_report_header(subtitle: str):
        c.setFillColor(HexColor("#0b3d24"))
        c.rect(0, height - 70, width, 70, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica-Bold", 18)
        c.drawString(34, height - 40, "LandCheck Custodian Report")
        c.setFont("Helvetica", 10)
        c.drawString(34, height - 56, subtitle)
        c.setFont("Helvetica", 8.5)
        c.setFillColorRGB(0.82, 0.95, 0.86)
        c.drawRightString(width - 34, height - 40, datetime.utcnow().strftime("Generated %d %b %Y %H:%M UTC"))

    def reset_text():
        c.setFillColorRGB(0.12, 0.12, 0.12)
        c.setFont("Helvetica", 8.2)

    draw_report_header("Custodians, distribution events, supervision, and existing-tree intake.")
    c.setFillColorRGB(0.12, 0.12, 0.12)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(34, height - 90, str(project.get("name") or "Project"))
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.36, 0.36, 0.36)
    c.drawString(
        34,
        height - 106,
        f"Location: {project.get('location_text') or '-'} | Sponsor: {project.get('sponsor') or '-'}",
    )

    total_custodians = int(summary.get("total_custodians") or 0)
    verified_custodians = int(summary.get("verified_custodians") or 0)
    total_events = int(summary.get("distribution_events") or 0)
    total_seedlings = int(summary.get("seedlings_distributed") or 0)
    total_allocations = int(summary.get("distribution_allocations") or 0)
    supervision_target_total = int(summary.get("supervision_target_total") or 0)
    supervision_assigned_total = int(summary.get("supervision_assigned") or 0)
    supervision_done_total = int(summary.get("supervision_done") or 0)
    supervision_live_total = max(supervision_assigned_total - supervision_done_total, 0)
    linked_existing_trees = int(summary.get("existing_trees") or 0)

    card_y = height - 146
    card_w = (width - 68 - 12) / 3
    card_h = 44
    cards = [
        ("Custodians", f"{total_custodians} ({verified_custodians} verified)"),
        ("Distribution Events", f"{total_events} events | {total_seedlings} seedlings"),
        (
            "Allocations / Supervision",
            f"{total_allocations} allocations | Sup {supervision_done_total}/{supervision_target_total} done ({supervision_live_total} live)",
        ),
    ]
    cx = 34
    for label, value in cards:
        _draw_rounded_box(c, cx, card_y - card_h, card_w, card_h, 4, fill_color=HexColor("#f5fbf6"))
        c.setFillColorRGB(0.22, 0.22, 0.22)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(cx + 8, card_y - 15, label)
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.33, 0.33, 0.33)
        c.drawString(cx + 8, card_y - 29, value[:60])
        cx += card_w + 6

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.12, 0.12, 0.12)
    c.drawString(34, height - 206, "Custodian Registry")

    y = height - 222
    c.setFont("Helvetica-Bold", 7.7)
    c.drawString(34, y, "Name")
    c.drawString(124, y, "Type")
    c.drawString(174, y, "Contact")
    c.drawString(246, y, "Verify")
    c.drawString(292, y, "Allocated")
    c.drawString(344, y, "Trees")
    c.drawString(378, y, "Healthy")
    c.drawString(426, y, "Sup L/T/D")
    c.drawString(504, y, "Community")
    y -= 11
    reset_text()

    for row in custodians:
        if y < 48:
            c.showPage()
            draw_report_header("Custodian registry (continued).")
            y = height - 88
            c.setFont("Helvetica-Bold", 7.7)
            c.setFillColorRGB(0.12, 0.12, 0.12)
            c.drawString(34, y, "Name")
            c.drawString(124, y, "Type")
            c.drawString(174, y, "Contact")
            c.drawString(246, y, "Verify")
            c.drawString(292, y, "Allocated")
            c.drawString(344, y, "Trees")
            c.drawString(378, y, "Healthy")
            c.drawString(426, y, "Sup L/T/D")
            c.drawString(504, y, "Community")
            y -= 11
            reset_text()
        contact = str(row.get("phone") or row.get("email") or "-")
        supervision_target = int(row.get("supervision_target") or 0)
        supervision_done = int(row.get("supervision_done") or 0)
        supervision_assigned = int(row.get("supervision_assigned") or 0)
        supervision_live = max(supervision_assigned - supervision_done, 0)
        c.drawString(34, y, str(row.get("name") or "-")[:28])
        c.drawString(124, y, str(row.get("custodian_type") or "-").replace("_", " ")[:10])
        c.drawString(174, y, contact[:14])
        c.drawString(246, y, str(row.get("verification_status") or "pending")[:8])
        c.drawRightString(334, y, str(int(row.get("allocated_seedlings") or 0)))
        c.drawRightString(368, y, str(int(row.get("linked_trees") or 0)))
        c.drawRightString(414, y, str(int(row.get("healthy_trees") or 0)))
        c.drawString(426, y, f"{supervision_live}/{supervision_target}/{supervision_done}"[:12])
        c.drawString(504, y, str(row.get("community_name") or "-")[:12])
        y -= 10

    c.showPage()
    draw_report_header("Distribution events and allocations")
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.12, 0.12, 0.12)
    c.drawString(34, height - 88, "Distribution Events")
    y = height - 104
    c.setFont("Helvetica-Bold", 8)
    c.drawString(34, y, "Date")
    c.drawString(104, y, "Species")
    c.drawString(222, y, "Qty")
    c.drawString(270, y, "Distributed By")
    c.drawString(382, y, "Batch Ref")
    c.drawString(474, y, "Notes")
    y -= 11
    reset_text()

    if not distribution_events:
        c.drawString(34, y, "No distribution events recorded.")
        y -= 12
    else:
        for row in distribution_events:
            if y < 46:
                c.showPage()
                draw_report_header("Distribution events (continued)")
                y = height - 88
                c.setFont("Helvetica-Bold", 8)
                c.setFillColorRGB(0.12, 0.12, 0.12)
                c.drawString(34, y, "Date")
                c.drawString(104, y, "Species")
                c.drawString(222, y, "Qty")
                c.drawString(270, y, "Distributed By")
                c.drawString(382, y, "Batch Ref")
                c.drawString(474, y, "Notes")
                y -= 11
                reset_text()
            c.drawString(34, y, str(row.get("event_date") or "-")[:12])
            c.drawString(104, y, str(row.get("species") or "Mixed")[:20])
            c.drawRightString(246, y, str(int(row.get("quantity") or 0)))
            c.drawString(270, y, str(row.get("distributed_by") or "-")[:18])
            c.drawString(382, y, str(row.get("source_batch_ref") or "-")[:14])
            c.drawString(474, y, str(row.get("notes") or "-")[:20])
            y -= 10

    c.showPage()
    draw_report_header("Existing tree intake in this project")
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.12, 0.12, 0.12)
    c.drawString(34, height - 88, "Existing Trees")
    y = height - 104
    c.setFont("Helvetica-Bold", 8)
    c.drawString(34, y, "Tree ID")
    c.drawString(80, y, "Species")
    c.drawString(188, y, "Height")
    c.drawString(236, y, "Status")
    c.drawString(308, y, "Custodian")
    c.drawString(412, y, "Created By")
    c.drawString(498, y, "Date")
    y -= 11
    reset_text()

    if not existing_trees:
        c.drawString(34, y, "No existing trees captured yet.")
    else:
        for row in existing_trees:
            if y < 46:
                c.showPage()
                draw_report_header("Existing trees (continued)")
                y = height - 88
                c.setFont("Helvetica-Bold", 8)
                c.setFillColorRGB(0.12, 0.12, 0.12)
                c.drawString(34, y, "Tree ID")
                c.drawString(80, y, "Species")
                c.drawString(188, y, "Height")
                c.drawString(236, y, "Status")
                c.drawString(308, y, "Custodian")
                c.drawString(412, y, "Created By")
                c.drawString(498, y, "Date")
                y -= 11
                reset_text()
            height_val = row.get("tree_height_m")
            height_label = "-"
            try:
                if height_val is not None and float(height_val) >= 0:
                    height_label = f"{float(height_val):.2f}m"
            except Exception:
                height_label = "-"
            c.drawString(34, y, str(row.get("id") or "-"))
            c.drawString(80, y, str(row.get("species") or "-")[:20])
            c.drawString(188, y, height_label)
            c.drawString(236, y, str(row.get("status") or "-")[:12])
            c.drawString(308, y, str(row.get("custodian_name") or "-")[:18])
            c.drawString(412, y, str(row.get("created_by") or "-")[:16])
            c.drawString(498, y, str(row.get("created_at") or "-")[:10])
            y -= 10

    if supervision_photo_rows:
        _render_photo_appendix_pages(
            c,
            width,
            height,
            project,
            [dict(row) for row in supervision_photo_rows if str(row.get("photo_url") or "").strip()],
            assignee_name=None,
        )

    c.save()


def render_green_existing_trees_report_pdf(
    output_path: str,
    project: dict,
    rows: list[dict],
    summary: dict,
    include_photos: bool = False,
    photo_rows: list[dict] | None = None,
):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    def _fmt_num(value, digits=2):
        try:
            return f"{float(value):,.{digits}f}"
        except Exception:
            return "0.00"

    def _wrap_text(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
        words = str(text or "").split()
        if not words:
            return [""]
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if c.stringWidth(candidate, font_name, font_size) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    # ------------------------------------------------------------------
    # PAGE 1: Executive summary for Existing Trees + CO2
    # ------------------------------------------------------------------
    c.setFillColor(HexColor("#0b3d24"))
    c.rect(0, height - 78, width, 78, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(34, height - 42, "LandCheck Existing Trees Report")
    c.setFont("Helvetica", 10)
    c.drawString(34, height - 59, "Detailed existing-tree inventory with per-tree CO2 estimates and optional photo appendix")
    c.setFont("Helvetica", 8.5)
    c.setFillColorRGB(0.82, 0.95, 0.86)
    c.drawRightString(width - 34, height - 42, datetime.utcnow().strftime("Generated %d %b %Y %H:%M UTC"))

    c.setFillColorRGB(0.12, 0.12, 0.12)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(34, height - 98, str(project.get("name") or "Project"))
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.36, 0.36, 0.36)
    project_meta = []
    if project.get("location_text"):
        project_meta.append(f"Location: {project.get('location_text')}")
    if project.get("sponsor"):
        project_meta.append(f"Sponsor: {project.get('sponsor')}")
    project_meta.append(f"Existing trees in report: {int(summary.get('total_existing_trees', 0) or 0)}")
    c.drawString(34, height - 114, " | ".join(project_meta)[:130])

    card_y = height - 186
    card_w = (width - 34 - 34 - 10 - 10) / 3
    card_h = 58
    _draw_stat_card(
        c, 34, card_y, card_w, card_h,
        "Existing Trees",
        int(summary.get("total_existing_trees", 0) or 0),
        sub=f"Carbon scope: {int(summary.get('carbon_scope_rows', 0) or 0)}",
        color=HexColor("#eef7f0"),
    )
    _draw_stat_card(
        c, 34 + card_w + 10, card_y, card_w, card_h,
        "Current CO2 (t)",
        _fmt_num(summary.get("current_co2_tonnes", 0), 3),
        sub="Height-aware current stock where available",
        color=HexColor("#f3faf5"),
    )
    _draw_stat_card(
        c, 34 + (card_w + 10) * 2, card_y, card_w, card_h,
        "Annual CO2 (t/yr)",
        _fmt_num(summary.get("annual_co2_tonnes", 0), 3),
        sub=f"40Y proj: {_fmt_num(summary.get('projected_lifetime_co2_tonnes', 0), 2)} t",
        color=HexColor("#f8faf9"),
    )

    card_y2 = card_y - 70
    _draw_stat_card(
        c, 34, card_y2, card_w, card_h,
        "Healthy / Alive",
        int(summary.get("alive_trees", 0) or 0),
        sub=f"Attention {int(summary.get('attention_trees', 0) or 0)} | Dead {int(summary.get('dead_trees', 0) or 0)}",
        color=HexColor("#f7fbf8"),
    )
    _draw_stat_card(
        c, 34 + card_w + 10, card_y2, card_w, card_h,
        "Height Data",
        int(summary.get("rows_with_height", 0) or 0),
        sub=f"Missing height: {int(summary.get('rows_missing_height', 0) or 0)}",
        color=HexColor("#f8faf9"),
    )
    _draw_stat_card(
        c, 34 + (card_w + 10) * 2, card_y2, card_w, card_h,
        "Age Data Quality",
        int(summary.get("trees_missing_age_data", 0) or 0),
        sub=f"Fallback age source: {int(summary.get('trees_with_fallback_age', 0) or 0)}",
        color=HexColor("#fffaf2"),
    )

    top_species = summary.get("top_species") or []
    if top_species:
        bar_data = []
        colors = ["#2e7d32", "#43a047", "#66bb6a", "#81c784", "#0ea5e9", "#f97316", "#8b5cf6", "#dc2626"]
        for idx, item in enumerate(top_species[:8]):
            label = str(item.get("species") or "Unknown")
            bar_data.append((label[:18], float(item.get("co2_kg") or 0.0), colors[idx % len(colors)]))
        _draw_bar_chart(c, 34, height - 470, width - 68, 150, bar_data, title="Top Existing Species by Current CO2 (kg)")
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.42, 0.42, 0.42)
        c.drawString(34, height - 480, "Context: current existing-tree CO2 totals by species (rows excluded from carbon scope contribute 0).")
    else:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(34, height - 360, "No species CO2 summary available for the current existing-tree scope.")

    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.18, 0.18, 0.18)
    c.drawString(34, 134, "Methodology / Interpretation")
    c.setFont("Helvetica", 7.6)
    c.setFillColorRGB(0.34, 0.34, 0.34)
    line_y = 120
    for line in _wrap_text(str(summary.get("methodology") or ""), "Helvetica", 7.6, width - 68):
        c.drawString(34, line_y, line)
        line_y -= 10
    notes = [
        f"Projection years: {int(summary.get('projection_years', 40) or 40)} (modeled future growth for living trees in carbon scope).",
        "Current CO2 uses measured tree height (tree_height_m) when available; otherwise the species growth model estimates height.",
        "Rows with count_in_carbon_scope = false remain listed for traceability but CO2 values are reported as 0 in this export.",
    ]
    for note in notes:
        for line in _wrap_text(note, "Helvetica", 7.4, width - 68):
            c.drawString(34, line_y, line)
            line_y -= 9

    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.drawString(34, 24, "LandCheck Green | Existing Trees Detailed Export")
    c.drawRightString(width - 34, 24, f"Rows: {len(rows)}")

    # ------------------------------------------------------------------
    # PAGE 2+: Carbon detail table
    # ------------------------------------------------------------------
    def _draw_carbon_table_page_header(title: str):
        c.setFont("Helvetica-Bold", 13)
        c.setFillColorRGB(0.12, 0.12, 0.12)
        c.drawString(28, height - 46, title)
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(28, height - 60, "Per-tree CO2 columns are zero where the tree is excluded from carbon scope.")
        c.setFont("Helvetica-Bold", 7.2)
        y_head = height - 78
        c.setFillColorRGB(0.16, 0.16, 0.16)
        c.drawString(28, y_head, "Tree")
        c.drawString(52, y_head, "Species")
        c.drawString(156, y_head, "Status")
        c.drawString(214, y_head, "Date")
        c.drawRightString(287, y_head, "Age")
        c.drawRightString(323, y_head, "H(m)")
        c.drawRightString(381, y_head, "CO2 Now")
        c.drawRightString(433, y_head, "Annual")
        c.drawRightString(491, y_head, "40Y CO2")
        c.drawString(498, y_head, "Scope")
        c.line(28, y_head - 3, width - 28, y_head - 3)
        return y_head - 14

    c.showPage()
    y = _draw_carbon_table_page_header("Existing Trees CO2 Detail")
    c.setFont("Helvetica", 6.6)

    for row in rows:
        if y < 44:
            c.showPage()
            y = _draw_carbon_table_page_header("Existing Trees CO2 Detail (continued)")
            c.setFont("Helvetica", 6.6)

        height_val = "-"
        try:
            if row.get("tree_height_m") is not None:
                height_val = f"{float(row.get('tree_height_m')):.1f}"
        except Exception:
            height_val = "-"
        c.setFillColorRGB(0.14, 0.14, 0.14)
        c.drawString(28, y, f"#{row.get('id', '-')}")
        c.drawString(52, y, str(row.get("species") or "-")[:24])
        c.drawString(156, y, str(row.get("status") or "-")[:13])
        c.drawString(214, y, str(row.get("planting_date") or "-")[:10])
        age_label = "-" if str(row.get("age_source") or "none") == "none" else _fmt_num(row.get("age_years"), 1)
        c.drawRightString(287, y, age_label)
        c.drawRightString(323, y, height_val)
        c.drawRightString(381, y, _fmt_num(row.get("current_co2_kg"), 1))
        c.drawRightString(433, y, _fmt_num(row.get("annual_co2_kg"), 1))
        c.drawRightString(491, y, _fmt_num(row.get("lifetime_co2_kg"), 0))
        c.drawString(498, y, "in" if bool(row.get("co2_in_scope", True)) else "out")
        y -= 10

    # ------------------------------------------------------------------
    # PAGE: Operational metadata detail
    # ------------------------------------------------------------------
    def _draw_meta_table_page_header(title: str):
        c.setFont("Helvetica-Bold", 13)
        c.setFillColorRGB(0.12, 0.12, 0.12)
        c.drawString(28, height - 46, title)
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(28, height - 60, "Operational details for traceability (custodian/source/review/maintenance/photo).")
        c.setFont("Helvetica-Bold", 7.1)
        y_head = height - 78
        c.setFillColorRGB(0.16, 0.16, 0.16)
        c.drawString(28, y_head, "Tree")
        c.drawString(52, y_head, "Origin")
        c.drawString(118, y_head, "Attr")
        c.drawString(156, y_head, "Custodian")
        c.drawString(250, y_head, "Created By")
        c.drawRightString(364, y_head, "Maint")
        c.drawString(372, y_head, "Review")
        c.drawString(430, y_head, "Photo")
        c.drawString(462, y_head, "Source Link")
        c.drawString(522, y_head, "Date")
        c.line(28, y_head - 3, width - 28, y_head - 3)
        return y_head - 14

    c.showPage()
    y = _draw_meta_table_page_header("Existing Trees Operational Detail")
    c.setFont("Helvetica", 6.4)

    for row in rows:
        if y < 44:
            c.showPage()
            y = _draw_meta_table_page_header("Existing Trees Operational Detail (continued)")
            c.setFont("Helvetica", 6.4)

        review_state = str(row.get("last_review_state") or "-")[:8]
        photo_flag = "Y" if str(row.get("photo_url") or "").strip() else "-"
        source_link = str(row.get("source_project_id") or "")
        source_label = f"#{source_link}" if source_link and source_link not in {"0", ""} else "-"
        c.setFillColorRGB(0.14, 0.14, 0.14)
        c.drawString(28, y, f"#{row.get('id', '-')}")
        c.drawString(52, y, str(row.get("tree_origin") or "-")[:11])
        c.drawString(118, y, str(row.get("attribution_scope") or "-")[:6])
        c.drawString(156, y, str(row.get("custodian_name") or "-")[:20])
        c.drawString(250, y, str(row.get("created_by") or "-")[:18])
        c.drawRightString(364, y, str(int(row.get("maintenance_count") or 0)))
        c.drawString(372, y, review_state)
        c.drawString(430, y, photo_flag)
        c.drawString(462, y, source_label)
        c.drawString(522, y, str(row.get("created_at") or "-")[:10])
        y -= 10

    if include_photos:
        _render_photo_appendix_pages(
            c,
            width,
            height,
            project,
            [dict(row) for row in (photo_rows or []) if str(row.get("photo_url") or "").strip()],
            assignee_name=None,
        )

    c.save()
