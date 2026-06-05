from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import math
import os
import ssl
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from PIL import Image, ImageOps


def render_green_org_credentials_pdf(organization: dict, users: list[dict]) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    page_w, page_h = A4

    def draw_header(page_no: int):
        c.setFillColor(HexColor("#0f172a"))
        c.setFont("Helvetica-Bold", 16)
        c.drawString(36, page_h - 42, "LandCheck Work - Organization User Credentials")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.35, 0.35, 0.35)
        c.drawString(36, page_h - 58, f"Organization: {organization.get('name') or '-'}")
        c.drawString(36, page_h - 72, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        c.drawRightString(page_w - 36, page_h - 58, f"Org ID: {organization.get('id') or '-'}")
        c.drawRightString(page_w - 36, page_h - 72, f"Page {page_no}")
        c.setFont("Helvetica-Oblique", 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(36, page_h - 88, "Passwords are not retrievable; only login usernames and access details are printed.")

    def draw_table_header(y: float):
        c.setFillColor(HexColor("#e8f5eb"))
        c.rect(36, y - 16, page_w - 72, 18, stroke=0, fill=1)
        c.setFillColorRGB(0.12, 0.25, 0.16)
        c.setFont("Helvetica-Bold", 8)
        for x, label in [
            (40, "User ID"),
            (102, "Name"),
            (250, "Role"),
            (325, "Access"),
            (382, "Login Username"),
            (506, "Status"),
        ]:
            c.drawString(x, y - 10, label)

    def trunc(value: str, max_len: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "..."

    page_no = 1
    draw_header(page_no)
    y = page_h - 108
    draw_table_header(y)
    y -= 26
    row_h = 16
    rows_drawn = 0
    c.setFont("Helvetica", 7.5)
    for user in users:
        if y < 64:
            c.showPage()
            page_no += 1
            draw_header(page_no)
            y = page_h - 108
            draw_table_header(y)
            y -= 26
            c.setFont("Helvetica", 7.5)
        if rows_drawn % 2 == 0:
            c.setFillColorRGB(0.98, 0.99, 0.985)
            c.rect(36, y - 12, page_w - 72, row_h, stroke=0, fill=1)
        c.setFillColorRGB(0.14, 0.14, 0.14)
        access_parts = []
        if user.get("allow_green", True):
            access_parts.append("Green")
        if user.get("allow_work", False):
            access_parts.append("Work")
        access = " / ".join(access_parts) if access_parts else "-"
        status = "Inactive" if user.get("is_active") is False else "Active"
        c.drawString(40, y - 2, trunc(user.get("user_uid") or "-", 12))
        c.drawString(102, y - 2, trunc(user.get("full_name") or "-", 28))
        c.drawString(250, y - 2, trunc(user.get("role_name") or user.get("role") or "-", 14))
        c.drawString(325, y - 2, trunc(access, 12))
        c.drawString(382, y - 2, trunc(user.get("work_username") or "-", 24))
        c.drawString(506, y - 2, status)
        y -= row_h
        rows_drawn += 1

    if rows_drawn == 0:
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawString(36, y, "No users found for this organization.")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()


def render_green_sponsor_certificate_pdf(sponsorship: dict, carbon: dict | None = None) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    page_w, page_h = A4

    carbon_data = carbon or {}
    sponsor_name = str(sponsorship.get("sponsor_name") or "").strip() or "Sponsor"
    sponsor_org = str(sponsorship.get("sponsor_organization_name") or "").strip()
    project_name = str(sponsorship.get("project_name") or "").strip() or "LandCheck Green Project"
    tree_label = f"Tree #{int(sponsorship.get('project_tree_no') or 0)}" if sponsorship.get("project_tree_no") else f"Tree ID {int(sponsorship.get('tree_id') or 0)}"
    species_label = str(sponsorship.get("species") or "").strip() or "Tree"
    planting_date = str(sponsorship.get("planting_date") or "").strip() or "-"
    location_text = str(sponsorship.get("location_text") or "").strip() or "-"
    dedication_type = str(sponsorship.get("dedication_type") or "").strip()
    dedication_name = str(sponsorship.get("dedication_name") or "").strip()
    dedication_message = str(sponsorship.get("dedication_message") or "").strip()

    c.setFillColor(HexColor("#f4fbf5"))
    c.rect(0, 0, page_w, page_h, stroke=0, fill=1)
    c.setFillColor(HexColor("#0f4f2a"))
    c.rect(28, page_h - 96, page_w - 56, 54, stroke=0, fill=1)
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Helvetica-Bold", 22)
    c.drawString(44, page_h - 66, "LandCheck Green Sponsorship Certificate")
    c.setFont("Helvetica", 10)
    c.drawString(44, page_h - 82, "Verified tree sponsorship linked to approved field evidence and live maintenance records")

    _draw_rounded_box(c, 34, 470, page_w - 68, 220, 18, fill_color=HexColor("#ffffff"), stroke_color=HexColor("#d7ead9"))
    c.setFillColor(HexColor("#0f2d1a"))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(52, 664, "Awarded to")
    c.setFont("Helvetica-Bold", 26)
    c.drawString(52, 632, sponsor_name)
    if sponsor_org:
        c.setFont("Helvetica", 12)
        c.setFillColor(HexColor("#315e46"))
        c.drawString(52, 612, sponsor_org)

    c.setFillColor(HexColor("#163826"))
    c.setFont("Helvetica", 12)
    c.drawString(52, 582, f"Project: {project_name}")
    c.drawString(52, 564, f"Location: {location_text}")
    c.drawString(52, 546, f"Sponsored tree: {tree_label} | Species: {species_label}")
    c.drawString(52, 528, f"Planting date: {planting_date}")
    c.drawString(52, 510, f"Issued: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    if dedication_type or dedication_name or dedication_message:
        _draw_rounded_box(c, 52, 438, page_w - 104, 54, 12, fill_color=HexColor("#f6fbf7"), stroke_color=HexColor("#d7ead9"))
        c.setFillColor(HexColor("#0f2d1a"))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(64, 474, "Dedication")
        c.setFont("Helvetica", 10)
        dedication_line = " | ".join(part for part in [dedication_type.replace("_", " ").title() if dedication_type else "", dedication_name] if part)
        if dedication_line:
            c.drawString(64, 458, dedication_line[:90])
        if dedication_message:
            c.drawString(64, 444, dedication_message[:106])

    _draw_rounded_box(c, 34, 280, page_w - 68, 150, 18, fill_color=HexColor("#ffffff"), stroke_color=HexColor("#d7ead9"))
    c.setFillColor(HexColor("#0f2d1a"))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(52, 406, "Carbon impact snapshot")
    c.setFont("Helvetica-Bold", 22)
    c.drawString(52, 372, f"{float(carbon_data.get('current_co2_kg') or 0):,.2f} kg")
    c.setFont("Helvetica", 11)
    c.setFillColor(HexColor("#315e46"))
    c.drawString(52, 356, "Current stored CO2")
    c.setFillColor(HexColor("#0f2d1a"))
    c.setFont("Helvetica-Bold", 16)
    c.drawString(220, 372, f"{float(carbon_data.get('annual_co2_kg') or 0):,.2f} kg/yr")
    c.setFont("Helvetica", 11)
    c.setFillColor(HexColor("#315e46"))
    c.drawString(220, 356, "Annual sequestration")
    c.setFillColor(HexColor("#0f2d1a"))
    c.setFont("Helvetica-Bold", 16)
    c.drawString(394, 372, f"{float(carbon_data.get('lifetime_co2_kg') or 0):,.2f} kg")
    c.setFont("Helvetica", 11)
    c.setFillColor(HexColor("#315e46"))
    c.drawString(394, 356, "Projected 40-year stock")

    primary_photo = str(sponsorship.get("photo_url") or "").strip()
    photo_urls = sponsorship.get("photo_urls") or []
    if isinstance(photo_urls, list):
        for candidate in photo_urls:
            candidate_url = str(candidate or "").strip()
            if candidate_url:
                primary_photo = primary_photo or candidate_url
                break
    if primary_photo:
        reader = _load_photo_reader(primary_photo, {})
        if reader is not None:
            _draw_rounded_box(c, page_w - 188, 500, 134, 134, 14, fill_color=HexColor("#ffffff"), stroke_color=HexColor("#d7ead9"))
            try:
                c.drawImage(reader, page_w - 180, 508, 118, 118, preserveAspectRatio=True, anchor="c")
            except Exception:
                pass

    c.setStrokeColor(HexColor("#b7d6be"))
    c.line(52, 202, 220, 202)
    c.line(page_w - 220, 202, page_w - 52, 202)
    c.setFillColor(HexColor("#315e46"))
    c.setFont("Helvetica", 10)
    c.drawString(52, 188, "LandCheck Green verification")
    c.drawString(page_w - 220, 188, "Sponsor reference")
    c.setFillColor(HexColor("#0f2d1a"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(page_w - 220, 172, str(sponsorship.get("unit_uid") or sponsorship.get("tree_id") or "-"))

    c.save()
    buffer.seek(0)
    return buffer.getvalue()


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


def _draw_project_logo(c, project: dict, x: float, y: float, size: float) -> float:
    """Draw organization logo if available and return horizontal space used."""
    logo_url = str(project.get("organization_logo_url") or "").strip()
    if not logo_url or size <= 0:
        return 0
    reader = _load_photo_reader(logo_url, {})
    if reader is None:
        return 0
    try:
        c.saveState()
        c.setFillColorRGB(0.05, 0.05, 0.05)
        c.setStrokeColorRGB(0.88, 0.95, 0.9)
        c.setLineWidth(0.4)
        c.roundRect(x, y, size, size, 5, stroke=1, fill=1)
        pad = max(1.5, size * 0.06)
        c.drawImage(
            reader,
            x + pad,
            y + pad,
            max(size - (pad * 2), 2),
            max(size - (pad * 2), 2),
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
        c.restoreState()
        return size + 10
    except Exception:
        try:
            c.restoreState()
        except Exception:
            pass
        return 0


def _draw_project_brand_header_bar(
    c,
    width: float,
    height: float,
    project: dict,
    *,
    report_label: str,
    subtitle: str | None = None,
    bar_height: float = 80,
    bar_color: str = "#0b3d24",
):
    """Draw report header bar with organization/project branding and logo."""
    left = 34
    right = 34
    top = height
    bar_h = float(bar_height)
    c.setFillColor(HexColor(bar_color))
    c.rect(0, top - bar_h, width, bar_h, stroke=0, fill=1)

    org_name = str(project.get("organization_name") or "").strip()
    project_name = str(project.get("name") or "").strip() or "Project"
    heading = org_name or project_name
    line_parts: list[str] = []
    if project_name and project_name != heading:
        line_parts.append(project_name)
    if report_label:
        line_parts.append(report_label)
    line2 = " | ".join(line_parts) if line_parts else (report_label or project_name)

    logo_size = 28 if bar_h <= 72 else 32
    logo_y = top - bar_h + max((bar_h - logo_size) / 2, 4)
    logo_dx = _draw_project_logo(c, project, left, logo_y, logo_size)
    text_x = left + logo_dx

    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 16 if bar_h <= 72 else 18)
    c.drawString(text_x, top - 28, heading[:64])

    c.setFont("Helvetica", 10.2)
    c.setFillColorRGB(0.88, 0.96, 0.9)
    c.drawString(text_x, top - 43, line2[:96])
    c.setFont("Helvetica-Oblique", 8.8)
    powered_text = f"Powered by LandCheck{f' | {subtitle}' if subtitle else ''}"
    c.drawString(text_x, top - (58 if bar_h > 72 else 55), powered_text[:120])

    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.82, 0.95, 0.86)
    c.drawRightString(width - right, top - 28, datetime.utcnow().strftime("Generated %d %b %Y %H:%M UTC"))


def _draw_stat_card(c, x, y, w, h, label, value, sub=None, color=None):
    """Draw a metric card with large value and label."""
    bg = HexColor("#f8faf9") if not color else color
    _draw_rounded_box(c, x, y, w, h, 4, fill_color=bg)
    c.setFillColorRGB(0.15, 0.15, 0.15)
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(x + w / 2, y + h - 28, str(value))
    c.setFont("Helvetica", 8.8)
    c.setFillColorRGB(0.28, 0.28, 0.28)
    c.drawCentredString(x + w / 2, y + h - 40, label)
    if sub:
        c.setFont("Helvetica", 7.8)
        c.setFillColorRGB(0.4, 0.4, 0.4)
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


def _optimize_photo_bytes(data: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            max_edge = max(1, int(os.getenv("GREEN_REPORT_PHOTO_MAX_EDGE", "1400") or 1400))
            if img.mode not in {"RGB", "L"}:
                img = img.convert("RGB")
            img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(
                out,
                format="JPEG",
                quality=max(50, min(95, int(os.getenv("GREEN_REPORT_PHOTO_JPEG_QUALITY", "78") or 78))),
                optimize=True,
            )
            return out.getvalue()
    except Exception:
        return data


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
                with urlopen(req, timeout=6) as response:
                    data = response.read()
            except Exception:
                if parsed.scheme == "https":
                    context = ssl._create_unverified_context()
                    with urlopen(req, timeout=6, context=context) as response:
                        data = response.read()
        else:
            if raw.startswith("/"):
                base_url = (os.getenv("GREEN_REPORT_PHOTO_BASE_URL") or os.getenv("BACKEND_URL") or "").strip().rstrip("/")
                if base_url:
                    remote_url = f"{base_url}{raw}"
                    req = Request(remote_url, headers={"User-Agent": "LandCheck-Green-Report/1.0"})
                    try:
                        with urlopen(req, timeout=6) as response:
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
            data = _optimize_photo_bytes(data)
            reader = ImageReader(io.BytesIO(data))
    except Exception:
        reader = None

    image_cache[raw] = reader
    return reader


def _prefetch_photo_readers(photo_rows, image_cache):
    unique_urls = []
    seen = set()
    for row in photo_rows or []:
        raw = str((row or {}).get("photo_url") or "").strip()
        if raw.startswith("//"):
            raw = f"https:{raw}"
        if raw and raw not in seen:
            seen.add(raw)
            unique_urls.append(raw)
    if not unique_urls:
        return
    max_workers = max(2, min(8, int(os.getenv("GREEN_REPORT_PHOTO_FETCH_WORKERS", "6") or 6)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_load_photo_reader, raw, image_cache) for raw in unique_urls]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass


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
    max_photos = max(1, int(os.getenv("GREEN_REPORT_APPENDIX_MAX_PHOTOS", "120") or 120))
    source_rows = [dict(row) for row in (photo_rows or []) if str((row or {}).get("photo_url") or "").strip()]
    truncated_count = max(0, len(source_rows) - max_photos)
    photo_rows = source_rows[:max_photos]

    if not photo_rows:
        c.showPage()
        c.setFont("Helvetica-Bold", 15)
        c.drawString(40, height - 50, "Tree Photo Appendix")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawString(40, height - 70, "No tree photos available for the selected report scope.")
        return

    image_cache = {}
    _prefetch_photo_readers(photo_rows, image_cache)
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
        if truncated_count > 0:
            c.setFillColor(HexColor("#8a5a00"))
            c.drawString(
                40,
                height - 78,
                f"Photo appendix trimmed to first {len(photo_rows)} photos for reliable download. {truncated_count} more not embedded.",
            )
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
    _draw_project_brand_header_bar(
        c,
        width,
        height,
        project,
        report_label="Executive Donor Report",
        subtitle=None,
        bar_height=80,
    )

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
    c.drawRightString(width - 40, 24, f"Powered by LandCheck | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")


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
    c.drawString(40, 24, "Powered by LandCheck | Trend Analytics")
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
    c.drawString(40, 24, "Powered by LandCheck | Carbon Analytics")
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
    c.setFont("Helvetica-Bold", 16)
    org_title = str(project.get("organization_name") or "").strip()
    project_title = str(project.get("name") or "").strip() or "Project"
    section_heading = org_title or project_title
    c.drawString(40, height - 50, section_heading[:64])
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.38, 0.38, 0.38)
    c.drawString(40, height - 64, f"Project: {project_title} | Tree Records Report | Powered by LandCheck"[:110])
    c.setFillColorRGB(0, 0, 0)

    c.setFont("Helvetica", 11)
    c.drawString(40, height - 88, f"Project: {project.get('name', '')}")
    c.drawString(40, height - 106, f"Location: {project.get('location_text', '')}")
    c.drawString(40, height - 124, f"Sponsor: {project.get('sponsor', '')}")
    c.drawString(40, height - 142, f"Created: {project.get('created_at', '')}")

    stats = project.get("stats", {})
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 178, "Summary")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 196, f"Total: {stats.get('total', 0)}")
    c.drawString(120, height - 196, f"Alive: {stats.get('alive', 0)}")
    c.drawString(200, height - 196, f"Dead: {stats.get('dead', 0)}")
    c.drawString(280, height - 196, f"Needs Attention: {stats.get('needs_attention', 0)}")
    c.drawString(420, height - 196, f"Survival: {stats.get('survival_rate', 0)}%")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 228, "Tree Records + Maintenance (latest 200)")

    def draw_tree_header(y_pos: float):
        c.setFont("Helvetica-Bold", 8)
        c.drawString(40, y_pos, "Tree ID")
        c.drawString(84, y_pos, "Species")
        c.drawString(160, y_pos, "Status")
        c.drawString(222, y_pos, "Planting Date")
        c.drawString(300, y_pos, "Maint #")
        c.drawString(344, y_pos, "Maint Type(s)")
        c.drawString(474, y_pos, "Last Maint Date")

    y = height - 248
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

    _draw_project_brand_header_bar(
        c,
        width,
        height,
        project,
        report_label="Work Report",
        subtitle="Assignments, progress, and maintenance operations summary",
        bar_height=80,
    )

    c.setFont("Helvetica", 11)
    c.drawString(40, height - 92, f"Project: {project.get('name', '')}")
    c.drawString(40, height - 110, f"Location: {project.get('location_text', '')}")
    c.drawString(40, height - 128, f"Sponsor: {project.get('sponsor', '')}")

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
    c.drawString(40, height - 162, "Assignee Summary + Maintenance")

    y = height - 182
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
        _draw_project_brand_header_bar(
            c,
            width,
            height,
            project,
            report_label="Custodian Report",
            subtitle=subtitle,
            bar_height=70,
        )

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
    _draw_project_brand_header_bar(
        c,
        width,
        height,
        project,
        report_label="Existing Trees Report",
        subtitle="Detailed existing-tree inventory with per-tree CO2 estimates and optional photo appendix",
        bar_height=78,
    )

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
    project_meta.append(f"Rows: {int(summary.get('total_existing_rows', len(rows)) or 0)}")
    if float(summary.get("total_existing_area_sqm", 0) or 0) > 0:
        project_meta.append(f"Area: {_fmt_num(summary.get('total_existing_area_ha', 0), 4)} ha")
    c.drawString(34, height - 114, " | ".join(project_meta)[:130])

    card_y = height - 186
    card_w = (width - 34 - 34 - 10 - 10) / 3
    card_h = 58
    _draw_stat_card(
        c, 34, card_y, card_w, card_h,
        "Existing Trees",
        int(summary.get("total_existing_trees", 0) or 0),
        sub=(
            f"Rows: {int(summary.get('total_existing_rows', len(rows)) or 0)}"
            + (
                f" | Area: {_fmt_num(summary.get('total_existing_area_ha', 0), 3)} ha"
                if float(summary.get("total_existing_area_sqm", 0) or 0) > 0
                else ""
            )
        ),
        color=HexColor("#eef7f0"),
    )
    _draw_stat_card(
        c, 34 + card_w + 10, card_y, card_w, card_h,
        "Current CO2 (t)",
        _fmt_num(summary.get("current_co2_tonnes", 0), 3),
        sub="current stock",
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
    ]
    for note in notes:
        for line in _wrap_text(note, "Helvetica", 7.4, width - 68):
            c.drawString(34, line_y, line)
            line_y -= 9

    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.drawString(34, 24, "Powered by LandCheck | Existing Trees Detailed Export")
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
        c.drawString(28, height - 60, "CO2 columns are zero where the row is excluded from carbon scope. Rows may represent multiple trees.")
        c.setFont("Helvetica-Bold", 7.2)
        y_head = height - 78
        c.setFillColorRGB(0.16, 0.16, 0.16)
        c.drawString(28, y_head, "Tree")
        c.drawString(52, y_head, "Species")
        c.drawString(156, y_head, "Status")
        c.drawString(214, y_head, "Date")
        c.drawRightString(287, y_head, "Age y/m")
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
        count_label = ""
        try:
            row_count = max(int(row.get("inventory_tree_count") or 1), 1)
            if row_count > 1:
                count_label = f" x{row_count}"
        except Exception:
            count_label = ""
        c.drawString(28, y, f"#{row.get('id', '-')}{count_label}")
        c.drawString(52, y, str(row.get("species") or "-")[:24])
        c.drawString(156, y, str(row.get("status") or "-")[:13])
        c.drawString(214, y, str(row.get("planting_date") or "-")[:10])
        age_source = str(row.get("age_source") or "none")
        age_months_raw = row.get("tree_age_months")
        age_months_label = ""
        try:
            if age_months_raw is not None:
                age_months_value = float(age_months_raw)
                if age_months_value >= 0:
                    age_months_label = str(int(round(age_months_value)))
        except Exception:
            age_months_label = ""
        if age_source == "none":
            age_label = "-"
        else:
            age_years_label = _fmt_num(row.get("age_years"), 1)
            age_label = f"{age_years_label}/{age_months_label}" if age_months_label else age_years_label
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
        c.drawString(28, height - 60, "Operational details for traceability (origin, custodian, batch count/area, review, maintenance, photos).")
        c.setFont("Helvetica-Bold", 7.1)
        y_head = height - 78
        c.setFillColorRGB(0.16, 0.16, 0.16)
        c.drawString(28, y_head, "Tree")
        c.drawString(52, y_head, "Origin")
        c.drawString(118, y_head, "Attr")
        c.drawString(156, y_head, "Custodian")
        c.drawString(250, y_head, "Created By")
        c.drawString(344, y_head, "Cnt/Area")
        c.drawRightString(414, y_head, "Maint")
        c.drawString(420, y_head, "Review")
        c.drawString(462, y_head, "Photo")
        c.drawString(495, y_head, "Date")
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
        try:
            row_count = max(int(row.get("inventory_tree_count") or 1), 1)
        except Exception:
            row_count = 1
        area_sqm = None
        try:
            area_sqm = float(row.get("existing_area_sqm")) if row.get("existing_area_sqm") is not None else None
        except Exception:
            area_sqm = None
        count_area_label = f"{row_count}"
        if area_sqm is not None and area_sqm > 0:
            count_area_label += f" / {int(round(area_sqm))}m2"
        c.setFillColorRGB(0.14, 0.14, 0.14)
        c.drawString(28, y, f"#{row.get('id', '-')}")
        c.drawString(52, y, str(row.get("tree_origin") or "-")[:11])
        c.drawString(118, y, str(row.get("attribution_scope") or "-")[:6])
        c.drawString(156, y, str(row.get("custodian_name") or "-")[:20])
        c.drawString(250, y, str(row.get("created_by") or "-")[:18])
        c.drawString(344, y, count_area_label[:11])
        c.drawRightString(414, y, str(int(row.get("maintenance_count") or 0)))
        c.drawString(420, y, review_state)
        c.drawString(462, y, photo_flag)
        c.drawString(495, y, str(row.get("created_at") or "-")[:10])
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


def _wrap_pdf_text_lines(c, text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
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


def _safe_float_value(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int_value(value, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _format_agric_label(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    return text.replace("_", " ").replace("-", " ").title()


def _format_yes_no_label(value) -> str:
    return "Yes" if bool(value) else "No"


def _first_available_plot_photo_url(row: dict) -> str:
    primary = str(row.get("primary_photo_url") or "").strip()
    if primary:
        return primary
    photo_urls = row.get("photo_urls") if isinstance(row.get("photo_urls"), list) else []
    for item in photo_urls:
        candidate = str(item or "").strip()
        if candidate:
            return candidate
    return str(row.get("photo_url") or "").strip()


def _extract_agric_polygon_rings(geometry) -> list[list[tuple[float, float]]]:
    if not isinstance(geometry, dict):
        return []
    source = geometry.get("geometry") if str(geometry.get("type") or "").strip() == "Feature" else geometry
    if not isinstance(source, dict):
        return []
    geom_type = str(source.get("type") or "").strip()
    coords = source.get("coordinates") or []
    rings: list[list[tuple[float, float]]] = []

    def _append_ring(raw_ring):
        if not isinstance(raw_ring, list):
            return
        ring: list[tuple[float, float]] = []
        for point in raw_ring:
            if not isinstance(point, list) or len(point) < 2:
                continue
            try:
                ring.append((float(point[0]), float(point[1])))
            except Exception:
                continue
        if len(ring) >= 3:
            rings.append(ring)

    if geom_type == "Polygon":
        for raw_ring in coords if isinstance(coords, list) else []:
            _append_ring(raw_ring)
    elif geom_type == "MultiPolygon":
        for polygon in coords if isinstance(coords, list) else []:
            if not isinstance(polygon, list):
                continue
            for raw_ring in polygon:
                _append_ring(raw_ring)
    return rings


def _collect_agric_geometry_points(row: dict) -> list[tuple[float, float]]:
    rings = _extract_agric_polygon_rings(row.get("existing_area_geojson"))
    if rings:
        return [point for ring in rings for point in ring]
    try:
        lng = float(row.get("lng"))
        lat = float(row.get("lat"))
        return [(lng, lat)]
    except Exception:
        return []


def _draw_image_bytes_fit(c, image_bytes: bytes | None, x: float, y: float, w: float, h: float):
    actual = {"x": x, "y": y, "w": w, "h": h, "src_w": w, "src_h": h}
    if not image_bytes:
        c.setFillColor(HexColor("#f6faf7"))
        c.rect(x, y, w, h, stroke=0, fill=1)
        c.setStrokeColor(HexColor("#d8e6dd"))
        c.rect(x, y, w, h, stroke=1, fill=0)
        return actual
    try:
        reader = ImageReader(io.BytesIO(image_bytes))
        src_w, src_h = reader.getSize()
        if not src_w or not src_h:
            raise ValueError("Invalid image size")
        scale = min(w / src_w, h / src_h)
        draw_w = src_w * scale
        draw_h = src_h * scale
        draw_x = x + (w - draw_w) / 2
        draw_y = y + (h - draw_h) / 2
        c.drawImage(reader, draw_x, draw_y, draw_w, draw_h, preserveAspectRatio=True, anchor="c", mask="auto")
        c.setStrokeColor(HexColor("#d8e6dd"))
        c.rect(draw_x, draw_y, draw_w, draw_h, stroke=1, fill=0)
        actual = {"x": draw_x, "y": draw_y, "w": draw_w, "h": draw_h, "src_w": src_w, "src_h": src_h}
    except Exception:
        c.setFillColor(HexColor("#f6faf7"))
        c.rect(x, y, w, h, stroke=0, fill=1)
        c.setStrokeColor(HexColor("#d8e6dd"))
        c.rect(x, y, w, h, stroke=1, fill=0)
    return actual


def _mercator_pixel(lng: float, lat: float, world_size: float) -> tuple[float, float]:
    x_val = (lng + 180.0) / 360.0 * world_size
    siny = math.sin(math.radians(lat))
    siny = min(max(siny, -0.9999), 0.9999)
    y_val = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * world_size
    return x_val, y_val


def _draw_agric_map_panel(
    c,
    *,
    x: float,
    y: float,
    w: float,
    h: float,
    map_png: bytes | None,
    map_view: dict | None,
    plot_rows: list[dict],
    highlight_plot_id: int | None = None,
    entity_singular_label: str = "plot",
    entity_plural_label: str = "plots",
):
    _draw_rounded_box(c, x, y, w, h, 6, fill_color=HexColor("#f8fbf8"), stroke_color=HexColor("#d9e7dc"))
    pad = 8
    box = _draw_image_bytes_fit(c, map_png, x + pad, y + pad, w - (pad * 2), h - (pad * 2))
    if not plot_rows:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.42, 0.42, 0.42)
        c.drawCentredString(x + (w / 2), y + (h / 2), f"No mapped {entity_plural_label}")
        return

    if not map_view:
        map_view = {}
    center_lng = _safe_float_value(map_view.get("lng"))
    center_lat = _safe_float_value(map_view.get("lat"))
    zoom = max(_safe_float_value(map_view.get("zoom"), 13.0), 1.0)
    world_size = 512 * (2 ** zoom)
    center_px, center_py = _mercator_pixel(center_lng, center_lat, world_size)
    scale_x = box["w"] / max(float(box["src_w"] or 1), 1.0)
    scale_y = box["h"] / max(float(box["src_h"] or 1), 1.0)

    def _to_canvas(point_lng: float, point_lat: float) -> tuple[float, float]:
        px, py = _mercator_pixel(point_lng, point_lat, world_size)
        img_px = (float(box["src_w"]) / 2.0) + (px - center_px)
        img_py = (float(box["src_h"]) / 2.0) + (py - center_py)
        return (
            float(box["x"]) + (img_px * scale_x),
            float(box["y"]) + float(box["h"]) - (img_py * scale_y),
        )

    light_red = HexColor("#cf6b5f")
    strong_red = HexColor("#b42318")
    dot_fill = HexColor("#c7352f")
    for row in plot_rows:
        row_id = _safe_int_value(row.get("id"))
        rings = _extract_agric_polygon_rings(row.get("existing_area_geojson"))
        is_highlight = highlight_plot_id is not None and row_id == int(highlight_plot_id)
        stroke = strong_red if is_highlight else light_red
        width_val = 2.2 if is_highlight else 1.0
        c.setStrokeColor(stroke)
        c.setFillColor(stroke)
        c.setLineWidth(width_val)
        if rings:
            for ring in rings:
                if len(ring) < 2:
                    continue
                path = c.beginPath()
                first_x, first_y = _to_canvas(ring[0][0], ring[0][1])
                path.moveTo(first_x, first_y)
                for point_lng, point_lat in ring[1:]:
                    point_x, point_y = _to_canvas(point_lng, point_lat)
                    path.lineTo(point_x, point_y)
                path.close()
                c.drawPath(path, stroke=1, fill=0)
        else:
            points = _collect_agric_geometry_points(row)
            if not points:
                continue
            anchor_x, anchor_y = _to_canvas(points[0][0], points[0][1])
            radius = 3.6 if is_highlight else 2.1
            c.circle(anchor_x, anchor_y, radius, stroke=1, fill=1)
        if is_highlight:
            record_profile = row.get("record_profile_data") or {}
            label = str(
                record_profile.get("plot_code")
                or record_profile.get("plot_name")
                or record_profile.get("asset_code")
                or record_profile.get("asset_name")
                or row.get("custodian_name")
                or f"{entity_singular_label.title()} #{row_id}"
            )[:22]
            label_x, label_y = _to_canvas(*(rings[0][0] if rings and rings[0] else _collect_agric_geometry_points(row)[0]))
            label_w = min(max(c.stringWidth(label, "Helvetica-Bold", 7.2) + 12, 56), w - 26)
            label_h = 14
            label_x = min(max(label_x + 6, x + 10), x + w - label_w - 10)
            label_y = min(max(label_y + 4, y + 12), y + h - label_h - 12)
            c.setFillColorRGB(1, 1, 1)
            c.roundRect(label_x, label_y, label_w, label_h, 4, stroke=0, fill=1)
            c.setFillColor(strong_red)
            c.setFont("Helvetica-Bold", 7.2)
            c.drawString(label_x + 6, label_y + 4, label)


def _draw_plot_geometry_zoom_panel(c, *, x: float, y: float, w: float, h: float, row: dict, entity_singular_label: str = "plot"):
    _draw_rounded_box(c, x, y, w, h, 6, fill_color=HexColor("#faf7f5"), stroke_color=HexColor("#e6d9d4"))
    pad = 12
    plot_x = x + pad
    plot_y = y + 22
    plot_w = w - (pad * 2)
    plot_h = h - 42
    c.setFillColor(HexColor("#fffdfc"))
    c.rect(plot_x, plot_y, plot_w, plot_h, stroke=0, fill=1)
    c.setStrokeColor(HexColor("#e8ddd9"))
    c.rect(plot_x, plot_y, plot_w, plot_h, stroke=1, fill=0)

    rings = _extract_agric_polygon_rings(row.get("existing_area_geojson"))
    points = [point for ring in rings for point in ring] if rings else _collect_agric_geometry_points(row)
    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.14, 0.14, 0.14)
    c.drawString(x + 12, y + h - 16, f"Zoomed {entity_singular_label.title()} Boundary")
    if not points:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.42, 0.42, 0.42)
        c.drawCentredString(x + (w / 2), y + (h / 2), "Boundary geometry unavailable")
        return

    lngs = [point[0] for point in points]
    lats = [point[1] for point in points]
    min_lng, max_lng = min(lngs), max(lngs)
    min_lat, max_lat = min(lats), max(lats)
    span_lng = max(max_lng - min_lng, 0.0002)
    span_lat = max(max_lat - min_lat, 0.0002)
    pad_lng = span_lng * 0.18
    pad_lat = span_lat * 0.18
    min_lng -= pad_lng
    max_lng += pad_lng
    min_lat -= pad_lat
    max_lat += pad_lat
    draw_span_lng = max(max_lng - min_lng, 0.0002)
    draw_span_lat = max(max_lat - min_lat, 0.0002)

    def _to_canvas(point_lng: float, point_lat: float) -> tuple[float, float]:
        px = plot_x + ((point_lng - min_lng) / draw_span_lng) * plot_w
        py = plot_y + ((point_lat - min_lat) / draw_span_lat) * plot_h
        return px, py

    c.setStrokeColor(HexColor("#b42318"))
    c.setLineWidth(2.0)
    if rings:
        for ring in rings:
            if len(ring) < 2:
                continue
            path = c.beginPath()
            first_x, first_y = _to_canvas(ring[0][0], ring[0][1])
            path.moveTo(first_x, first_y)
            for point_lng, point_lat in ring[1:]:
                point_x, point_y = _to_canvas(point_lng, point_lat)
                path.lineTo(point_x, point_y)
            path.close()
            c.drawPath(path, stroke=1, fill=0)
    else:
        anchor_x, anchor_y = _to_canvas(points[0][0], points[0][1])
        c.setFillColor(HexColor("#b42318"))
        c.circle(anchor_x, anchor_y, 4, stroke=1, fill=1)
    try:
        anchor_lng = _safe_float_value(row.get("lng"))
        anchor_lat = _safe_float_value(row.get("lat"))
        anchor_x, anchor_y = _to_canvas(anchor_lng, anchor_lat)
        c.setFillColor(HexColor("#b42318"))
        c.circle(anchor_x, anchor_y, 3.1, stroke=1, fill=1)
    except Exception:
        pass
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.46, 0.46, 0.46)
    c.drawString(x + 12, y + 8, f"Red boundary = mapped {entity_singular_label} | point = capture anchor")


def _draw_plot_photo_panel(c, *, x: float, y: float, w: float, h: float, row: dict, image_cache: dict, entity_singular_label: str = "plot"):
    _draw_rounded_box(c, x, y, w, h, 6, fill_color=HexColor("#f8faf9"), stroke_color=HexColor("#d7e5db"))
    photo_url = _first_available_plot_photo_url(row)
    record_profile = row.get("record_profile_data") or {}
    caption = str(
        record_profile.get("plot_name")
        or record_profile.get("asset_name")
        or record_profile.get("plot_code")
        or record_profile.get("asset_code")
        or row.get("custodian_name")
        or f"{entity_singular_label.title()} #{row.get('id', '-')}"
    )
    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.12, 0.12, 0.12)
    c.drawString(x + 10, y + h - 16, f"{entity_singular_label.title()} Photo Evidence")
    c.setFont("Helvetica", 7.5)
    c.setFillColorRGB(0.42, 0.42, 0.42)
    c.drawString(x + 10, y + h - 27, caption[:72])
    img_x = x + 10
    img_y = y + 10
    img_w = w - 20
    img_h = h - 44
    reader = _load_photo_reader(photo_url, image_cache) if photo_url else None
    if reader is not None:
        try:
            src_w, src_h = reader.getSize()
            scale = min(img_w / max(src_w, 1), img_h / max(src_h, 1))
            draw_w = max(src_w * scale, 2)
            draw_h = max(src_h * scale, 2)
            draw_x = img_x + (img_w - draw_w) / 2
            draw_y = img_y + (img_h - draw_h) / 2
            c.drawImage(reader, draw_x, draw_y, draw_w, draw_h, preserveAspectRatio=True, anchor="c", mask="auto")
        except Exception:
            reader = None
    if reader is None:
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.rect(img_x, img_y, img_w, img_h, stroke=1, fill=0)
        c.setFont("Helvetica-Oblique", 8)
        c.setFillColorRGB(0.46, 0.46, 0.46)
        c.drawCentredString(img_x + (img_w / 2), img_y + (img_h / 2), f"No photo embedded for this {entity_singular_label}")


def render_green_agric_programme_pdf(
    output_path: str,
    project: dict,
    summary: dict,
    farmer_rows: list[dict],
    plot_rows: list[dict],
    overview_map_png: bytes | None = None,
    overview_map_view: dict | None = None,
    include_photos: bool = False,
):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4
    agric_config = project.get("agric_config") if isinstance(project.get("agric_config"), dict) else {}
    body_font = "Helvetica"
    body_font_size = 8.8
    body_line_gap = 10.4
    table_font_size = 7.9
    table_header_size = 8.1
    note_font_size = 8.1
    total_plots = len(plot_rows)
    farmer_page_size = 24
    plot_schedule_page_size = 24
    farmer_page_count = int(math.ceil(len(farmer_rows) / float(farmer_page_size))) if farmer_rows else 0
    plot_schedule_page_count = int(math.ceil(total_plots / float(plot_schedule_page_size))) if plot_rows else 0
    total_pages = 2 + farmer_page_count + plot_schedule_page_count + total_plots
    current_page = 1

    registered_farmers = _safe_int_value(summary.get("registered_farmers"))
    verified_farmers = _safe_int_value(summary.get("verified_farmers"))
    mapped_plots = _safe_int_value(summary.get("mapped_plots"))
    mapped_area_ha = _safe_float_value(summary.get("mapped_area_ha"))
    estimated_yield_kg = _safe_float_value(summary.get("estimated_yield_kg"))
    support_visit_done = _safe_int_value(summary.get("support_visit_done"))
    support_target_total = _safe_int_value(summary.get("support_target_total"))
    support_visit_live = _safe_int_value(summary.get("support_visit_live"))
    field_capture_done = _safe_int_value(summary.get("field_capture_done"))
    field_capture_assigned = _safe_int_value(summary.get("field_capture_assigned"))
    field_capture_live = _safe_int_value(summary.get("field_capture_live"))
    allocated_units = _safe_float_value(summary.get("allocated_units"))
    polygon_plots = _safe_int_value(summary.get("polygon_plots"))
    photo_evidence_plots = _safe_int_value(summary.get("photo_evidence_plots"))
    farmer_linked_plots = _safe_int_value(summary.get("farmer_linked_plots"))
    crop_profile_plots = _safe_int_value(summary.get("crop_profile_plots"))
    season_profile_plots = _safe_int_value(summary.get("season_profile_plots"))
    reviewed_plots = _safe_int_value(summary.get("reviewed_plots"))
    identity_ready_farmers = _safe_int_value(summary.get("identity_ready_farmers"))
    finance_access_farmers = _safe_int_value(summary.get("finance_access_farmers"))
    insurance_access_farmers = _safe_int_value(summary.get("insurance_access_farmers"))
    grouped_farmers = _safe_int_value(summary.get("grouped_farmers"))

    def format_area(value: float) -> str:
        return f"{value:.2f} ha" if value > 0 else "Not captured"

    def format_quantity(value: float, suffix: str) -> str:
        return f"{value:,.0f} {suffix}" if value > 0 else "Not declared"

    def format_count_pair(done: int, target: int) -> str:
        return f"{done}/{target}" if target > 0 else "0/0"

    def draw_footer() -> None:
        c.setStrokeColor(HexColor("#d6dfd9"))
        c.setLineWidth(0.5)
        c.line(34, 22, width - 34, 22)
        c.setFont("Helvetica", 7.8)
        c.setFillColorRGB(0.34, 0.34, 0.34)
        c.drawString(34, 10, "LandCheck Agric programme report")
        c.drawRightString(width - 34, 10, f"Page {current_page} of {max(total_pages, 1)}")

    def finish_page(*, final: bool = False) -> None:
        nonlocal current_page
        draw_footer()
        if not final:
            c.showPage()
            current_page += 1

    def draw_section_header(
        title: str,
        subtitle: str,
        *,
        report_label: str,
        bar_height: float = 74,
        bar_color: str = "#113b24",
    ) -> None:
        _draw_project_brand_header_bar(
            c,
            width,
            height,
            project,
            report_label=report_label,
            subtitle=subtitle,
            bar_height=bar_height,
            bar_color=bar_color,
        )
        c.setFillColorRGB(0.08, 0.14, 0.11)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(34, height - bar_height - 20, title[:76])
        c.setFont("Helvetica", 9.3)
        c.setFillColorRGB(0.26, 0.26, 0.26)
        c.drawString(34, height - bar_height - 33, subtitle[:118])

    def draw_list_box(
        title: str,
        x: float,
        y: float,
        w: float,
        h: float,
        lines: list[str],
        *,
        fill_color: str = "#f8faf9",
        stroke_color: str = "#d7e5db",
        bullet: bool = False,
    ) -> None:
        _draw_rounded_box(c, x, y, w, h, 7, fill_color=HexColor(fill_color), stroke_color=HexColor(stroke_color))
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.12, 0.12, 0.12)
        c.drawString(x + 10, y + h - 16, title[:54])
        current_y = y + h - 30
        c.setFont(body_font, body_font_size)
        c.setFillColorRGB(0.16, 0.16, 0.16)
        for line in lines:
            prefix = "- " if bullet else ""
            wrapped = _wrap_pdf_text_lines(c, f"{prefix}{line}", body_font, body_font_size, w - 20)
            for idx, part in enumerate(wrapped[:4]):
                if current_y < y + 10:
                    return
                if bullet and idx > 0 and part.startswith("- "):
                    part = f"  {part[2:]}"
                c.drawString(x + 10, current_y, part)
                current_y -= body_line_gap

    def draw_table_shell(title: str, subtitle: str) -> tuple[float, float, float, float]:
        draw_section_header(title, subtitle, report_label="Agric Programme Report")
        table_x = 34
        table_y = 56
        table_w = width - 68
        table_h = height - 172
        _draw_rounded_box(c, table_x, table_y, table_w, table_h, 8, fill_color=HexColor("#fbfcfb"), stroke_color=HexColor("#d7e5db"))
        return table_x, table_y, table_w, table_h

    def format_plot_label(row: dict) -> str:
        record_profile = row.get("record_profile_data") or {}
        return str(record_profile.get("plot_name") or record_profile.get("plot_code") or f"Plot #{row.get('id', '-')}")

    def executive_summary_lines() -> list[str]:
        lines = [
            f"{registered_farmers} registered farmer record(s) are linked to {mapped_plots} mapped plot(s) covering {format_area(mapped_area_ha)}.",
        ]
        top_crops = summary.get("top_commodities") or []
        if top_crops:
            top_crop = top_crops[0] or {}
            lines.append(
                f"Current mapped commodity coverage is led by {str(top_crop.get('label') or 'Unknown')} ({_safe_float_value(top_crop.get('area_ha')):.2f} ha)."
            )
        if support_target_total > 0:
            lines.append(
                f"Support visits completed: {support_visit_done} of {support_target_total}, with {support_visit_live} still outstanding."
            )
        if allocated_units > 0:
            lines.append(f"Recorded support allocations total {allocated_units:,.0f} units/packages for the current project snapshot.")
        if field_capture_assigned > 0:
            lines.append(
                f"Field capture tasks closed: {field_capture_done} of {field_capture_assigned}, with {field_capture_live} still open."
            )
        elif mapped_plots > 0:
            lines.append("Mapped plots are already on record; no separate field-capture task backlog is currently open.")
        return lines[:5]

    def coverage_snapshot_lines() -> list[str]:
        return [
            f"Polygon boundary coverage: {polygon_plots}/{max(mapped_plots, 1)} plot(s) ({_safe_float_value(summary.get('geo_readiness_pct')):.1f}%).",
            f"Photo evidence captured: {photo_evidence_plots}/{max(mapped_plots, 1)} plot(s) ({_safe_float_value(summary.get('photo_readiness_pct')):.1f}%).",
            f"Farmer ID / code ready: {identity_ready_farmers}/{max(registered_farmers, 1)} farmer(s) ({_safe_float_value(summary.get('identity_readiness_pct')):.1f}%).",
            f"Crop and season profiling: {crop_profile_plots}/{max(mapped_plots, 1)} crop-ready, {season_profile_plots}/{max(mapped_plots, 1)} season-ready.",
            f"Submitted or reviewed plot records: {reviewed_plots}/{max(mapped_plots, 1)} ({_safe_float_value(summary.get('review_readiness_pct')):.1f}%).",
        ]

    def follow_up_lines() -> list[str]:
        lines: list[str] = []
        missing_identity = max(registered_farmers - identity_ready_farmers, 0)
        missing_photos = max(mapped_plots - photo_evidence_plots, 0)
        missing_reviews = max(mapped_plots - reviewed_plots, 0)
        missing_geo = max(mapped_plots - polygon_plots, 0)
        if missing_identity > 0:
            lines.append(f"Complete farmer code or national ID for {missing_identity} farmer record(s).")
        if missing_geo > 0:
            lines.append(f"Close boundary mapping for {missing_geo} plot record(s) without polygon coverage.")
        if missing_photos > 0:
            lines.append(f"Capture plot photos for {missing_photos} mapped plot(s) to improve field evidence coverage.")
        if missing_reviews > 0:
            lines.append(f"Submit or review {missing_reviews} plot record(s) still outside the review-ready set.")
        if support_target_total > support_visit_done:
            lines.append(f"Complete {support_target_total - support_visit_done} planned support visit(s) still outstanding.")
        if field_capture_assigned > field_capture_done:
            lines.append(f"Close {field_capture_assigned - field_capture_done} remaining field-capture task(s).")
        if not lines:
            lines.append("No major registry or evidence gaps are flagged in the current project snapshot.")
        return lines[:5]

    image_cache: dict = {}
    if include_photos:
        photo_seed_rows = []
        for row in plot_rows:
            photo_url = _first_available_plot_photo_url(row)
            if photo_url:
                item = dict(row)
                item["photo_url"] = photo_url
                photo_seed_rows.append(item)
        _prefetch_photo_readers(photo_seed_rows, image_cache)

    draw_section_header(
        "Programme Summary",
        "Organisation-ready farmer registry, mapped plot evidence, and support-delivery snapshot.",
        report_label="Agric Programme Report",
        bar_height=78,
    )

    c.setFont("Helvetica", 9.4)
    c.setFillColorRGB(0.22, 0.22, 0.22)
    meta_parts = []
    if project.get("location_text"):
        meta_parts.append(f"Location: {project.get('location_text')}")
    if agric_config.get("program_type"):
        meta_parts.append(f"Programme: {_format_agric_label(agric_config.get('program_type'))}")
    if agric_config.get("focus_commodities"):
        meta_parts.append(f"Commodities: {str(agric_config.get('focus_commodities'))[:38]}")
    if agric_config.get("season_label"):
        meta_parts.append(f"Season: {agric_config.get('season_label')}")
    if project.get("sponsor"):
        meta_parts.append(f"Sponsor: {project.get('sponsor')}")
    c.drawString(34, height - 125, " | ".join(meta_parts)[:128])

    card_w = (width - 68 - 16) / 3
    card_h = 56
    top_y = height - 205
    cards = [
        ("Registered Farmers", registered_farmers, f"Verified {verified_farmers}"),
        ("Mapped Plots", mapped_plots, f"Farmer linked {farmer_linked_plots}"),
        ("Mapped Area", f"{mapped_area_ha:.2f} ha" if mapped_area_ha > 0 else "0.00 ha", "Portfolio footprint"),
        ("Support Visits", format_count_pair(support_visit_done, support_target_total), f"Open {support_visit_live}"),
        ("Photo Evidence", format_count_pair(photo_evidence_plots, mapped_plots), "Plots with photo"),
        ("Review Coverage", format_count_pair(reviewed_plots, mapped_plots), "Submitted or approved"),
    ]
    for index, (label, value, sub) in enumerate(cards):
        row_index = index // 3
        col_index = index % 3
        _draw_stat_card(
            c,
            34 + (col_index * (card_w + 8)),
            top_y - (row_index * (card_h + 10)),
            card_w,
            card_h,
            label,
            value,
            sub=sub,
            color=HexColor("#f4faf6" if index % 2 == 0 else "#fbfcfb"),
        )

    top_crops = summary.get("top_commodities") or []
    if top_crops:
        bar_rows = []
        colors = ["#166534", "#2f855a", "#3f8f5d", "#68a86f", "#0f766e", "#2563eb"]
        for idx, item in enumerate(top_crops[:6]):
            bar_rows.append(
                (
                    str(item.get("label") or "Unknown")[:18],
                    _safe_float_value(item.get("area_ha")),
                    colors[idx % len(colors)],
                )
            )
        _draw_bar_chart(c, 34, 214, width - 68, 110, bar_rows, title="Mapped Commodity Footprint (ha)")

    draw_list_box(
        "Executive Summary",
        34,
        52,
        (width - 78) / 2,
        148,
        executive_summary_lines(),
        bullet=True,
    )
    draw_list_box(
        "Data Coverage Snapshot",
        44 + ((width - 78) / 2),
        52,
        (width - 78) / 2,
        148,
        coverage_snapshot_lines(),
        bullet=True,
        fill_color="#f7faf8",
    )
    finish_page()

    draw_section_header(
        "Portfolio Map & Operational Readiness",
        "Project map, mapped boundary coverage, and immediate follow-up priorities.",
        report_label="Agric Programme Report",
    )
    _draw_agric_map_panel(
        c,
        x=34,
        y=230,
        w=width - 68,
        h=340,
        map_png=overview_map_png,
        map_view=overview_map_view,
        plot_rows=plot_rows,
        highlight_plot_id=None,
    )
    draw_list_box(
        "Portfolio Readiness",
        34,
        62,
        (width - 78) / 2,
        148,
        [
            f"Farmer-linked plots: {farmer_linked_plots}/{max(mapped_plots, 1)}.",
            f"Crop profiling complete: {crop_profile_plots}/{max(mapped_plots, 1)} plot(s).",
            f"Season or reference date captured: {season_profile_plots}/{max(mapped_plots, 1)} plot(s).",
            f"Farmer grouping captured: {grouped_farmers}/{max(registered_farmers, 1)} farmer(s).",
            f"Finance flags: {finance_access_farmers} finance-ready, {insurance_access_farmers} insurance-ready.",
            f"Estimated yield on record: {format_quantity(estimated_yield_kg, 'kg')}.",
        ],
        bullet=True,
    )
    draw_list_box(
        "Recommended Follow-up",
        44 + ((width - 78) / 2),
        62,
        (width - 78) / 2,
        148,
        follow_up_lines(),
        bullet=True,
        fill_color="#fcfaf7",
        stroke_color="#e4ddd0",
    )
    finish_page(final=(farmer_page_count == 0 and plot_schedule_page_count == 0 and total_plots == 0))

    if farmer_rows:
        farmer_chunks = [farmer_rows[idx : idx + farmer_page_size] for idx in range(0, len(farmer_rows), farmer_page_size)]
        for chunk_index, chunk in enumerate(farmer_chunks, start=1):
            table_x, table_y, table_w, table_h = draw_table_shell(
                "Farmer Registry",
                "Programme registry view for organisation targeting, support planning, and supervision follow-up.",
            )
            columns = [
                ("Farmer", 34, 124),
                ("Contact", 158, 92),
                ("Location", 250, 86),
                ("Primary Crop", 336, 82),
                ("Plots", 418, 38),
                ("Area (ha)", 456, 54),
                ("Visits", 510, 52),
            ]
            header_y = table_y + table_h - 34
            c.setFillColor(HexColor("#eff6f0"))
            c.rect(table_x + 1, header_y - 12, table_w - 2, 18, stroke=0, fill=1)
            c.setFont("Helvetica-Bold", table_header_size)
            c.setFillColorRGB(0.13, 0.13, 0.13)
            for label, x_pos, _ in columns:
                c.drawString(x_pos, header_y, label)
            row_y = header_y - 18
            row_height = 20
            for row_index, row in enumerate(chunk):
                profile = row.get("profile_data") or {}
                if row_index % 2 == 0:
                    c.setFillColor(HexColor("#fafcfa"))
                    c.rect(table_x + 1, row_y - 10, table_w - 2, row_height, stroke=0, fill=1)
                c.setStrokeColor(HexColor("#e5ece6"))
                c.setLineWidth(0.4)
                c.line(table_x + 1, row_y - 10, table_x + table_w - 1, row_y - 10)
                c.setFillColorRGB(0.18, 0.18, 0.18)
                c.setFont("Helvetica", table_font_size)
                farmer_name = str(row.get("name") or "-")[:24]
                contact_text = str(row.get("phone") or row.get("email") or "-")[:16]
                location_text = f"{str(profile.get('state_name') or row.get('community_name') or '-')[:10]} / {str(row.get('local_government') or '-')[:10]}"
                crop_text = str(profile.get("primary_crop") or "-")[:14]
                visit_text = f"{_safe_int_value(row.get('support_visit_done'))}/{_safe_int_value(row.get('support_target'))}"
                c.drawString(34, row_y, farmer_name)
                c.drawString(158, row_y, contact_text)
                c.drawString(250, row_y, location_text)
                c.drawString(336, row_y, crop_text)
                c.drawRightString(450, row_y, str(_safe_int_value(row.get("plot_count"))))
                c.drawRightString(506, row_y, f"{_safe_float_value(row.get('mapped_area_ha')):.2f}")
                c.drawRightString(560, row_y, visit_text)
                row_y -= row_height
            c.setFont("Helvetica", note_font_size)
            c.setFillColorRGB(0.32, 0.32, 0.32)
            c.drawString(table_x + 10, table_y + 10, f"Farmer records on this page: {len(chunk)}")
            c.drawRightString(table_x + table_w - 10, table_y + 10, f"Registry page {chunk_index}/{len(farmer_chunks)}")
            finish_page(final=(chunk_index == len(farmer_chunks) and plot_schedule_page_count == 0 and total_plots == 0))

    if plot_rows:
        plot_chunks = [plot_rows[idx : idx + plot_schedule_page_size] for idx in range(0, len(plot_rows), plot_schedule_page_size)]
        for chunk_index, chunk in enumerate(plot_chunks, start=1):
            table_x, table_y, table_w, table_h = draw_table_shell(
                "Plot Inventory Schedule",
                "Mapped plot register for field evidence, supervision, and downstream compliance or finance review.",
            )
            columns = [
                ("Plot", 34, 112),
                ("Farmer", 146, 94),
                ("Crop", 240, 80),
                ("Season", 320, 80),
                ("Area", 400, 44),
                ("Capture", 444, 50),
                ("Review", 494, 36),
                ("Photo", 530, 30),
            ]
            header_y = table_y + table_h - 34
            c.setFillColor(HexColor("#eff6f0"))
            c.rect(table_x + 1, header_y - 12, table_w - 2, 18, stroke=0, fill=1)
            c.setFont("Helvetica-Bold", table_header_size)
            c.setFillColorRGB(0.13, 0.13, 0.13)
            for label, x_pos, _ in columns:
                c.drawString(x_pos, header_y, label)
            row_y = header_y - 18
            row_height = 20
            for row_index, row in enumerate(chunk):
                record_profile = row.get("record_profile_data") or {}
                if row_index % 2 == 0:
                    c.setFillColor(HexColor("#fafcfa"))
                    c.rect(table_x + 1, row_y - 10, table_w - 2, row_height, stroke=0, fill=1)
                c.setStrokeColor(HexColor("#e5ece6"))
                c.setLineWidth(0.4)
                c.line(table_x + 1, row_y - 10, table_x + table_w - 1, row_y - 10)
                c.setFont("Helvetica", table_font_size)
                c.setFillColorRGB(0.18, 0.18, 0.18)
                season_bits = [str(record_profile.get("season_name") or "").strip(), str(record_profile.get("season_year") or "").strip()]
                season_text = " ".join([item for item in season_bits if item]).strip() or "-"
                c.drawString(34, row_y, format_plot_label(row)[:22])
                c.drawString(146, row_y, str(row.get("custodian_name") or "-")[:18])
                c.drawString(240, row_y, str(record_profile.get("commodity") or row.get("species") or "-")[:14])
                c.drawString(320, row_y, season_text[:14])
                c.drawRightString(438, row_y, f"{_safe_float_value(record_profile.get('area_hectares') or row.get('existing_area_ha')):.2f}")
                c.drawString(444, row_y, _format_agric_label(record_profile.get("boundary_capture_method"))[:10])
                c.drawString(494, row_y, _format_agric_label(row.get("last_review_state"))[:8])
                c.drawString(530, row_y, "Yes" if _first_available_plot_photo_url(row) else "No")
                row_y -= row_height
            c.setFont("Helvetica", note_font_size)
            c.setFillColorRGB(0.32, 0.32, 0.32)
            c.drawString(table_x + 10, table_y + 10, f"Plots on this page: {len(chunk)}")
            c.drawRightString(table_x + table_w - 10, table_y + 10, f"Schedule page {chunk_index}/{len(plot_chunks)}")
            finish_page(final=(chunk_index == len(plot_chunks) and total_plots == 0))

    for plot_index, row in enumerate(plot_rows, start=1):
        record_profile = row.get("record_profile_data") or {}
        farmer_profile = row.get("custodian_profile_data") or {}
        plot_name = format_plot_label(row)
        plot_code = str(record_profile.get("plot_code") or "").strip()
        commodity = str(record_profile.get("commodity") or row.get("species") or "-")
        season_bits = [str(record_profile.get("season_name") or "").strip(), str(record_profile.get("season_year") or "").strip()]
        season_label = " ".join([item for item in season_bits if item]).strip() or "Not captured"
        area_ha = _safe_float_value(record_profile.get("area_hectares") or row.get("existing_area_ha"))
        photo_url = _first_available_plot_photo_url(row)

        _draw_project_brand_header_bar(
            c,
            width,
            height,
            project,
            report_label="Plot Evidence Page",
            subtitle=f"Plot evidence page {plot_index} of {max(total_plots, 1)}",
            bar_height=64,
            bar_color="#163b2a",
        )
        c.setFillColorRGB(0.08, 0.14, 0.11)
        c.setFont("Helvetica-Bold", 15)
        c.drawString(34, height - 84, plot_name[:58])
        c.setFont("Helvetica", 9.3)
        c.setFillColorRGB(0.24, 0.24, 0.24)
        meta_line = f"Farmer: {str(row.get('custodian_name') or 'Farmer not linked')[:26]} | Crop: {commodity[:20]} | Area: {format_area(area_ha)} | Season: {season_label[:20]}"
        if plot_code:
            meta_line = f"Code: {plot_code[:14]} | {meta_line}"
        c.drawString(34, height - 98, meta_line[:126])

        _draw_agric_map_panel(
            c,
            x=34,
            y=404,
            w=252,
            h=228,
            map_png=overview_map_png,
            map_view=overview_map_view,
            plot_rows=plot_rows,
            highlight_plot_id=_safe_int_value(row.get("id")),
        )
        _draw_plot_geometry_zoom_panel(
            c,
            x=308,
            y=404,
            w=252,
            h=228,
            row=row,
        )

        farmer_lines = [
            f"Farmer: {str(row.get('custodian_name') or '-')}",
            f"Farmer code / ID: {str(farmer_profile.get('farmer_code') or farmer_profile.get('national_id') or '-')}",
            f"Contact: {str(row.get('phone') or row.get('email') or '-')}",
            f"Group: {str(farmer_profile.get('farmer_group') or '-')}",
            f"Location: {str(farmer_profile.get('state_name') or row.get('community_name') or '-')}, {str(row.get('local_government') or '-')}",
            f"Land tenure: {_format_agric_label(farmer_profile.get('land_tenure'))}",
            f"Finance / insurance: {_format_yes_no_label(farmer_profile.get('finance_access'))} / {_format_yes_no_label(farmer_profile.get('insurance_access'))}",
        ]
        plot_lines = [
            f"Commodity / variety: {commodity} / {str(record_profile.get('variety') or '-')}",
            f"Area: {format_area(area_ha)}",
            f"Season: {season_label}",
            f"Irrigation: {_format_agric_label(record_profile.get('irrigation_type'))}",
            f"Production stage: {_format_agric_label(record_profile.get('production_stage'))}",
            f"Boundary capture: {_format_agric_label(record_profile.get('boundary_capture_method'))}",
            f"Estimated yield: {format_quantity(_safe_float_value(record_profile.get('estimated_yield_kg')), 'kg')}",
        ]
        status_lines = [
            f"Field capture tasks: {format_count_pair(_safe_int_value(row.get('field_capture_done')), _safe_int_value(row.get('field_capture_assigned')))} closed",
            f"Support visits: {format_count_pair(_safe_int_value(row.get('support_visit_done')), _safe_int_value(row.get('support_visit_assigned')))} completed",
            f"Review state: {_format_agric_label(row.get('last_review_state'))}",
            f"Record status: {_format_agric_label(row.get('status'))}",
            f"Created by: {str(row.get('created_by') or '-')[:20]}",
            f"Created date: {str(row.get('created_at') or '-')[:10]}",
        ]
        box_w = (width - 68 - 16) / 3
        draw_list_box("Farmer Profile", 34, 220, box_w, 152, farmer_lines)
        draw_list_box("Plot Profile", 42 + box_w, 220, box_w, 152, plot_lines, fill_color="#fbfcfb")
        draw_list_box("Programme Status", 50 + (box_w * 2), 220, box_w, 152, status_lines, fill_color="#fcfaf7", stroke_color="#e4ddd0")

        notes_lines = [
            f"GPS anchor: {str(row.get('lat') or '-')[:18]}, {str(row.get('lng') or '-')[:18]}",
            f"Field notes: {str(row.get('notes') or 'No additional field notes were captured for this plot.')}",
        ]
        if include_photos and photo_url:
            draw_list_box("Coordinates & Field Notes", 34, 144, width - 68, 62, notes_lines)
            _draw_plot_photo_panel(c, x=34, y=34, w=width - 68, h=98, row=row, image_cache=image_cache)
        else:
            if include_photos and not photo_url:
                notes_lines.append("Photo evidence: No plot photo was available to embed for this record.")
            draw_list_box("Coordinates & Field Notes", 34, 72, width - 68, 134, notes_lines)

        finish_page(final=(plot_index == total_plots))

    if total_pages == 0:
        finish_page(final=True)

    c.save()


