from __future__ import annotations

"""Printable marketing PDFs (one-page flyer, multi-page brochure), rendered live from current
availability, in either design family:

* luxury - dark emerald and gold, serif display type;
* promo  - the bright poster look: the estate's logo colour, sky backdrop, price ribbon.

The company logo is placed on every page. The naira sign is drawn from a fallback face because the
brand fonts do not carry it."""

import io
import os
import re
from dataclasses import dataclass

from PIL import Image
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.services.estates import marketing_design as design
from app.services.estates import marketing_promo as promo
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
TINT = (238, 246, 241)
GOLDEN = (255, 200, 70)


@dataclass(frozen=True)
class Theme:
    name: str
    dark: tuple  # fill for cards, table headers, bands
    accent: tuple  # accent lines and headings on light pages
    hi: tuple  # highlight text on dark fills
    heading_font: str
    heading_colour: tuple
    light_header: bool
    tint: tuple


def make_theme(ctx: MarketingContext, style: str) -> Theme:
    if style == "promo":
        accent = promo.accent_from_logo(ctx.logo_bytes)
        return Theme("promo", (40, 46, 52), accent, GOLDEN, XBOLD, INK, True, (243, 247, 250))
    return Theme("luxury", NIGHT, GOLD, GOLD_SOFT, SERIF, NIGHT, False, TINT)


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
    widget.barFillColor = Color(*_c(design.NIGHT))
    drawing = Drawing(size, size)
    drawing.add(widget)
    renderPDF.draw(drawing, pdf, x, y)


def _image(pdf: canvas.Canvas, data: bytes, x: float, y: float, w: float, h: float) -> None:
    pdf.drawImage(ImageReader(io.BytesIO(data)), x, y, width=w, height=h, preserveAspectRatio=True, anchor="c", mask="auto")


