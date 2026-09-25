from __future__ import annotations

"""Printable marketing PDFs (one-page flyer, multi-page brochure), rendered live from current
availability. Same visual language as the social ads: satellite imagery, serif display type, gold
accents. The naira sign is drawn from a fallback face because the brand fonts do not carry it."""

import io
import os
import re

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.services.estates import marketing_design as design
from app.services.estates.marketing_render import MarketingContext, area_text, naira, naira_short

_FONTS_READY = False
REG, MED, BOLD, XBOLD, SERIF, NAIRA = "LCSans", "LCSans-Medium", "LCSans-Bold", "LCSans-ExtraBold", "LCSerif", "LCNaira"
PAGE_W, PAGE_H = A4
MARGIN = 40

NIGHT = design.NIGHT
GOLD = design.GOLD
GOLD_SOFT = design.GOLD_SOFT
IVORY = design.IVORY
MIST = design.MIST
INK = (16, 28, 22)
MUTED = (104, 118, 110)
BRAND = (26, 143, 90)
BRAND_DARK = (15, 110, 68)
TINT = (238, 246, 241)


def _fonts() -> None:
    global _FONTS_READY
    if _FONTS_READY:
        return
    def path(name: str, fallback: str) -> str:
        candidate = os.path.join(design.FONT_DIR, name)
        return candidate if os.path.exists(candidate) else fallback
    pdfmetrics.registerFont(TTFont(REG, path("Manrope-Regular.ttf", design._DEJAVU)))
    pdfmetrics.registerFont(TTFont(MED, path("Manrope-Medium.ttf", design._DEJAVU)))
    pdfmetrics.registerFont(TTFont(BOLD, path("Manrope-Bold.ttf", design._DEJAVU_BOLD)))
    pdfmetrics.registerFont(TTFont(XBOLD, path("Manrope-ExtraBold.ttf", design._DEJAVU_BOLD)))
    pdfmetrics.registerFont(TTFont(SERIF, path("PlayfairDisplay-Bold.ttf", design._DEJAVU_BOLD)))
    pdfmetrics.registerFont(TTFont(NAIRA, design._DEJAVU_BOLD))
    _FONTS_READY = True


def _c(rgb: tuple) -> tuple[float, float, float]:
    return (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)


def _fill(pdf: canvas.Canvas, rgb: tuple) -> None:
    pdf.setFillColorRGB(*_c(rgb))


def _runs(value: str) -> list[tuple[str, bool]]:
    return [(chunk, chunk == "₦") for chunk in re.split("(₦)", str(value)) if chunk]


def _width(value: str, font: str, size: float) -> float:
    return sum(pdfmetrics.stringWidth(chunk, NAIRA if is_naira else font, size * (0.92 if is_naira else 1)) for chunk, is_naira in _runs(value))


def _text(pdf: canvas.Canvas, x: float, y: float, value: str, *, font: str = REG, size: float = 10, colour: tuple = INK, align: str = "left") -> None:
    total = _width(value, font, size)
    cursor = x if align == "left" else x - total / 2 if align == "center" else x - total
    _fill(pdf, colour)
    for chunk, is_naira in _runs(value):
        face = NAIRA if is_naira else font
        face_size = size * (0.92 if is_naira else 1)
        pdf.setFont(face, face_size)
        pdf.drawString(cursor, y, chunk)
        cursor += pdfmetrics.stringWidth(chunk, face, face_size)


def _fit(value: str, width: float, font: str, start: float, minimum: float) -> float:
    size = start
    while size > minimum and _width(value, font, size) > width:
        size -= 0.5
    return size


def _paragraph(pdf: canvas.Canvas, value: str, x: float, y: float, width: float, *, font: str = REG, size: float = 10.5, leading: float | None = None, colour: tuple = INK, max_lines: int = 40) -> float:
    leading = leading or size * 1.5
    for line in simpleSplit(str(value or ""), font, size, width)[:max_lines]:
        _text(pdf, x, y, line, font=font, size=size, colour=colour)
        y -= leading
    return y