def render_green_relief_programme_pdf(
    output_path: str,
    project: dict,
    summary: dict,
    beneficiary_rows: list[dict],
    site_rows: list[dict],
    overview_map_png: bytes | None = None,
    overview_map_view: dict | None = None,
    include_photos: bool = False,
):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4
    relief_config = project.get("relief_config") if isinstance(project.get("relief_config"), dict) else {}
    body_font = "Helvetica"
    body_font_size = 8.8
    body_line_gap = 10.4
    table_font_size = 7.9
    table_header_size = 8.1
    note_font_size = 8.1
    total_sites = len(site_rows)
    beneficiary_page_size = 24
    site_schedule_page_size = 24
    beneficiary_page_count = int(math.ceil(len(beneficiary_rows) / float(beneficiary_page_size))) if beneficiary_rows else 0
    site_schedule_page_count = int(math.ceil(total_sites / float(site_schedule_page_size))) if site_rows else 0
    total_pages = 2 + beneficiary_page_count + site_schedule_page_count + total_sites
    current_page = 1

    registered_beneficiaries = _safe_int_value(summary.get("registered_beneficiaries"))
    verified_beneficiaries = _safe_int_value(summary.get("verified_beneficiaries"))
    mapped_sites = _safe_int_value(summary.get("mapped_sites"))
    mapped_area_ha = _safe_float_value(summary.get("mapped_area_ha"))
    allocated_units = _safe_float_value(summary.get("allocated_units"))
    support_visit_done = _safe_int_value(summary.get("support_visit_done"))
    support_target_total = _safe_int_value(summary.get("support_target_total"))
    support_visit_live = _safe_int_value(summary.get("support_visit_live"))
    field_capture_done = _safe_int_value(summary.get("field_capture_done"))
    field_capture_assigned = _safe_int_value(summary.get("field_capture_assigned"))
    field_capture_live = _safe_int_value(summary.get("field_capture_live"))
    polygon_sites = _safe_int_value(summary.get("polygon_sites"))
    photo_evidence_sites = _safe_int_value(summary.get("photo_evidence_sites"))
    linked_beneficiary_sites = _safe_int_value(summary.get("linked_beneficiary_sites"))
    damage_profile_sites = _safe_int_value(summary.get("damage_profile_sites"))
    response_profile_sites = _safe_int_value(summary.get("response_profile_sites"))
    reviewed_sites = _safe_int_value(summary.get("reviewed_sites"))
    identity_ready_beneficiaries = _safe_int_value(summary.get("identity_ready_beneficiaries"))
    vulnerable_beneficiaries = _safe_int_value(summary.get("vulnerable_beneficiaries"))
    displaced_beneficiaries = _safe_int_value(summary.get("displaced_beneficiaries"))
    institutional_beneficiaries = _safe_int_value(summary.get("institutional_beneficiaries"))
    total_population_served = _safe_int_value(summary.get("total_population_served"))
    total_estimated_repair_cost = _safe_float_value(summary.get("total_estimated_repair_cost"))

    def format_area(value: float) -> str:
        return f"{value:.2f} ha" if value > 0 else "Not captured"

    def format_currency(value: float) -> str:
        return f"NGN {value:,.0f}" if value > 0 else "Not declared"

    def format_quantity(value: float, suffix: str) -> str:
        return f"{value:,.0f} {suffix}" if value > 0 else "Not declared"

    def format_count_pair(done: int, target: int) -> str:
        return f"{done}/{target}" if target > 0 else "0/0"

    def format_site_label(row: dict) -> str:
        record_profile = row.get("record_profile_data") or {}
        return str(record_profile.get("asset_code") or record_profile.get("asset_name") or record_profile.get("plot_name") or f"Site #{row.get('id', '-')}")

    def draw_footer() -> None:
        c.setStrokeColor(HexColor("#d6dfd9"))
        c.setLineWidth(0.5)
        c.line(34, 22, width - 34, 22)
        c.setFont("Helvetica", 7.8)
        c.setFillColorRGB(0.34, 0.34, 0.34)
        c.drawString(34, 10, "LandCheck Relief & Recovery programme report")
        c.drawRightString(width - 34, 10, f"Page {current_page} of {max(total_pages, 1)}")

    def finish_page(*, final: bool = False) -> None:
        nonlocal current_page
        draw_footer()
        if not final:
            c.showPage()
            current_page += 1

    def draw_section_header(
        title: str,
        subtitle: str,
        *,
        report_label: str,
        bar_height: float = 74,
        bar_color: str = "#113b24",
    ) -> None:
        _draw_project_brand_header_bar(
            c,
            width,
            height,
            project,
            report_label=report_label,
            subtitle=subtitle,
            bar_height=bar_height,
            bar_color=bar_color,
        )
        c.setFillColorRGB(0.08, 0.14, 0.11)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(34, height - bar_height - 20, title[:76])
        c.setFont("Helvetica", 9.3)
        c.setFillColorRGB(0.26, 0.26, 0.26)
        c.drawString(34, height - bar_height - 33, subtitle[:118])

    def draw_list_box(
        title: str,
        x: float,
        y: float,
        w: float,
        h: float,
        lines: list[str],
        *,
        fill_color: str = "#f8faf9",
        stroke_color: str = "#d7e5db",
        bullet: bool = False,
    ) -> None:
        _draw_rounded_box(c, x, y, w, h, 7, fill_color=HexColor(fill_color), stroke_color=HexColor(stroke_color))
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.12, 0.12, 0.12)
        c.drawString(x + 10, y + h - 16, title[:54])
        current_y = y + h - 30
        c.setFont(body_font, body_font_size)
        c.setFillColorRGB(0.16, 0.16, 0.16)
        for line in lines:
            prefix = "- " if bullet else ""
            wrapped = _wrap_pdf_text_lines(c, f"{prefix}{line}", body_font, body_font_size, w - 20)
            for idx, part in enumerate(wrapped[:4]):
                if current_y < y + 10:
                    return
                if bullet and idx > 0 and part.startswith("- "):
                    part = f"  {part[2:]}"
                c.drawString(x + 10, current_y, part)
                current_y -= body_line_gap

    def draw_table_shell(title: str, subtitle: str) -> tuple[float, float, float, float]:
        draw_section_header(title, subtitle, report_label="Relief & Recovery Report")
        table_x = 34
        table_y = 56
        table_w = width - 68
        table_h = height - 172
        _draw_rounded_box(c, table_x, table_y, table_w, table_h, 8, fill_color=HexColor("#fbfcfb"), stroke_color=HexColor("#d7e5db"))
        return table_x, table_y, table_w, table_h

    def executive_summary_lines() -> list[str]:
        lines = [
            f"{registered_beneficiaries} beneficiary record(s) are linked to {mapped_sites} mapped site(s) covering {format_area(mapped_area_ha)}.",
        ]
        top_assets = summary.get("top_asset_types") or []
        if top_assets:
            top_asset = top_assets[0] or {}
            lines.append(
                f"Current site inventory is led by {str(top_asset.get('label') or 'Unknown')} ({_safe_int_value(top_asset.get('site_count'))} mapped site record(s))."
            )
        if support_target_total > 0:
            lines.append(
                f"Relief or recovery visits completed: {support_visit_done} of {support_target_total}, with {support_visit_live} still outstanding."
            )
        if allocated_units > 0:
            lines.append(f"Recorded support allocations total {allocated_units:,.0f} units, kits, or material packages for this project snapshot.")
        if field_capture_assigned > 0:
            lines.append(
                f"Initial site capture tasks closed: {field_capture_done} of {field_capture_assigned}, with {field_capture_live} still open."
            )
        elif mapped_sites > 0:
            lines.append("Mapped sites are already on record; no separate first-capture task backlog is currently open.")
        return lines[:5]

    def coverage_snapshot_lines() -> list[str]:
        return [
            f"Polygon boundary coverage: {polygon_sites}/{max(mapped_sites, 1)} site(s) ({_safe_float_value(summary.get('geo_readiness_pct')):.1f}%).",
            f"Photo evidence captured: {photo_evidence_sites}/{max(mapped_sites, 1)} site(s) ({_safe_float_value(summary.get('photo_readiness_pct')):.1f}%).",
            f"Beneficiary ID / code ready: {identity_ready_beneficiaries}/{max(registered_beneficiaries, 1)} beneficiary record(s) ({_safe_float_value(summary.get('identity_readiness_pct')):.1f}%).",
            f"Damage and response profiling: {damage_profile_sites}/{max(mapped_sites, 1)} damage-ready, {response_profile_sites}/{max(mapped_sites, 1)} response-ready.",
            f"Submitted or reviewed site records: {reviewed_sites}/{max(mapped_sites, 1)} ({_safe_float_value(summary.get('review_readiness_pct')):.1f}%).",
        ]

    def follow_up_lines() -> list[str]:
        lines: list[str] = []
        missing_identity = max(registered_beneficiaries - identity_ready_beneficiaries, 0)
        missing_photos = max(mapped_sites - photo_evidence_sites, 0)
        missing_reviews = max(mapped_sites - reviewed_sites, 0)
        missing_geo = max(mapped_sites - polygon_sites, 0)
        missing_damage = max(mapped_sites - damage_profile_sites, 0)
        if missing_identity > 0:
            lines.append(f"Complete beneficiary code or government ID for {missing_identity} registry record(s).")
        if missing_geo > 0:
            lines.append(f"Close boundary mapping for {missing_geo} site record(s) without polygon coverage.")
        if missing_damage > 0:
            lines.append(f"Capture damage level and response pathway for {missing_damage} site record(s) still missing assessment profiling.")
        if missing_photos > 0:
            lines.append(f"Capture site photos for {missing_photos} mapped site record(s) to strengthen field evidence.")
        if missing_reviews > 0:
            lines.append(f"Submit or review {missing_reviews} site record(s) still outside the review-ready set.")
        if support_target_total > support_visit_done:
            lines.append(f"Complete {support_target_total - support_visit_done} planned relief or recovery visit(s) still outstanding.")
        if field_capture_assigned > field_capture_done:
            lines.append(f"Close {field_capture_assigned - field_capture_done} remaining initial site-capture task(s).")
        if not lines:
            lines.append("No major registry or field-evidence gaps are flagged in the current project snapshot.")
        return lines[:5]

    image_cache: dict = {}
    if include_photos:
        photo_seed_rows = []
        for row in site_rows:
            photo_url = _first_available_plot_photo_url(row)
            if photo_url:
                item = dict(row)
                item["photo_url"] = photo_url
                photo_seed_rows.append(item)
        _prefetch_photo_readers(photo_seed_rows, image_cache)

    draw_section_header(
        "Programme Summary",
        "Organisation-ready beneficiary registry, mapped site evidence, and relief-delivery snapshot.",
        report_label="Relief & Recovery Report",
        bar_height=78,
    )

    c.setFont("Helvetica", 9.4)
    c.setFillColorRGB(0.22, 0.22, 0.22)
    meta_parts = []
    if project.get("location_text"):
        meta_parts.append(f"Location: {project.get('location_text')}")
    if relief_config.get("program_type"):
        meta_parts.append(f"Programme: {_format_agric_label(relief_config.get('program_type'))}")
    if relief_config.get("intervention_focus"):
        meta_parts.append(f"Focus: {str(relief_config.get('intervention_focus'))[:34]}")
    if relief_config.get("package_types"):
        meta_parts.append(f"Packages: {str(relief_config.get('package_types'))[:34]}")
    if relief_config.get("target_zone"):
        meta_parts.append(f"Target Zone: {str(relief_config.get('target_zone'))[:24]}")
    if project.get("sponsor"):
        meta_parts.append(f"Sponsor: {project.get('sponsor')}")
    c.drawString(34, height - 125, " | ".join(meta_parts)[:128])

    card_w = (width - 68 - 16) / 3
    card_h = 56
    top_y = height - 205
    cards = [
        ("Registered Beneficiaries", registered_beneficiaries, f"Verified {verified_beneficiaries}"),
        ("Mapped Sites", mapped_sites, f"Beneficiary linked {linked_beneficiary_sites}"),
        ("Mapped Area", f"{mapped_area_ha:.2f} ha" if mapped_area_ha > 0 else "0.00 ha", "Portfolio footprint"),
        ("Relief Visits", format_count_pair(support_visit_done, support_target_total), f"Open {support_visit_live}"),
        ("Photo Evidence", format_count_pair(photo_evidence_sites, mapped_sites), "Sites with photo"),
        ("Review Coverage", format_count_pair(reviewed_sites, mapped_sites), "Submitted or approved"),
    ]
    for index, (label, value, sub) in enumerate(cards):
        row_index = index // 3
        col_index = index % 3
        _draw_stat_card(
            c,
            34 + (col_index * (card_w + 8)),
            top_y - (row_index * (card_h + 10)),
            card_w,
            card_h,
            label,
            value,
            sub=sub,
            color=HexColor("#f4faf6" if index % 2 == 0 else "#fbfcfb"),
        )

    top_assets = summary.get("top_asset_types") or []
    if top_assets:
        bar_rows = []
        colors = ["#8b1e3f", "#b42318", "#cf5c36", "#6857a6", "#0f766e", "#2563eb"]
        for idx, item in enumerate(top_assets[:6]):
            bar_rows.append(
                (
                    str(item.get("label") or "Unknown")[:18],
                    _safe_float_value(item.get("site_count")),
                    colors[idx % len(colors)],
                )
            )
        _draw_bar_chart(c, 34, 214, width - 68, 110, bar_rows, title="Mapped Asset Mix (site count)")

    draw_list_box(
        "Executive Summary",
        34,
        52,
        (width - 78) / 2,
        148,
        executive_summary_lines(),
        bullet=True,
    )
    draw_list_box(
        "Data Coverage Snapshot",
        44 + ((width - 78) / 2),
        52,
        (width - 78) / 2,
        148,
        coverage_snapshot_lines(),
        bullet=True,
        fill_color="#f7faf8",
    )
    finish_page()

    draw_section_header(
        "Portfolio Map & Operational Readiness",
        "Project map, mapped site-boundary coverage, and immediate follow-up priorities.",
        report_label="Relief & Recovery Report",
    )
    _draw_agric_map_panel(
        c,
        x=34,
        y=230,
        w=width - 68,
        h=340,
        map_png=overview_map_png,
        map_view=overview_map_view,
        plot_rows=site_rows,
        highlight_plot_id=None,
        entity_singular_label="site",
        entity_plural_label="sites",
    )
    draw_list_box(
        "Portfolio Readiness",
        34,
        62,
        (width - 78) / 2,
        148,
        [
            f"Beneficiary-linked sites: {linked_beneficiary_sites}/{max(mapped_sites, 1)}.",
            f"Damage profiling complete: {damage_profile_sites}/{max(mapped_sites, 1)} site(s).",
            f"Response pathway captured: {response_profile_sites}/{max(mapped_sites, 1)} site(s).",
            f"Vulnerability flags on record: {vulnerable_beneficiaries}/{max(registered_beneficiaries, 1)} beneficiary record(s).",
            f"Displacement-sensitive cases: {displaced_beneficiaries}; institutional or community records: {institutional_beneficiaries}.",
            f"Population served on record: {total_population_served}; estimated repair exposure: {format_currency(total_estimated_repair_cost)}.",
        ],
        bullet=True,
    )
    draw_list_box(
        "Recommended Follow-up",
        44 + ((width - 78) / 2),
        62,
        (width - 78) / 2,
        148,
        follow_up_lines(),
        bullet=True,
        fill_color="#fcfaf7",
        stroke_color="#e4ddd0",
    )
    finish_page(final=(beneficiary_page_count == 0 and site_schedule_page_count == 0 and total_sites == 0))

    if beneficiary_rows:
        beneficiary_chunks = [beneficiary_rows[idx : idx + beneficiary_page_size] for idx in range(0, len(beneficiary_rows), beneficiary_page_size)]
        for chunk_index, chunk in enumerate(beneficiary_chunks, start=1):
            table_x, table_y, table_w, table_h = draw_table_shell(
                "Beneficiary Registry",
                "Programme registry view for targeting, distribution planning, and follow-up accountability.",
            )
            columns = [
                ("Beneficiary", 34, 118),
                ("Type", 152, 64),
                ("Contact", 216, 88),
                ("Settlement", 304, 86),
                ("Support", 390, 76),
                ("Sites", 466, 36),
                ("Visits", 510, 52),
            ]
            header_y = table_y + table_h - 34
            c.setFillColor(HexColor("#eff6f0"))
            c.rect(table_x + 1, header_y - 12, table_w - 2, 18, stroke=0, fill=1)
            c.setFont("Helvetica-Bold", table_header_size)
            c.setFillColorRGB(0.13, 0.13, 0.13)
            for label, x_pos, _ in columns:
                c.drawString(x_pos, header_y, label)
            row_y = header_y - 18
            row_height = 20
            for row_index, row in enumerate(chunk):
                profile = row.get("profile_data") or {}
                if row_index % 2 == 0:
                    c.setFillColor(HexColor("#fafcfa"))
                    c.rect(table_x + 1, row_y - 10, table_w - 2, row_height, stroke=0, fill=1)
                c.setStrokeColor(HexColor("#e5ece6"))
                c.setLineWidth(0.4)
                c.line(table_x + 1, row_y - 10, table_x + table_w - 1, row_y - 10)
                c.setFillColorRGB(0.18, 0.18, 0.18)
                c.setFont("Helvetica", table_font_size)
                beneficiary_name = str(row.get("name") or "-")[:22]
                type_text = _format_agric_label(row.get("custodian_type"))[:10]
                contact_text = str(row.get("phone") or row.get("email") or "-")[:16]
                location_text = str(profile.get("current_settlement") or row.get("community_name") or row.get("local_government") or "-")[:16]
                support_text = str(profile.get("support_category") or profile.get("priority_needs") or "-")[:12]
                visit_text = f"{_safe_int_value(row.get('support_visit_done'))}/{_safe_int_value(row.get('support_target'))}"
                c.drawString(34, row_y, beneficiary_name)
                c.drawString(152, row_y, type_text)
                c.drawString(216, row_y, contact_text)
                c.drawString(304, row_y, location_text)
                c.drawString(390, row_y, support_text)
                c.drawRightString(502, row_y, str(_safe_int_value(row.get("site_count"))))
                c.drawRightString(560, row_y, visit_text)
                row_y -= row_height
            c.setFont("Helvetica", note_font_size)
            c.setFillColorRGB(0.32, 0.32, 0.32)
            c.drawString(table_x + 10, table_y + 10, f"Beneficiary records on this page: {len(chunk)}")
            c.drawRightString(table_x + table_w - 10, table_y + 10, f"Registry page {chunk_index}/{len(beneficiary_chunks)}")
            finish_page(final=(chunk_index == len(beneficiary_chunks) and site_schedule_page_count == 0 and total_sites == 0))

    if site_rows:
        site_chunks = [site_rows[idx : idx + site_schedule_page_size] for idx in range(0, len(site_rows), site_schedule_page_size)]
        for chunk_index, chunk in enumerate(site_chunks, start=1):
            table_x, table_y, table_w, table_h = draw_table_shell(
                "Site Inventory Schedule",
                "Mapped site register for assessment evidence, recovery prioritisation, and donor or government reporting.",
            )
            columns = [
                ("Site", 34, 112),
                ("Beneficiary", 146, 92),
                ("Asset", 238, 74),
                ("Damage", 312, 64),
                ("Response", 376, 68),
                ("Area", 444, 38),
                ("Review", 484, 40),
                ("Photo", 528, 30),
            ]
            header_y = table_y + table_h - 34
            c.setFillColor(HexColor("#eff6f0"))
            c.rect(table_x + 1, header_y - 12, table_w - 2, 18, stroke=0, fill=1)
            c.setFont("Helvetica-Bold", table_header_size)
            c.setFillColorRGB(0.13, 0.13, 0.13)
            for label, x_pos, _ in columns:
                c.drawString(x_pos, header_y, label)
            row_y = header_y - 18
            row_height = 20
            for row_index, row in enumerate(chunk):
                record_profile = row.get("record_profile_data") or {}
                if row_index % 2 == 0:
                    c.setFillColor(HexColor("#fafcfa"))
                    c.rect(table_x + 1, row_y - 10, table_w - 2, row_height, stroke=0, fill=1)
                c.setStrokeColor(HexColor("#e5ece6"))
                c.setLineWidth(0.4)
                c.line(table_x + 1, row_y - 10, table_x + table_w - 1, row_y - 10)
                c.setFont("Helvetica", table_font_size)
                c.setFillColorRGB(0.18, 0.18, 0.18)
                c.drawString(34, row_y, format_site_label(row)[:20])
                c.drawString(146, row_y, str(row.get("custodian_name") or "-")[:16])
                c.drawString(238, row_y, _format_agric_label(record_profile.get("asset_type") or row.get("species"))[:12])
                c.drawString(312, row_y, _format_agric_label(record_profile.get("damage_level"))[:10])
                c.drawString(376, row_y, _format_agric_label(record_profile.get("response_pathway"))[:11])
                c.drawRightString(478, row_y, f"{_safe_float_value(record_profile.get('area_hectares') or row.get('existing_area_ha')):.2f}")
                c.drawString(484, row_y, _format_agric_label(row.get("last_review_state"))[:8])
                c.drawString(528, row_y, "Yes" if _first_available_plot_photo_url(row) else "No")
                row_y -= row_height
            c.setFont("Helvetica", note_font_size)
            c.setFillColorRGB(0.32, 0.32, 0.32)
            c.drawString(table_x + 10, table_y + 10, f"Sites on this page: {len(chunk)}")
            c.drawRightString(table_x + table_w - 10, table_y + 10, f"Schedule page {chunk_index}/{len(site_chunks)}")
            finish_page(final=(chunk_index == len(site_chunks) and total_sites == 0))

    for site_index, row in enumerate(site_rows, start=1):
        record_profile = row.get("record_profile_data") or {}
        beneficiary_profile = row.get("custodian_profile_data") or {}
        site_name = format_site_label(row)
        site_code = str(record_profile.get("asset_code") or "").strip()
        asset_type = _format_agric_label(record_profile.get("asset_type") or row.get("species"))
        damage_label = _format_agric_label(record_profile.get("damage_level"))
        response_label = _format_agric_label(record_profile.get("response_pathway"))
        area_ha = _safe_float_value(record_profile.get("area_hectares") or row.get("existing_area_ha"))
        photo_url = _first_available_plot_photo_url(row)

        _draw_project_brand_header_bar(
            c,
            width,
            height,
            project,
            report_label="Site Evidence Page",
            subtitle=f"Site evidence page {site_index} of {max(total_sites, 1)}",
            bar_height=64,
            bar_color="#163b2a",
        )
        c.setFillColorRGB(0.08, 0.14, 0.11)
        c.setFont("Helvetica-Bold", 15)
        c.drawString(34, height - 84, site_name[:58])
        c.setFont("Helvetica", 9.3)
        c.setFillColorRGB(0.24, 0.24, 0.24)
        meta_line = f"Beneficiary: {str(row.get('custodian_name') or 'Beneficiary not linked')[:24]} | Asset: {asset_type[:20]} | Area: {format_area(area_ha)} | Damage: {damage_label[:18]}"
        if site_code:
            meta_line = f"Code: {site_code[:14]} | {meta_line}"
        c.drawString(34, height - 98, meta_line[:126])

        _draw_agric_map_panel(
            c,
            x=34,
            y=404,
            w=252,
            h=228,
            map_png=overview_map_png,
            map_view=overview_map_view,
            plot_rows=site_rows,
            highlight_plot_id=_safe_int_value(row.get("id")),
            entity_singular_label="site",
            entity_plural_label="sites",
        )
        _draw_plot_geometry_zoom_panel(
            c,
            x=308,
            y=404,
            w=252,
            h=228,
            row=row,
            entity_singular_label="site",
        )

        beneficiary_lines = [
            f"Beneficiary: {str(row.get('custodian_name') or '-')}",
            f"Beneficiary code / ID: {str(beneficiary_profile.get('beneficiary_code') or beneficiary_profile.get('government_id') or '-')}",
            f"Contact: {str(row.get('phone') or row.get('email') or '-')}",
            f"Displacement status: {_format_agric_label(beneficiary_profile.get('displacement_status'))}",
            f"Settlement: {str(beneficiary_profile.get('current_settlement') or row.get('community_name') or '-')}",
            f"Support category: {str(beneficiary_profile.get('support_category') or '-')}",
            f"Household / flags: {str(beneficiary_profile.get('household_size') or '-')} / {str(beneficiary_profile.get('vulnerability_flags') or '-')[:34]}",
        ]
        site_lines = [
            f"Asset type: {asset_type}",
            f"Damage level: {damage_label}",
            f"Response pathway: {response_label}",
            f"Area: {format_area(area_ha)}",
            f"Occupancy / tenure: {_format_agric_label(record_profile.get('occupancy_status'))} / {_format_agric_label(record_profile.get('tenure_status'))}",
            f"Population served: {_safe_int_value(record_profile.get('population_served'))}",
            f"Estimated repair cost: {format_currency(_safe_float_value(record_profile.get('estimated_repair_cost')))}",
        ]
        status_lines = [
            f"Initial capture tasks: {format_count_pair(_safe_int_value(row.get('field_capture_done')), _safe_int_value(row.get('field_capture_assigned')))} closed",
            f"Relief visits: {format_count_pair(_safe_int_value(row.get('support_visit_done')), _safe_int_value(row.get('support_visit_assigned')))} completed",
            f"Review state: {_format_agric_label(row.get('last_review_state'))}",
            f"Record status: {_format_agric_label(row.get('status'))}",
            f"Created by: {str(row.get('created_by') or '-')[:20]}",
            f"Created date: {str(row.get('created_at') or '-')[:10]}",
        ]
        box_w = (width - 68 - 16) / 3
        draw_list_box("Beneficiary Profile", 34, 220, box_w, 152, beneficiary_lines)
        draw_list_box("Site Profile", 42 + box_w, 220, box_w, 152, site_lines, fill_color="#fbfcfb")
        draw_list_box("Programme Status", 50 + (box_w * 2), 220, box_w, 152, status_lines, fill_color="#fcfaf7", stroke_color="#e4ddd0")

        notes_lines = [
            f"GPS anchor: {str(row.get('lat') or '-')[:18]}, {str(row.get('lng') or '-')[:18]}",
            f"Support package: {str(record_profile.get('support_package') or 'Not recorded')}",
            f"Field notes: {str(row.get('notes') or record_profile.get('reported_need') or 'No additional field notes were captured for this site.')}",
        ]
        if include_photos and photo_url:
            draw_list_box("Coordinates, Support & Field Notes", 34, 144, width - 68, 62, notes_lines)
            _draw_plot_photo_panel(c, x=34, y=34, w=width - 68, h=98, row=row, image_cache=image_cache, entity_singular_label="site")
        else:
            if include_photos and not photo_url:
                notes_lines.append("Photo evidence: No site photo was available to embed for this record.")
            draw_list_box("Coordinates, Support & Field Notes", 34, 72, width - 68, 134, notes_lines)

        finish_page(final=(site_index == total_sites))

    if total_pages == 0:
        finish_page(final=True)

    c.save()
