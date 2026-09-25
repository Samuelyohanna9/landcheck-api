from __future__ import annotations

"""Printable marketing PDFs (one-page flyer, multi-page brochure), rendered live from current
availability. Uses a bundled TrueType font so the naira sign prints correctly."""

import io
from typing import Any

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.services.estates.marketing_render import (
    BOLD_TTF,
    BRAND,
    BRAND_DARK,
    DEEP,
    INK,
    MINT,
    MUTED,
    REGULAR_TTF,
    STATUS_LABEL,
    MarketingContext,
    area_text,
    layout_png_for_documents,
    naira,
    naira_short,
)

_FONTS_READY = False
REG, BOLD = "LCSans", "LCSans-Bold"
PAGE_W, PAGE_H = A4
MARGIN = 40


def _fonts() -> None:
    global _FONTS_READY
    if _FONTS_READY:
        return
    pdfmetrics.registerFont(TTFont(REG, REGULAR_TTF))
    pdfmetrics.registerFont(TTFont(BOLD, BOLD_TTF))
    _FONTS_READY = True


def _c(rgb: tuple) -> tuple[float, float, float]:
    return (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)


def _fill(pdf: canvas.Canvas, rgb: tuple) -> None:
    pdf.setFillColorRGB(*_c(rgb))


def _text(pdf: canvas.Canvas, x: float, y: float, value: str, *, font: str = REG, size: float = 10, colour: tuple = INK, align: str = "left") -> None:
    pdf.setFont(font, size)
    _fill(pdf, colour)
    if align == "right":
        pdf.drawRightString(x, y, value)
    elif align == "center":
        pdf.drawCentredString(x, y, value)
    else:
        pdf.drawString(x, y, value)


def _fit(pdf: canvas.Canvas, value: str, width: float, font: str, start: float, minimum: float) -> float:
    size = start
    while size > minimum and pdf.stringWidth(value, font, size) > width:
        size -= 0.5
    return size


def _paragraph(pdf: canvas.Canvas, value: str, x: float, y: float, width: float, *, font: str = REG, size: float = 10.5, leading: float | None = None, colour: tuple = INK, max_lines: int = 40) -> float:
    leading = leading or size * 1.45
    lines = simpleSplit(str(value or ""), font, size, width)[:max_lines]
    pdf.setFont(font, size)
    _fill(pdf, colour)
    for line in lines:
        pdf.drawString(x, y, line)
        y -= leading
    return y


def _qr(pdf: canvas.Canvas, url: str, x: float, y: float, size: float) -> None:
    widget = qr.QrCodeWidget(url)
    widget.barWidth = size
    widget.barHeight = size
    widget.x = 0
    widget.y = 0
    drawing = Drawing(size, size)
    drawing.add(widget)
    renderPDF.draw(drawing, pdf, x, y)


def _image(pdf: canvas.Canvas, data: bytes, x: float, y: float, w: float, h: float) -> None:
    pdf.drawImage(ImageReader(io.BytesIO(data)), x, y, width=w, height=h, preserveAspectRatio=True, anchor="c", mask="auto")


def _logo(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y_top: float, max_w: float, max_h: float) -> float:
    """Draws the logo on a white plate. Returns the width used (0 when there is no logo)."""
    if not ctx.logo_bytes:
        return 0
    try:
        reader = ImageReader(io.BytesIO(ctx.logo_bytes))
        iw, ih = reader.getSize()
        scale = min(max_w / iw, max_h / ih)
        w, h = iw * scale, ih * scale
        pdf.setFillColorRGB(1, 1, 1)
        pdf.roundRect(x, y_top - h - 12, w + 14, h + 12, 6, fill=1, stroke=0)
        pdf.drawImage(reader, x + 7, y_top - h - 6, width=w, height=h, mask="auto")
        return w + 14
    except Exception:
        return 0


def _pill(pdf: canvas.Canvas, x_right: float, y: float, value: str, fill: tuple, ink: tuple = (255, 255, 255), size: float = 9) -> None:
    width = pdf.stringWidth(value, BOLD, size) + 18
    _fill(pdf, fill)
    pdf.roundRect(x_right - width, y, width, size + 10, (size + 10) / 2, fill=1, stroke=0)
    _text(pdf, x_right - width + 9, y + 5.5, value, font=BOLD, size=size, colour=ink)