def _qr(pdf: canvas.Canvas, url: str, x: float, y: float, size: float) -> None:
    widget = qr.QrCodeWidget(url)
    widget.barWidth = size
    widget.barHeight = size
    widget.x = 0
    widget.y = 0
    widget.barFillColor = _rl(design.NIGHT)
    drawing = Drawing(size, size)
    drawing.add(widget)
    renderPDF.draw(drawing, pdf, x, y)


def _rl(rgb: tuple):
    from reportlab.lib.colors import Color

    return Color(*_c(rgb))


def _image(pdf: canvas.Canvas, data: bytes, x: float, y: float, w: float, h: float) -> None:
    pdf.drawImage(ImageReader(io.BytesIO(data)), x, y, width=w, height=h, preserveAspectRatio=True, anchor="c", mask="auto")


def _stat_boxes(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y: float, width: float) -> None:
    gap = 10
    box_w = (width - gap * 2) / 3
    items = [(str(ctx.available), "Available", (94, 232, 160)), (str(ctx.reserved), "Reserved", (255, 206, 110)), (str(ctx.sold), "Sold", (160, 200, 255))]
    for index, (value, label, colour) in enumerate(items):
        bx = x + index * (box_w + gap)
        _fill(pdf, NIGHT)
        pdf.roundRect(bx, y, box_w, 58, 10, fill=1, stroke=0)
        _text(pdf, bx + box_w / 2, y + 27, value, font=XBOLD, size=24, colour=colour, align="center")
        _text(pdf, bx + box_w / 2, y + 12, label.upper(), font=BOLD, size=7.5, colour=MIST, align="center")


def _plots_table(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y: float, width: float, rows: int, *, title: str = "Available plots") -> float:
    _text(pdf, x, y, title, font=SERIF, size=14, colour=NIGHT)
    _fill(pdf, GOLD)
    pdf.rect(x, y - 6, 34, 2, fill=1, stroke=0)
    y -= 14
    columns = [("Plot", 0.14), ("Size", 0.22), ("Price", 0.28), ("Details", 0.36)]
    _fill(pdf, NIGHT)
    pdf.roundRect(x, y - 19, width, 19, 4, fill=1, stroke=0)
    cx = x + 9
    for label, share in columns:
        _text(pdf, cx, y - 13, label.upper(), font=BOLD, size=7.5, colour=GOLD_SOFT)
        cx += width * share
    y -= 19
    shown = ctx.available_plots[:rows]
    for index, plot in enumerate(shown):
        if index % 2 == 0:
            _fill(pdf, (245, 249, 246))
            pdf.rect(x, y - 17, width, 17, fill=1, stroke=0)
        price = naira(plot.asking_price) if (ctx.show_prices and plot.asking_price is not None) else "On request"
        cx = x + 9
        cells = [str(plot.plot_number), area_text(plot.area_sqm), price, (plot.public_address or "")[:34]]
        for position, (cell, (_label, share)) in enumerate(zip(cells, columns)):
            _text(pdf, cx, y - 12, cell, font=BOLD if position in (0, 2) else REG, size=9, colour=INK)
            cx += width * share
        y -= 17
    remaining = len(ctx.available_plots) - len(shown)
    if remaining > 0:
        _text(pdf, x + 9, y - 13, f"+ {remaining} more available - scan the code for the full live list", font=MED, size=8.5, colour=MUTED)
        y -= 21
    elif not shown:
        _text(pdf, x + 9, y - 13, "No plots are currently available.", size=9, colour=MUTED)
        y -= 21
    return y


def _payment_plan_line(ctx: MarketingContext) -> str | None:
    if not ctx.payment_plan:
        return None
    return "  ·  ".join(f"{item.get('percentage')}% {item.get('label')}" for item in ctx.payment_plan)