def _jpeg(png: bytes, quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _logo(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y_top: float, max_w: float, max_h: float, *, plate: bool, align: str = "left") -> float:
    """Draw the company logo (optionally on a white plate). Returns the width used, 0 when no logo."""
    if not ctx.logo_bytes:
        return 0
    try:
        reader = ImageReader(io.BytesIO(ctx.logo_bytes))
        iw, ih = reader.getSize()
        scale = min(max_w / iw, max_h / ih)
        w, h = iw * scale, ih * scale
        pad = 7 if plate else 0
        left = x if align == "left" else x - w - pad * 2
        if plate:
            pdf.setFillColorRGB(1, 1, 1)
            pdf.roundRect(left, y_top - h - pad * 2, w + pad * 2, h + pad * 2, 6, fill=1, stroke=0)
        pdf.drawImage(reader, left + pad, y_top - h - pad, width=w, height=h, mask="auto")
        return w + pad * 2
    except Exception:
        return 0


def _stat_boxes(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y: float, width: float, theme: Theme) -> None:
    gap = 10
    box_w = (width - gap * 2) / 3
    items = [(str(ctx.available), "Available", (94, 232, 160)), (str(ctx.reserved), "Reserved", (255, 206, 110)), (str(ctx.sold), "Sold", (160, 200, 255))]
    for index, (value, label, colour) in enumerate(items):
        bx = x + index * (box_w + gap)
        _fill(pdf, theme.dark)
        pdf.roundRect(bx, y, box_w, 58, 10, fill=1, stroke=0)
        _text(pdf, bx + box_w / 2, y + 27, value, font=XBOLD, size=24, colour=colour, align="center")
        _text(pdf, bx + box_w / 2, y + 12, label.upper(), font=BOLD, size=7.5, colour=MIST, align="center")


def _heading(pdf: canvas.Canvas, x: float, y: float, text: str, theme: Theme, size: float = 14) -> None:
    _text(pdf, x, y, text, font=theme.heading_font, size=size, colour=theme.heading_colour)
    _fill(pdf, theme.accent)
    pdf.rect(x, y - 6, 34, 2, fill=1, stroke=0)


def _plots_table(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y: float, width: float, rows: int, theme: Theme, *, title: str = "Available plots") -> float:
    _heading(pdf, x, y, title, theme)
    y -= 14
    columns = [("Plot", 0.14), ("Size", 0.22), ("Price", 0.28), ("Details", 0.36)]
    _fill(pdf, theme.dark)
    pdf.roundRect(x, y - 19, width, 19, 4, fill=1, stroke=0)
    cx = x + 9
    for label, share in columns:
        _text(pdf, cx, y - 13, label.upper(), font=BOLD, size=7.5, colour=theme.hi)
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


def _contact_lines(ctx: MarketingContext) -> list[tuple[str, str]]:
    lines = []
    if ctx.agent_name:
        lines.append(("Your agent", ctx.agent_name))
    reach = ctx.contact_phone or (f"+{ctx.whatsapp_digits}" if ctx.whatsapp_digits else None)
    if reach:
        lines.append(("Call / WhatsApp", reach))
    if ctx.contact_email:
        lines.append(("Email", ctx.contact_email))
    lines.append(("Live map & reservations", ctx.page_url.split("?")[0].replace("https://", "")))
    return lines


def _contact_block(pdf: canvas.Canvas, ctx: MarketingContext, x: float, y: float, width: float, theme: Theme, *, step: float = 29) -> None:
    for label, value in _contact_lines(ctx)[:4]:
        _text(pdf, x, y, label.upper(), font=BOLD, size=7, colour=theme.hi)
        size = _fit(value, width, BOLD, 12, 7)
        _text(pdf, x, y - 14, value, font=BOLD, size=size, colour=(255, 255, 255))
        y -= step


def _title_bar(pdf: canvas.Canvas, ctx: MarketingContext, title: str, theme: Theme) -> float:
    if theme.light_header:
        _fill(pdf, theme.tint)
        pdf.rect(0, PAGE_H - 78, PAGE_W, 78, fill=1, stroke=0)
        _fill(pdf, theme.accent)
        pdf.rect(0, PAGE_H - 81, PAGE_W, 3, fill=1, stroke=0)
        _text(pdf, MARGIN, PAGE_H - 48, title, font=XBOLD, size=21, colour=theme.accent)
        used = _logo(pdf, ctx, PAGE_W - MARGIN, PAGE_H - 16, 130, 46, plate=False, align="right")
        if not used:
            _text(pdf, PAGE_W - MARGIN, PAGE_H - 46, ctx.estate.name, font=BOLD, size=10, colour=INK, align="right")
    else:
        _fill(pdf, theme.dark)
        pdf.rect(0, PAGE_H - 78, PAGE_W, 78, fill=1, stroke=0)
        _fill(pdf, theme.accent)
        pdf.rect(MARGIN, PAGE_H - 78, 46, 3, fill=1, stroke=0)
        _text(pdf, MARGIN, PAGE_H - 46, title, font=SERIF, size=21, colour=(255, 255, 255))
        used = _logo(pdf, ctx, PAGE_W - MARGIN, PAGE_H - 14, 120, 40, plate=True, align="right")
        if not used:
            _text(pdf, PAGE_W - MARGIN, PAGE_H - 46, ctx.estate.name, font=MED, size=9.5, colour=MIST, align="right")
    return PAGE_H - 108


def _poster_page(pdf: canvas.Canvas, ctx: MarketingContext) -> None:
    """A full-bleed page from the promo poster, rendered at print resolution."""
    data = _jpeg(promo.estate_promo(ctx, "poster_hd"))
    pdf.drawImage(ImageReader(io.BytesIO(data)), 0, 0, width=PAGE_W, height=PAGE_H)


def render_flyer_pdf(ctx: MarketingContext, style: str = "luxury") -> bytes:
    _fonts()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"{ctx.estate.name} - flyer")
    if style == "promo":
        _poster_page(pdf, ctx)
        pdf.showPage()
        pdf.save()
        return buffer.getvalue()

    theme = make_theme(ctx, "luxury")
    content_w = PAGE_W - MARGIN * 2
    banner_h = 318
    try:
        _image(pdf, design.hero_banner(ctx, 1190, int(1190 * banner_h / PAGE_W)), 0, PAGE_H - banner_h, PAGE_W, banner_h)
    except ValueError:
        _fill(pdf, NIGHT)
        pdf.rect(0, PAGE_H - banner_h, PAGE_W, banner_h, fill=1, stroke=0)
        _text(pdf, MARGIN, PAGE_H - 160, ctx.estate.name, font=SERIF, size=34, colour=(255, 255, 255))

    y = PAGE_H - banner_h - 18
    _stat_boxes(pdf, ctx, MARGIN, y - 58, content_w, theme)
    y -= 58 + 26
    if ctx.min_price and ctx.show_prices:
        _text(pdf, MARGIN, y + 10, "PLOTS FROM", font=BOLD, size=7.5, colour=(15, 110, 68))
        _text(pdf, MARGIN, y - 12, naira_short(ctx.min_price), font=XBOLD, size=21, colour=NIGHT)
        plan = _payment_plan_line(ctx)
        if plan:
            _text(pdf, PAGE_W - MARGIN, y - 4, f"Pay in stages: {plan}", font=MED, size=8.5, colour=MUTED, align="right")
        y -= 40

    band_h = 150
    band_top = 44 + band_h
    rows = max(3, int((y - band_top - 52) // 17))
    y = _plots_table(pdf, ctx, MARGIN, y, content_w, min(rows, 12), theme)

    _fill(pdf, theme.dark)
    pdf.roundRect(MARGIN, 44, content_w, band_h, 14, fill=1, stroke=0)
    _fill(pdf, theme.accent)
    pdf.rect(MARGIN + 138, 44 + band_h - 20, 34, 2.5, fill=1, stroke=0)
    _fill(pdf, (255, 255, 255))
    pdf.roundRect(MARGIN + 16, 44 + (band_h - 104) / 2, 104, 104, 10, fill=1, stroke=0)
    _qr(pdf, ctx.page_url, MARGIN + 22, 44 + (band_h - 92) / 2, 92)
    _text(pdf, MARGIN + 138, 44 + band_h - 38, "Scan for live availability", font=SERIF, size=15, colour=(255, 255, 255))
    _contact_block(pdf, ctx, MARGIN + 138, 44 + band_h - 56, content_w - 158, theme, step=25)
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


def render_brochure_pdf(ctx: MarketingContext, style: str = "luxury") -> bytes:
    _fonts()
    theme = make_theme(ctx, "promo" if style == "promo" else "luxury")
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"{ctx.estate.name} - brochure")
    content_w = PAGE_W - MARGIN * 2

    # Cover
    if theme.name == "promo":
        _poster_page(pdf, ctx)
    else:
        try:
            _image(pdf, design.cover_page(ctx, 1190, int(1190 * PAGE_H / PAGE_W)), 0, 0, PAGE_W, PAGE_H)
        except ValueError:
            _fill(pdf, NIGHT)
            pdf.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
            _text(pdf, MARGIN, PAGE_H / 2, ctx.estate.name, font=SERIF, size=38, colour=(255, 255, 255))
    pdf.showPage()

    # About
    y = _title_bar(pdf, ctx, "About the estate", theme)
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
        _fill(pdf, theme.tint)
        pdf.roundRect(MARGIN, y - 32, content_w, 36, 9, fill=1, stroke=0)
        _text(pdf, MARGIN + 16, y - 18, label.upper(), font=BOLD, size=8, colour=MUTED)
        _text(pdf, PAGE_W - MARGIN - 16, y - 19, value, font=BOLD, size=11.5, colour=INK, align="right")
        y -= 44
    if ctx.payment_plan:
        y -= 8
        _heading(pdf, MARGIN, y, "Payment plan", theme)
        y -= 26
        gap = 10
        n = len(ctx.payment_plan)
        box_w = (content_w - gap * (n - 1)) / n
        for index, item in enumerate(ctx.payment_plan):
            bx = MARGIN + index * (box_w + gap)
            _fill(pdf, theme.dark)
            pdf.roundRect(bx, y - 52, box_w, 56, 10, fill=1, stroke=0)
            _text(pdf, bx + box_w / 2, y - 24, f"{item.get('percentage')}%", font=XBOLD, size=19, colour=theme.hi, align="center")
            _text(pdf, bx + box_w / 2, y - 42, str(item.get("label"))[:22], font=MED, size=8.5, colour=MIST, align="center")
        y -= 72
    _footer(pdf, ctx)
    pdf.showPage()

    # Area outlook (only when published)
    if ctx.forecast and ctx.forecast.get("data_available"):
        forecast = ctx.forecast
        growth = forecast.get("growth") or {}
        y = _title_bar(pdf, ctx, "Area outlook", theme)
        label = _potential_label(forecast)
        _fill(pdf, theme.tint)
        pdf.roundRect(MARGIN, y - 72, content_w, 78, 12, fill=1, stroke=0)
        _text(pdf, MARGIN + 18, y - 26, "LAND VALUE POTENTIAL", font=BOLD, size=8, colour=MUTED)
        _text(pdf, MARGIN + 18, y - 56, label, font=theme.heading_font, size=25, colour=theme.accent if theme.light_header else theme.heading_colour)
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
            _heading(pdf, MARGIN, y, "What supports the outlook", theme, 13)
            y -= 20
            for item in supporting[:6]:
                y = _paragraph(pdf, f"•  {item}", MARGIN, y, content_w, size=10, max_lines=3) - 3
        y -= 10
        _paragraph(pdf, "This is a location-screening scenario based on analysis of multiple data sources. It is not a guarantee of future development or price growth.", MARGIN, y, content_w, size=8.5, colour=MUTED, max_lines=3)
        _footer(pdf, ctx)
        pdf.showPage()

    # Layout map - satellite
    y = _title_bar(pdf, ctx, "Estate layout", theme)
    map_h = 470
    try:
        _image(pdf, design.document_map(ctx, 1500, int(1500 * map_h / content_w)), MARGIN, y - map_h, content_w, map_h)
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
    _stat_boxes(pdf, ctx, MARGIN, y - 58, content_w, theme)
    _footer(pdf, ctx)
    pdf.showPage()

    # Price list (paginated, capped)
    remaining = list(ctx.available_plots)
    first = True
    pages = 0
    while (first or remaining) and pages < 3:
        first = False
        pages += 1
        y = _title_bar(pdf, ctx, "Available plots & prices", theme)
        rows_per_page = 36
        page_rows = remaining[:rows_per_page]
        remaining = remaining[rows_per_page:]
        clone = MarketingContext(**{**ctx.__dict__, "available_plots": page_rows})
        y = _plots_table(pdf, clone, MARGIN, y, content_w, rows_per_page, theme, title=f"{len(ctx.available_plots)} plots available")
        if remaining and pages == 3:
            _text(pdf, MARGIN + 9, y - 10, f"+ {len(remaining)} more plots - scan the code on the last page for the full live list", font=MED, size=9, colour=MUTED)
        _footer(pdf, ctx)
        pdf.showPage()

    # How to buy + contact
    y = _title_bar(pdf, ctx, "How to buy", theme)
    steps = [
        ("Choose your plot", "Browse the live map and pick an available plot that suits your budget."),
        ("Inspect the land", "Book a site inspection or visit with your agent before you commit."),
        ("Reserve", "Reserve the plot with your first payment and receive your reservation record."),
        ("Pay and receive documents", "Complete payment as agreed and receive your allocation documents."),
    ]
    for index, (title, body) in enumerate(steps, start=1):
        _fill(pdf, theme.dark)
        pdf.circle(MARGIN + 16, y - 6, 16, fill=1, stroke=0)
        _text(pdf, MARGIN + 16, y - 11, str(index), font=XBOLD if theme.light_header else SERIF, size=14, colour=theme.hi, align="center")
        _text(pdf, MARGIN + 46, y - 2, title, font=BOLD, size=12.5, colour=INK)
        y = _paragraph(pdf, body, MARGIN + 46, y - 18, content_w - 52, size=10, colour=MUTED, max_lines=2) - 16
    y -= 10
    _fill(pdf, theme.dark)
    pdf.roundRect(MARGIN, y - 196, content_w, 196, 16, fill=1, stroke=0)
    _fill(pdf, theme.accent if theme.name == "luxury" else GOLDEN)
    pdf.rect(MARGIN + 22, y - 30, 36, 2.5, fill=1, stroke=0)
    _fill(pdf, (255, 255, 255))
    pdf.roundRect(MARGIN + 20, y - 176, 156, 156, 12, fill=1, stroke=0)
    _qr(pdf, ctx.page_url, MARGIN + 30, y - 166, 136)
    _text(pdf, MARGIN + 200, y - 52, "Talk to us", font=SERIF if theme.name == "luxury" else XBOLD, size=19, colour=(255, 255, 255))
    _contact_block(pdf, ctx, MARGIN + 200, y - 78, content_w - 222, theme, step=27)
    _footer(pdf, ctx)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