def _stat_boxes(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y: float, width: float) -> None:
    gap = 10
    box_w = (width - gap * 2) / 3
    items = [(str(ctx.available), "Available", BRAND), (str(ctx.reserved), "Reserved", (214, 145, 0)), (str(ctx.sold), "Sold", (42, 120, 214))]
    for index, (value, label, colour) in enumerate(items):
        bx = x + index * (box_w + gap)
        _fill(pdf, (238, 246, 241))
        pdf.roundRect(bx, y, box_w, 54, 8, fill=1, stroke=0)
        _text(pdf, bx + box_w / 2, y + 28, value, font=BOLD, size=22, colour=colour, align="center")
        _text(pdf, bx + box_w / 2, y + 11, label.upper(), font=BOLD, size=7.5, colour=MUTED, align="center")


def _plots_table(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y: float, width: float, rows: int, *, title: str = "Available plots") -> float:
    _text(pdf, x, y, title, font=BOLD, size=12, colour=BRAND_DARK)
    y -= 8
    columns = [("Plot", 0.14), ("Size", 0.22), ("Price", 0.28), ("Details", 0.36)]
    _fill(pdf, DEEP)
    pdf.roundRect(x, y - 18, width, 18, 4, fill=1, stroke=0)
    cx = x + 8
    for label, share in columns:
        _text(pdf, cx, y - 12.5, label.upper(), font=BOLD, size=7.5, colour=(255, 255, 255))
        cx += width * share
    y -= 18
    shown = ctx.available_plots[:rows]
    for index, plot in enumerate(shown):
        if index % 2 == 0:
            _fill(pdf, (246, 249, 247))
            pdf.rect(x, y - 17, width, 17, fill=1, stroke=0)
        price = naira(plot.asking_price) if (ctx.show_prices and plot.asking_price is not None) else "On request"
        cx = x + 8
        cells = [str(plot.plot_number), area_text(plot.area_sqm), price, (plot.public_address or "")[:34]]
        for cell, (_label, share) in zip(cells, columns):
            _text(pdf, cx, y - 12, cell, font=BOLD if cell == cells[0] else REG, size=9, colour=INK)
            cx += width * share
        y -= 17
    remaining = len(ctx.available_plots) - len(shown)
    if remaining > 0:
        _text(pdf, x + 8, y - 12, f"+ {remaining} more available - scan the code for the full live list", size=8.5, colour=MUTED)
        y -= 20
    elif not shown:
        _text(pdf, x + 8, y - 12, "No plots are currently available.", size=9, colour=MUTED)
        y -= 20
    return y


def _payment_plan_line(ctx: MarketingContext) -> str | None:
    if not ctx.payment_plan:
        return None
    return "  /  ".join(f"{item.get('percentage')}% {item.get('label')}" for item in ctx.payment_plan)


def _footer(pdf: canvas.Canvas, ctx: MarketingContext) -> None:
    _text(pdf, PAGE_W / 2, 22, f"Availability as of {ctx.stamp}. Prices and availability are subject to change.  Powered by LandCheck Estates", size=7, colour=MUTED, align="center")


def _contact_block(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y: float, width: float) -> None:
    lines = []
    if ctx.agent_name:
        lines.append(("Your agent", ctx.agent_name))
    if ctx.contact_phone:
        lines.append(("Call / WhatsApp", ctx.contact_phone))
    lines.append(("Live map & reservations", ctx.page_url.split("?")[0].replace("https://", "")))
    for label, value in lines:
        _text(pdf, x, y, label.upper(), font=BOLD, size=7, colour=MUTED)
        size = _fit(pdf, value, width, BOLD, 11.5, 7)
        _text(pdf, x, y - 14, value, font=BOLD, size=size, colour=INK)
        y -= 34