def _footer(pdf: canvas.Canvas, ctx: MarketingContext) -> None:
    _text(pdf, PAGE_W / 2, 22, f"Availability as of {ctx.stamp}. Prices and availability are subject to change.   Powered by LandCheck Estates", font=MED, size=7, colour=MUTED, align="center")


def _contact_block(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y: float, width: float, *, on_dark: bool = False) -> None:
    lines = []
    if ctx.agent_name:
        lines.append(("Your agent", ctx.agent_name))
    if ctx.contact_phone:
        lines.append(("Call / WhatsApp", ctx.contact_phone))
    lines.append(("Live map & reservations", ctx.page_url.split("?")[0].replace("https://", "")))
    for label, value in lines:
        _text(pdf, x, y, label.upper(), font=BOLD, size=7, colour=GOLD if on_dark else MUTED)
        size = _fit(value, width, BOLD, 12, 7)
        _text(pdf, x, y - 15, value, font=BOLD, size=size, colour=IVORY if on_dark else INK)
        y -= 29


def _title_bar(pdf: canvas.Canvas, ctx: MarketingContext, title: str) -> float:
    _fill(pdf, NIGHT)
    pdf.rect(0, PAGE_H - 78, PAGE_W, 78, fill=1, stroke=0)
    _fill(pdf, GOLD)
    pdf.rect(MARGIN, PAGE_H - 78, 46, 3, fill=1, stroke=0)
    _text(pdf, MARGIN, PAGE_H - 46, title, font=SERIF, size=21, colour=(255, 255, 255))
    _text(pdf, PAGE_W - MARGIN, PAGE_H - 46, ctx.estate.name, font=MED, size=9.5, colour=MIST, align="right")
    return PAGE_H - 108


def render_flyer_pdf(ctx: MarketingContext) -> bytes:
    _fonts()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"{ctx.estate.name} - flyer")
    content_w = PAGE_W - MARGIN * 2

    banner_h = 318
    try:
        _image(pdf, design.hero_banner(ctx, 1190, int(1190 * banner_h / PAGE_W)), 0, PAGE_H - banner_h, PAGE_W, banner_h)
    except ValueError:
        _fill(pdf, NIGHT)
        pdf.rect(0, PAGE_H - banner_h, PAGE_W, banner_h, fill=1, stroke=0)
        _text(pdf, MARGIN, PAGE_H - 160, ctx.estate.name, font=SERIF, size=34, colour=(255, 255, 255))

    y = PAGE_H - banner_h - 18
    _stat_boxes(pdf, ctx, MARGIN, y - 58, content_w)
    y -= 58 + 26

    if ctx.min_price and ctx.show_prices:
        _text(pdf, MARGIN, y + 10, "PLOTS FROM", font=BOLD, size=7.5, colour=BRAND_DARK)
        _text(pdf, MARGIN, y - 12, naira_short(ctx.min_price), font=XBOLD, size=21, colour=NIGHT)
        plan = _payment_plan_line(ctx)
        if plan:
            _text(pdf, PAGE_W - MARGIN, y - 4, f"Pay in stages: {plan}", font=MED, size=8.5, colour=MUTED, align="right")
        y -= 40

    band_h = 138
    band_top = 44 + band_h
    rows = max(3, int((y - band_top - 52) // 17))
    y = _plots_table(pdf, ctx, MARGIN, y, content_w, rows=min(rows, 12))

    _fill(pdf, NIGHT)
    pdf.roundRect(MARGIN, 44, content_w, band_h, 14, fill=1, stroke=0)
    _fill(pdf, GOLD)
    pdf.rect(MARGIN + 18, 44 + band_h - 22, 34, 2.5, fill=1, stroke=0)
    _fill(pdf, (255, 255, 255))
    pdf.roundRect(MARGIN + 16, 44 + (band_h - 104) / 2, 104, 104, 10, fill=1, stroke=0)
    _qr(pdf, ctx.page_url, MARGIN + 22, 44 + (band_h - 92) / 2, 92)
    _text(pdf, MARGIN + 138, 44 + band_h - 42, "Scan for live availability", font=SERIF, size=15, colour=(255, 255, 255))
    _contact_block(pdf, ctx, MARGIN + 138, 44 + band_h - 58, content_w - 158, on_dark=True)
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


def render_brochure_pdf(ctx: MarketingContext) -> bytes:
    _fonts()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"{ctx.estate.name} - brochure")
    content_w = PAGE_W - MARGIN * 2

    # Cover - full-bleed satellite
    try:
        _image(pdf, design.cover_page(ctx, 1190, int(1190 * PAGE_H / PAGE_W)), 0, 0, PAGE_W, PAGE_H)
    except ValueError:
        _fill(pdf, NIGHT)
        pdf.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
        _text(pdf, MARGIN, PAGE_H / 2, ctx.estate.name, font=SERIF, size=38, colour=(255, 255, 255))
    pdf.showPage()

    # About
    y = _title_bar(pdf, ctx, "About the estate")
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
        _fill(pdf, TINT)
        pdf.roundRect(MARGIN, y - 32, content_w, 36, 9, fill=1, stroke=0)
        _text(pdf, MARGIN + 16, y - 18, label.upper(), font=BOLD, size=8, colour=MUTED)
        _text(pdf, PAGE_W - MARGIN - 16, y - 19, value, font=BOLD, size=11.5, colour=INK, align="right")
        y -= 44
    if ctx.payment_plan:
        y -= 8
        _text(pdf, MARGIN, y, "Payment plan", font=SERIF, size=14, colour=NIGHT)
        _fill(pdf, GOLD)
        pdf.rect(MARGIN, y - 6, 34, 2, fill=1, stroke=0)
        y -= 26
        gap = 10
        n = len(ctx.payment_plan)
        box_w = (content_w - gap * (n - 1)) / n
        for index, item in enumerate(ctx.payment_plan):
            bx = MARGIN + index * (box_w + gap)
            _fill(pdf, NIGHT)
            pdf.roundRect(bx, y - 52, box_w, 56, 10, fill=1, stroke=0)
            _text(pdf, bx + box_w / 2, y - 24, f"{item.get('percentage')}%", font=XBOLD, size=19, colour=GOLD_SOFT, align="center")
            _text(pdf, bx + box_w / 2, y - 42, str(item.get("label"))[:22], font=MED, size=8.5, colour=MIST, align="center")
        y -= 72
    _footer(pdf, ctx)
    pdf.showPage()

    # Area outlook (only when published)
    if ctx.forecast and ctx.forecast.get("data_available"):
        forecast = ctx.forecast
        growth = forecast.get("growth") or {}
        y = _title_bar(pdf, ctx, "Area outlook")
        label = _potential_label(forecast)
        _fill(pdf, TINT)
        pdf.roundRect(MARGIN, y - 72, content_w, 78, 12, fill=1, stroke=0)
        _text(pdf, MARGIN + 18, y - 26, "LAND VALUE POTENTIAL", font=BOLD, size=8, colour=MUTED)
        _text(pdf, MARGIN + 18, y - 56, label, font=SERIF, size=25, colour=NIGHT)
        headline = str((forecast.get("reach_estimate") or {}).get("headline") or "").replace("; this analysis does not treat that as a promise of future development", "")
        y = _paragraph(pdf, headline, MARGIN + 160, y - 26, content_w - 178, size=10, max_lines=4)
        y = min(y, PAGE_H - 108 - 78) - 20
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
            _text(pdf, MARGIN, y, "What supports the outlook", font=SERIF, size=13, colour=NIGHT)
            y -= 20
            for item in supporting[:6]:
                y = _paragraph(pdf, f"•  {item}", MARGIN, y, content_w, size=10, max_lines=3) - 3
        y -= 10
        _paragraph(pdf, "This is a location-screening scenario based on analysis of multiple data sources. It is not a guarantee of future development or price growth.", MARGIN, y, content_w, size=8.5, colour=MUTED, max_lines=3)
        _footer(pdf, ctx)
        pdf.showPage()

    # Layout map - satellite
    y = _title_bar(pdf, ctx, "Estate layout")
    map_h = 470
    try:
        map_bytes = design.document_map(ctx, 1500, int(1500 * map_h / content_w))
        _image(pdf, map_bytes, MARGIN, y - map_h, content_w, map_h)
    except ValueError:
        pass
    y -= map_h + 22
    legend = [("Available", (34, 214, 128)), ("Reserved", (255, 186, 60)), ("Sold / unavailable", (40, 52, 62))]
    lx = MARGIN
    for label, colour in legend:
        _fill(pdf, colour)
        pdf.roundRect(lx, y - 3, 12, 12, 3, fill=1, stroke=0)
        _text(pdf, lx + 18, y, label, font=MED, size=9.5, colour=INK)
        lx += 34 + _width(label, MED, 9.5) + 14
    y -= 36
    _stat_boxes(pdf, ctx, MARGIN, y - 58, content_w)
    _footer(pdf, ctx)
    pdf.showPage()

    # Price list (paginated)
    remaining = list(ctx.available_plots)
    first = True
    pages = 0
    while (first or remaining) and pages < 3:
        first = False
        pages += 1
        y = _title_bar(pdf, ctx, "Available plots & prices")
        rows_per_page = 36
        page_rows = remaining[:rows_per_page]
        remaining = remaining[rows_per_page:]
        clone = MarketingContext(**{**ctx.__dict__, "available_plots": page_rows})
        y = _plots_table(pdf, clone, MARGIN, y, content_w, rows=rows_per_page, title=f"{len(ctx.available_plots)} plots available")
        if remaining and pages == 3:
            _text(pdf, MARGIN + 9, y - 10, f"+ {len(remaining)} more plots - scan the code on the last page for the full live list", font=MED, size=9, colour=MUTED)
        _footer(pdf, ctx)
        pdf.showPage()

    # How to buy + contact
    y = _title_bar(pdf, ctx, "How to buy")
    steps = [
        ("Choose your plot", "Browse the live map and pick an available plot that suits your budget."),
        ("Inspect the land", "Book a site inspection or visit with your agent before you commit."),
        ("Reserve", "Reserve the plot with your first payment and receive your reservation record."),
        ("Pay and receive documents", "Complete payment as agreed and receive your allocation documents."),
    ]
    for index, (title, body) in enumerate(steps, start=1):
        _fill(pdf, NIGHT)
        pdf.circle(MARGIN + 16, y - 6, 16, fill=1, stroke=0)
        _text(pdf, MARGIN + 16, y - 11, str(index), font=SERIF, size=14, colour=GOLD_SOFT, align="center")
        _text(pdf, MARGIN + 46, y - 2, title, font=BOLD, size=12.5, colour=INK)
        y = _paragraph(pdf, body, MARGIN + 46, y - 18, content_w - 52, size=10, colour=MUTED, max_lines=2) - 16
    y -= 10
    _fill(pdf, NIGHT)
    pdf.roundRect(MARGIN, y - 196, content_w, 196, 16, fill=1, stroke=0)
    _fill(pdf, GOLD)
    pdf.rect(MARGIN + 22, y - 30, 36, 2.5, fill=1, stroke=0)
    _fill(pdf, (255, 255, 255))
    pdf.roundRect(MARGIN + 20, y - 176, 156, 156, 12, fill=1, stroke=0)
    _qr(pdf, ctx.page_url, MARGIN + 30, y - 166, 136)
    _text(pdf, MARGIN + 200, y - 52, "Talk to us", font=SERIF, size=19, colour=(255, 255, 255))
    _contact_block(pdf, ctx, MARGIN + 200, y - 78, content_w - 222, on_dark=True)
    _footer(pdf, ctx)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