def render_flyer_pdf(ctx: MarketingContext) -> bytes:
    _fonts()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"{ctx.estate.name} - flyer")
    content_w = PAGE_W - MARGIN * 2

    _fill(pdf, DEEP)
    pdf.rect(0, PAGE_H - 150, PAGE_W, 150, fill=1, stroke=0)
    logo_w = _logo(pdf, ctx, MARGIN, PAGE_H - 24, 150, 40)
    if not logo_w:
        _text(pdf, MARGIN, PAGE_H - 44, ctx.organization_name, font=BOLD, size=13, colour=(255, 255, 255))
    _pill(pdf, PAGE_W - MARGIN, PAGE_H - 46, "AVAILABLE NOW", (24, 112, 76), MINT)
    name_size = _fit(pdf, ctx.estate.name, content_w, BOLD, 30, 16)
    _text(pdf, MARGIN, PAGE_H - 98, ctx.estate.name, font=BOLD, size=name_size, colour=(255, 255, 255))
    if ctx.estate.public_tagline:
        _text(pdf, MARGIN, PAGE_H - 118, ctx.estate.public_tagline[:96], size=11, colour=MINT)
    if ctx.location:
        _text(pdf, MARGIN, PAGE_H - 136, ctx.location, size=9.5, colour=(210, 232, 220))

    map_h = 228
    map_top = PAGE_H - 168
    try:
        _image(pdf, layout_png_for_documents(ctx, 1500, int(1500 * map_h / content_w)), MARGIN, map_top - map_h, content_w, map_h)
    except ValueError:
        _fill(pdf, (238, 246, 241))
        pdf.roundRect(MARGIN, map_top - map_h, content_w, map_h, 10, fill=1, stroke=0)
    y = map_top - map_h - 14
    _stat_boxes(pdf, ctx, MARGIN, y - 54, content_w)
    y -= 54 + 26

    if ctx.min_price and ctx.show_prices:
        _text(pdf, MARGIN, y, f"Plots from {naira_short(ctx.min_price)}", font=BOLD, size=15, colour=BRAND_DARK)
        plan = _payment_plan_line(ctx)
        if plan:
            _text(pdf, PAGE_W - MARGIN, y, f"Pay in stages: {plan}", size=8.5, colour=MUTED, align="right")
        y -= 22
    band_h = 128
    band_top = 44 + band_h
    rows = max(3, int((y - band_top - 58) // 17))
    y = _plots_table(pdf, ctx, MARGIN, y, content_w, rows=min(rows, 12))

    _fill(pdf, (238, 246, 241))
    pdf.roundRect(MARGIN, 44, content_w, band_h, 10, fill=1, stroke=0)
    _qr(pdf, ctx.page_url, MARGIN + 14, 44 + (band_h - 100) / 2, 100)
    _text(pdf, MARGIN + 130, 44 + band_h - 24, "Scan for live availability", font=BOLD, size=13, colour=BRAND_DARK)
    _contact_block(pdf, ctx, MARGIN + 130, 44 + band_h - 46, content_w - 150)
    _footer(pdf, ctx)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _potential_label(forecast: dict) -> str:
    outlook = forecast.get("value_outlook")
    if isinstance(outlook, dict) and outlook.get("label"):
        return str(outlook["label"])
    growth = forecast.get("growth") or {}
    score = 0
    try:
        rate = float(growth.get("annual_percent_rate"))
        score += 2 if rate >= 3 else 1 if rate > 0 else 0
    except (TypeError, ValueError):
        pass
    try:
        frontier = float(growth.get("frontier_distance_m"))
        score += 2 if frontier <= 1000 else 1 if frontier <= 3000 else 0
    except (TypeError, ValueError):
        pass
    if growth.get("direction") and growth.get("direction") != "No clear direction":
        score += 1
    if (forecast.get("confidence") or {}).get("level") == "high":
        score += 1
    return "Strong" if score >= 5 else "Moderate" if score >= 3 else "Emerging" if score >= 1 else "Unclear"


def _brochure_header(pdf: canvas.Canvas, ctx: MarketingContext, title: str) -> float:
    _fill(pdf, DEEP)
    pdf.rect(0, PAGE_H - 70, PAGE_W, 70, fill=1, stroke=0)
    _text(pdf, MARGIN, PAGE_H - 42, title, font=BOLD, size=18, colour=(255, 255, 255))
    _text(pdf, PAGE_W - MARGIN, PAGE_H - 42, ctx.estate.name, size=9.5, colour=MINT, align="right")
    return PAGE_H - 100


def render_brochure_pdf(ctx: MarketingContext) -> bytes:
    _fonts()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"{ctx.estate.name} - brochure")
    content_w = PAGE_W - MARGIN * 2

    # Cover
    _fill(pdf, DEEP)
    pdf.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    logo_w = _logo(pdf, ctx, MARGIN + 6, PAGE_H - 50, 180, 56)
    if not logo_w:
        _text(pdf, MARGIN + 6, PAGE_H - 78, ctx.organization_name, font=BOLD, size=16, colour=(255, 255, 255))
    y = PAGE_H - 250
    for line in simpleSplit(ctx.estate.name, BOLD, 40, content_w - 12)[:3]:
        _text(pdf, MARGIN + 6, y, line, font=BOLD, size=40, colour=(255, 255, 255))
        y -= 48
    if ctx.estate.public_tagline:
        y = _paragraph(pdf, ctx.estate.public_tagline, MARGIN + 6, y - 6, content_w - 40, size=15, colour=MINT, max_lines=3)
    if ctx.location:
        _text(pdf, MARGIN + 6, y - 14, ctx.location, size=12, colour=(210, 232, 220))
    try:
        cover_map = layout_png_for_documents(ctx, 1500, 900)
        _fill(pdf, (246, 248, 250))
        pdf.roundRect(MARGIN, 150, content_w, 250, 14, fill=1, stroke=0)
        _image(pdf, cover_map, MARGIN + 8, 158, content_w - 16, 234)
    except ValueError:
        pass
    _text(pdf, MARGIN + 6, 96, f"{ctx.available} plots available  ·  {ctx.reserved} reserved  ·  {ctx.sold} sold", font=BOLD, size=12, colour=MINT)
    _text(pdf, MARGIN + 6, 70, f"Availability as of {ctx.stamp}", size=9, colour=(170, 205, 185))
    pdf.showPage()

    # About
    y = _brochure_header(pdf, ctx, "About the estate")
    description = ctx.estate.public_description or ctx.estate.description or "Explore the published estate layout and choose a plot that suits your plans."
    y = _paragraph(pdf, description, MARGIN, y, content_w, size=11, max_lines=18)
    y -= 18
    facts = [("Total plots", str(len(ctx.plots))), ("Available now", str(ctx.available))]
    if ctx.estate.approximate_area_sqm:
        facts.append(("Estate size", f"{float(ctx.estate.approximate_area_sqm) / 10000:,.1f} hectares"))
    if ctx.min_price and ctx.show_prices:
        facts.append(("Starting price", naira(ctx.min_price)))
    if ctx.location:
        facts.append(("Location", ctx.location))
    for label, value in facts:
        _fill(pdf, (238, 246, 241))
        pdf.roundRect(MARGIN, y - 30, content_w, 34, 8, fill=1, stroke=0)
        _text(pdf, MARGIN + 14, y - 17, label.upper(), font=BOLD, size=8, colour=MUTED)
        _text(pdf, PAGE_W - MARGIN - 14, y - 18, value, font=BOLD, size=11, colour=INK, align="right")
        y -= 42
    plan = _payment_plan_line(ctx)
    if plan:
        y -= 6
        _text(pdf, MARGIN, y, "Payment plan", font=BOLD, size=12, colour=BRAND_DARK)
        y -= 20
        gap = 10
        n = len(ctx.payment_plan)
        box_w = (content_w - gap * (n - 1)) / n
        for index, item in enumerate(ctx.payment_plan):
            bx = MARGIN + index * (box_w + gap)
            _fill(pdf, DEEP)
            pdf.roundRect(bx, y - 50, box_w, 54, 8, fill=1, stroke=0)
            _text(pdf, bx + box_w / 2, y - 22, f"{item.get('percentage')}%", font=BOLD, size=18, colour=(255, 255, 255), align="center")
            _text(pdf, bx + box_w / 2, y - 40, str(item.get("label"))[:22], size=8.5, colour=MINT, align="center")
        y -= 70
    _footer(pdf, ctx)
    pdf.showPage()

    # Area outlook (only when the developer has published it)
    if ctx.forecast and ctx.forecast.get("data_available"):
        forecast = ctx.forecast
        growth = forecast.get("growth") or {}
        y = _brochure_header(pdf, ctx, "Area outlook")
        label = _potential_label(forecast)
        _fill(pdf, (238, 246, 241))
        pdf.roundRect(MARGIN, y - 70, content_w, 74, 10, fill=1, stroke=0)
        _text(pdf, MARGIN + 16, y - 26, "LAND VALUE POTENTIAL", font=BOLD, size=8, colour=MUTED)
        _text(pdf, MARGIN + 16, y - 54, label, font=BOLD, size=24, colour=BRAND_DARK)
        headline = str((forecast.get("reach_estimate") or {}).get("headline") or "").replace("; this analysis does not treat that as a promise of future development", "")
        y = _paragraph(pdf, headline, MARGIN + 150, y - 24, content_w - 166, size=10, max_lines=4)
        y = min(y, PAGE_H - 100 - 74) - 20
        rows = []
        if growth.get("annual_percent_rate") is not None:
            rows.append(("Surrounding built-up growth", f"{growth['annual_percent_rate']}% per year (area growth, not price growth)"))
        if growth.get("direction"):
            rows.append(("Development moving toward", str(growth["direction"])))
        if growth.get("frontier_distance_m") is not None:
            distance = float(growth["frontier_distance_m"])
            rows.append(("Nearest development", "Nearby" if distance <= 100 else f"About {distance:,.0f} m away"))
        for label_text, value in rows:
            _text(pdf, MARGIN, y, label_text.upper(), font=BOLD, size=7.5, colour=MUTED)
            y = _paragraph(pdf, value, MARGIN, y - 15, content_w, font=BOLD, size=11, max_lines=2) - 8
        supporting = (forecast.get("factors") or {}).get("supporting") or []
        if supporting:
            y -= 6
            _text(pdf, MARGIN, y, "What supports the outlook", font=BOLD, size=12, colour=BRAND_DARK)
            y -= 18
            for item in supporting[:6]:
                y = _paragraph(pdf, f"•  {item}", MARGIN, y, content_w, size=10, max_lines=3) - 3
        y -= 10
        _paragraph(pdf, "This is a location-screening scenario based on analysis of multiple data sources. It is not a guarantee of future development or price growth.", MARGIN, y, content_w, size=8.5, colour=MUTED, max_lines=3)
        _footer(pdf, ctx)
        pdf.showPage()

    # Layout map
    y = _brochure_header(pdf, ctx, "Estate layout")
    map_h = 470
    try:
        _image(pdf, layout_png_for_documents(ctx, 1500, int(1500 * map_h / content_w)), MARGIN, y - map_h, content_w, map_h)
    except ValueError:
        pass
    y -= map_h + 20
    legend = [("Available", BRAND), ("Reserved", (214, 145, 0)), ("Sold / unavailable", (180, 188, 198))]
    lx = MARGIN
    for label, colour in legend:
        _fill(pdf, colour)
        pdf.roundRect(lx, y - 3, 12, 12, 3, fill=1, stroke=0)
        _text(pdf, lx + 18, y, label, size=9.5, colour=INK)
        lx += 32 + pdf.stringWidth(label, REG, 9.5) + 14
    y -= 34
    _stat_boxes(pdf, ctx, MARGIN, y - 54, content_w)
    _footer(pdf, ctx)
    pdf.showPage()

    # Price list (paginated)
    remaining = list(ctx.available_plots)
    first = True
    while first or remaining:
        first = False
        y = _brochure_header(pdf, ctx, "Available plots & prices")
        rows_per_page = 36
        page_ctx_rows = remaining[:rows_per_page]
        remaining = remaining[rows_per_page:]
        clone = MarketingContext(**{**ctx.__dict__, "available_plots": page_ctx_rows})
        _plots_table(pdf, clone, MARGIN, y, content_w, rows=rows_per_page, title=f"{len(ctx.available_plots)} plots available")
        _footer(pdf, ctx)
        pdf.showPage()

    # How to buy + contact
    y = _brochure_header(pdf, ctx, "How to buy")
    steps = [
        ("Choose your plot", "Browse the live map and pick an available plot that suits your budget."),
        ("Inspect the land", "Book a site inspection or visit with your agent before you commit."),
        ("Reserve", "Reserve the plot with your first payment and receive your reservation record."),
        ("Pay and receive documents", "Complete payment as agreed and receive your allocation documents."),
    ]
    for index, (title, body) in enumerate(steps, start=1):
        _fill(pdf, DEEP)
        pdf.circle(MARGIN + 16, y - 6, 15, fill=1, stroke=0)
        _text(pdf, MARGIN + 16, y - 11, str(index), font=BOLD, size=14, colour=(255, 255, 255), align="center")
        _text(pdf, MARGIN + 44, y - 2, title, font=BOLD, size=12.5, colour=INK)
        y = _paragraph(pdf, body, MARGIN + 44, y - 18, content_w - 50, size=10, colour=MUTED, max_lines=2) - 16
    y -= 10
    _fill(pdf, (238, 246, 241))
    pdf.roundRect(MARGIN, y - 190, content_w, 190, 12, fill=1, stroke=0)
    _qr(pdf, ctx.page_url, MARGIN + 20, y - 170, 150)
    _text(pdf, MARGIN + 190, y - 34, "Talk to us", font=BOLD, size=16, colour=BRAND_DARK)
    _contact_block(pdf, ctx, MARGIN + 190, y - 62, content_w - 210)
    _footer(pdf, ctx)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
