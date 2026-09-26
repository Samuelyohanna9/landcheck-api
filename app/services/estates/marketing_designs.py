from __future__ import annotations

"""Three more premium flyer / ad design families, built from the estate's own live data:

* heritage  - warm ivory paper, serif headline, a framed satellite "photograph", a refined price schedule;
* bold      - the company's own logo colour as a full-bleed field, oversized price, sticker badge;
* blueprint - a deep-navy survey drawing: grid, coordinates, dimension lines, a specification table.

All of them work for the whole estate and for a single plot, in the four ad shapes (status 9:16, feed 4:5,
A4 poster, landscape link card). Every claim comes from data (plots, prices, payment plan, contact)."""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from app.services.estates import marketing_design as dz
from app.services.estates import marketing_promo as promo
from app.services.estates import marketing_render as mr

DESIGN_NAMES = ("heritage", "bold", "blueprint")

# Heritage palette
PAPER = (247, 242, 232)
PAPER_DEEP = (238, 230, 214)
NAVY = (20, 33, 61)
NAVY_SOFT = (78, 90, 116)
BRASS = (170, 132, 56)
BRASS_SOFT = (208, 176, 108)
# Blueprint palette
BP_TOP = (7, 22, 44)
BP_BOTTOM = (11, 39, 72)
CYAN = (92, 200, 255)
CYAN_SOFT = (150, 220, 255)
AMBER = (255, 190, 80)
SLATE = (118, 134, 158)
WHITE = (255, 255, 255)
YELLOW = (255, 214, 64)


# ── Spec ─────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Spec:
    ctx: "mr.MarketingContext"
    title: str
    kicker: str
    tagline: str
    location: str | None
    badge: str
    price_value: str | None
    price_caption: str | None
    cards: list[tuple[str, str, str]]
    facts: list[tuple[str, str]]
    hero: Callable[[int, int, tuple[float, float, float, float]], tuple[Image.Image, bool]]
    qr_url: str | None
    accent: tuple[int, int, int]
    focus: Any = None
    for_plot: bool = False


def _sizes_range(ctx: "mr.MarketingContext") -> str | None:
    areas = []
    for plot in ctx.available_plots:
        try:
            areas.append(float(plot.area_sqm))
        except Exception:
            continue
    if not areas:
        return None
    low, high = int(round(min(areas))), int(round(max(areas)))
    return f"{low:,} sqm" if low == high else f"{low:,} - {high:,} sqm"


def _plan_text(ctx: "mr.MarketingContext") -> str | None:
    if not ctx.payment_plan:
        return None
    parts = [f"{item.get('percentage')}% {str(item.get('label') or '').lower()}" for item in ctx.payment_plan if item.get("percentage")]
    return " · ".join(parts) or None


def _city(ctx: "mr.MarketingContext") -> str | None:
    if not ctx.location:
        return None
    first = re.split(r"[,/]", str(ctx.location))[0].strip()
    return first if 2 < len(first) <= 32 else None


def estate_spec(ctx: "mr.MarketingContext", *, qr: bool) -> Spec:
    value, caption = promo._deposit(ctx)
    if not ctx.available:
        value, caption = "Sold out", "join the waiting list"
    facts: list[tuple[str, str]] = [("Plots available", str(ctx.available))]
    sizes = _sizes_range(ctx)
    if sizes:
        facts.append(("Plot sizes", sizes))
    if ctx.show_prices and ctx.min_price:
        facts.append(("Prices from", mr.naira(ctx.min_price)))
    plan = _plan_text(ctx)
    facts.append(("Payment plan", plan or "Ask our team"))
    if ctx.location:
        facts.append(("Location", ctx.location))
    city = _city(ctx)
    return Spec(
        ctx=ctx, title=ctx.estate.name, kicker="An estate opportunity" if ctx.available else "Estate",
        tagline=(ctx.estate.public_tagline or (f"Premium plots in {city}" if city else "Land you can see, visit and own"))[:90], location=ctx.location,
        badge=f"{ctx.available} plot{'s' if ctx.available != 1 else ''} available now" if ctx.available else "Fully sold",
        price_value=value, price_caption=caption, cards=promo._size_cards(ctx) if ctx.available else [], facts=facts,
        hero=lambda w, h, rect: dz.render_map(ctx, w, h, rect=rect, labels=False), qr_url=ctx.page_url if qr else None, accent=promo.accent_from_logo(ctx.logo_bytes),
    )


def plot_spec(ctx: "mr.MarketingContext", plot: Any, *, qr: bool) -> Spec:
    status = plot.commercial_status
    price = mr.naira(plot.asking_price) if (ctx.show_prices and plot.asking_price is not None) else None
    label = mr.STATUS_LABEL.get(status, "Not available")
    size = mr.area_text(plot.area_sqm).replace("m²", "sqm") if plot.area_sqm else None
    cards = [(size or "Size on request", price or "On request", "Plot size & price")]
    facts = [("Plot", str(plot.plot_number))]
    if size:
        facts.append(("Size", size))
    facts.append(("Price", price or "On request"))
    deposit_value, deposit_caption = promo._deposit(ctx)
    if deposit_caption == "initial deposit" and plot.asking_price is not None and ctx.payment_plan:
        try:
            pct = float(ctx.payment_plan[0].get("percentage"))
            amount = mr.naira(float(plot.asking_price) * pct / 100)
            cards.append(("Initial deposit", amount, f"{pct:g}% to secure it"))
            facts.append(("Initial deposit", amount))
        except (TypeError, ValueError):
            pass
    facts.append(("Status", label))
    return Spec(
        ctx=ctx, title=f"Plot {plot.plot_number}", kicker=f"At {ctx.estate.name}", tagline=ctx.estate.public_tagline or (f"In {_city(ctx)}" if _city(ctx) else "A plot you can see and visit"),
        location=ctx.location, badge="Available now" if status == "available" else label,
        price_value=mr.naira_short(plot.asking_price) if price else label, price_caption="asking price" if price else None, cards=cards, facts=facts,
        hero=lambda w, h, rect: dz.render_map(ctx, w, h, rect=rect, focus=plot), qr_url=ctx.plot_url(plot) if qr else None,
        accent=promo.accent_from_logo(ctx.logo_bytes), focus=plot, for_plot=True,
    )


# ── Drawing helpers ──────────────────────────────────────────────────────────────────────────
def _mix(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _noise(canvas: Image.Image, amount: int = 9) -> None:
    grain = Image.effect_noise(canvas.size, 40).convert("RGB")
    canvas.paste(Image.blend(canvas, ImageChops.multiply(canvas, ImageChops.lighter(grain, Image.new("RGB", canvas.size, (200, 200, 200)))), amount / 100))


def _shadow(canvas: Image.Image, box: tuple[int, int, int, int], *, radius: int = 10, blur: int = 24, alpha: int = 90, dy: int = 10, colour: tuple = (10, 16, 30)) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([box[0], box[1] + dy, box[2], box[3] + dy], radius=radius, fill=colour + (alpha,))
    dz.over(canvas, layer.filter(ImageFilter.GaussianBlur(blur)))


def _paste_round(canvas: Image.Image, image: Image.Image, box: tuple[int, int, int, int], radius: int) -> None:
    w, h = box[2] - box[0], box[3] - box[1]
    canvas.paste(image.resize((w, h), Image.LANCZOS) if image.size != (w, h) else image, (box[0], box[1]), dz.rounded_mask((w, h), radius))


def _qr(canvas: Image.Image, url: str, x: int, y: int, size: int, *, dark: tuple, light: tuple = WHITE, pad: int = 0, plate: tuple | None = None, radius: int = 0) -> int:
    code = mr.qr_image(url, size, dark=dark, light=light).convert("RGB")
    if plate is not None:
        ImageDraw.Draw(canvas).rounded_rectangle([x - pad, y - pad, x + code.width + pad, y + code.height + pad], radius=radius, fill=plate)
    canvas.paste(code, (x, y))
    return code.width


def _contact_lines(ctx: "mr.MarketingContext") -> list[str]:
    reach = ctx.contact_phone or (f"+{ctx.whatsapp_digits}" if ctx.whatsapp_digits else None)
    lines = [item for item in (ctx.agent_name, reach, ctx.contact_email) if item]
    return lines[:3]


def _imagery_credit(canvas: Image.Image, has_imagery: bool, x: int, y: int, colour: tuple, size: int = 12, align: str = "r") -> None:
    if has_imagery:
        dz.T(ImageDraw.Draw(canvas, "RGBA"), x, y, "Imagery © Mapbox © OpenStreetMap © Maxar", dz.sans(size, "medium"), colour, align=align)


def _tile_cover(hero: Callable, w: int, h: int) -> tuple[Image.Image, bool]:
    return hero(w, h, (w * 0.04, h * 0.05, w * 0.96, h * 0.95))


def _dots(d: ImageDraw.ImageDraw, x0: float, x1: float, y: float, colour: tuple, gap: float = 9) -> None:
    x = x0
    while x < x1:
        d.ellipse([x, y - 1.5, x + 3, y + 1.5], fill=colour)
        x += gap


def _sun_meta(spec: Spec) -> str:
    return datetime.now(timezone.utc).strftime("%d %b %Y").upper()


def _centre(spec: Spec) -> tuple[float, float]:
    try:
        b = dz.plot_focus_bounds(spec.focus) if spec.focus is not None else dz.estate_bounds(spec.ctx)
        return (b[1] + b[3]) / 2, (b[0] + b[2]) / 2
    except Exception:
        return 0.0, 0.0


def _span_metres(spec: Spec) -> tuple[int, int]:
    try:
        b = dz.plot_focus_bounds(spec.focus) if spec.focus is not None else dz.estate_bounds(spec.ctx)
        lat = (b[1] + b[3]) / 2
        return int((b[2] - b[0]) * 111320 * math.cos(math.radians(lat))), int((b[3] - b[1]) * 110540)
    except Exception:
        return 0, 0


def _fmt_deg(value: float, positive: str, negative: str) -> str:
    return f"{abs(value):.4f}° {positive if value >= 0 else negative}"


def _wrap_title(d: ImageDraw.ImageDraw, text: str, width: float, start: int, minimum: int, maker: Callable, lines: int = 2) -> tuple[list[str], Any]:
    """The largest size at which the text fits `lines` lines."""
    size = start
    while size >= minimum:
        fnt = maker(size)
        wrapped = dz.wrap(d, text, fnt, width, lines)
        if not any(line.endswith("...") for line in wrapped) and all(dz.measure(d, line, fnt) <= width for line in wrapped):
            return wrapped, fnt
        size -= 4
    fnt = maker(minimum)
    return dz.wrap(d, text, fnt, width, lines), fnt


# ════════════════════════════════════════════════════════════════════════════════════════════
# HERITAGE
# ════════════════════════════════════════════════════════════════════════════════════════════
def _heritage_vertical(spec: Spec, W: int, H: int) -> bytes:
    ctx = spec.ctx
    su = W / 1080
    tall = H / W
    k = 0.86 if tall < 1.3 else 0.94 if tall < 1.5 else 1.0
    s = su * k
    canvas = Image.new("RGB", (W, H), PAPER)
    canvas.paste(dz.gradient(W, H, PAPER, PAPER_DEEP), (0, 0))
    _noise(canvas, 7)
    d = ImageDraw.Draw(canvas, "RGBA")
    m = int(66 * su)
    d.rectangle([int(24 * su), int(24 * su), W - int(24 * su), H - int(24 * su)], outline=NAVY + (255,), width=max(2, int(2 * su)))
    d.rectangle([int(34 * su), int(34 * su), W - int(34 * su), H - int(34 * su)], outline=BRASS + (255,), width=max(1, int(1.5 * su)))

    y = int(70 * s)
    # centred brand: logo plate (or company name in spaced capitals)
    if ctx.logo_bytes:
        from PIL import Image as _I
        import io as _io
        try:
            logo = _I.open(_io.BytesIO(ctx.logo_bytes)).convert("RGBA")
            logo.thumbnail((int(360 * s), int(78 * s)), _I.LANCZOS)
            plate = _I.new("RGBA", (logo.width + int(40 * s), logo.height + int(24 * s)), (255, 255, 255, 235))
            px = int((W - plate.width) / 2)
            _shadow(canvas, (px, y, px + plate.width, y + plate.height), radius=int(14 * s), blur=int(14 * s), alpha=50, dy=int(6 * s))
            canvas.paste(plate, (px, y), dz.rounded_mask(plate.size, int(14 * s)))
            canvas.paste(logo, (px + int(20 * s), y + int(12 * s)), logo)
            y += plate.height + int(20 * s)
        except Exception:
            pass
    else:
        name = ctx.organization_name.upper()
        f = dz.fit(d, name, W - 2 * m, int(30 * s), 16, lambda z: dz.sans(z, "semibold"))
        dz.T(d, W / 2, y, name, f, NAVY, align="m", spacing=6 * s)
        y += int(f.size * 1.6)
    # ornament
    cy = y + int(8 * s)
    d.line([(W / 2 - 150 * s, cy), (W / 2 - 18 * s, cy)], fill=BRASS + (255,), width=max(1, int(2 * s)))
    d.line([(W / 2 + 18 * s, cy), (W / 2 + 150 * s, cy)], fill=BRASS + (255,), width=max(1, int(2 * s)))
    d.polygon([(W / 2, cy - 9 * s), (W / 2 + 9 * s, cy), (W / 2, cy + 9 * s), (W / 2 - 9 * s, cy)], fill=BRASS + (255,))
    y = cy + int(26 * s)
    kf = dz.sans(int(23 * s), "semibold")
    dz.T(d, W / 2, y, spec.kicker.upper(), kf, BRASS, align="m", spacing=6 * s)
    y += int(kf.size * 1.8)
    lines, tf = _wrap_title(d, spec.title, W - 2 * m - 20 * s, int(124 * s), int(58 * s), lambda z: dz.serif(z, "bold"), 2)
    for line in lines:
        dz.T(d, W / 2, y, line, tf, NAVY, align="m")
        y += int(tf.size * 1.08)
    y += int(6 * s)
    gf = dz.serif_italic(int(38 * s))
    for line in dz.wrap(d, spec.tagline, gf, W - 2 * m - 60 * s, 2):
        dz.T(d, W / 2, y, line, gf, NAVY_SOFT, align="m")
        y += int(gf.size * 1.32)
    if spec.location:
        y += int(6 * s)
        loc = dz.sans(int(22 * s), "semibold")
        loc_lines = dz.wrap(d, spec.location.upper(), loc, (W - 2 * m - 130 * s) / 1.12, 2)
        widest = max(dz.measure(d, line, loc, 2 * s) for line in loc_lines)
        promo._pin(d, W / 2 - widest / 2 - 24 * s, y + 3 * s, 18 * s, BRASS)
        for line in loc_lines:
            dz.T(d, W / 2 + 6 * s, y, line, loc, NAVY, align="m", spacing=2 * s)
            y += int(loc.size * 1.4)
        y += int(8 * s)

    # ---- bottom block first (so the photograph gets the rest of the height) ----
    foot_h = int(150 * s)
    rows = spec.cards[:3] if not spec.for_plot else spec.cards[:2]
    table_h = int(len(rows) * 56 * s + (20 * s if rows else 0))
    stats_h = int(138 * s)
    bottom_needed = foot_h + table_h + stats_h + int(50 * s)
    frame_top = y + int(14 * s)
    frame_bottom = H - int(48 * su) - bottom_needed
    frame_bottom = max(frame_bottom, frame_top + int(260 * s))
    frame = (m, frame_top, W - m, frame_bottom)
    _shadow(canvas, frame, radius=4, blur=int(26 * s), alpha=85, dy=int(14 * s))
    d.rectangle(frame, fill=(255, 255, 255, 255))
    pad = int(14 * s)
    inner = (frame[0] + pad, frame[1] + pad, frame[2] - pad, frame[3] - pad - int(34 * s))
    iw, ih = inner[2] - inner[0], inner[3] - inner[1]
    hero, has_imagery = spec.hero(iw, ih, (iw * 0.06, ih * 0.07, iw * 0.94, ih * 0.93))
    canvas.paste(hero, (inner[0], inner[1]))
    d.rectangle(inner, outline=NAVY + (255,), width=max(1, int(2 * s)))
    caption = f"Live satellite view · {ctx.available} plots available" if not spec.for_plot else f"Plot {spec.focus.plot_number} on the estate layout"
    cf = dz.serif_italic(int(24 * s))
    dz.T(d, W / 2, inner[3] + int(6 * s), caption, cf, NAVY_SOFT, align="m")
    y = frame[3] + int(34 * s)

    # ---- three key figures ----
    stat_items = []
    if spec.price_value:
        stat_items.append((("FROM" if not spec.for_plot else "ASKING PRICE") if spec.price_caption != "initial deposit" else "INITIAL DEPOSIT", spec.price_value))
    if not spec.for_plot:
        sizes = _sizes_range(ctx)
        if sizes:
            stat_items.append(("PLOT SIZES", sizes.replace(" sqm", "")))
        stat_items.append(("AVAILABLE", str(ctx.available)))
    else:
        for label, value, _cap in spec.cards[:2]:
            stat_items.append((label.upper(), value))
    stat_items = stat_items[:3]
    if stat_items:
        cw = (W - 2 * m) / len(stat_items)
        for index, (label, value) in enumerate(stat_items):
            cx = m + cw * index + cw / 2
            if index:
                d.line([(m + cw * index, y + 6 * s), (m + cw * index, y + 96 * s)], fill=BRASS_SOFT + (255,), width=max(1, int(1.5 * s)))
            dz.T(d, cx, y, label, dz.sans(int(19 * s), "semibold"), BRASS, align="m", spacing=4 * s)
            vf = dz.fit(d, value, cw - 26 * s, int(60 * s), 24, lambda z: dz.serif(z, "bold"))
            dz.T(d, cx, y + int(34 * s), value, vf, NAVY, align="m")
    y += stats_h

    # ---- schedule (dotted leaders) ----
    for label, value, caption_text in rows:
        lf2 = dz.sans(int(28 * s), "semibold")
        vf2 = dz.sans(int(30 * s), "extrabold")
        dz.T(d, m, y, label, lf2, NAVY)
        vw = dz.measure(d, value, vf2)
        dz.T(d, W - m, y - int(1 * s), value, vf2, NAVY, align="r")
        lw = dz.measure(d, label, lf2)
        _dots(d, m + lw + 16 * s, W - m - vw - 16 * s, y + lf2.size * 0.86, BRASS_SOFT + (255,), gap=10 * s)
        y += int(56 * s)
    if rows:
        y += int(6 * s)

    # ---- footer ----
    fy = H - int(48 * su) - foot_h
    d.line([(m, fy), (W - m, fy)], fill=BRASS + (255,), width=max(1, int(2 * s)))
    q = int(112 * s)
    text_right = W - m - (q + int(30 * s) if spec.qr_url else 0)
    yy = fy + int(20 * s)
    lines_out = _contact_lines(ctx)
    if lines_out:
        dz.T(d, m, yy, "TO RESERVE OR BOOK A VISIT", dz.sans(int(17 * s), "semibold"), BRASS, spacing=4 * s)
        yy += int(30 * s)
        for i, line in enumerate(lines_out):
            f = dz.fit(d, line, text_right - m, int(29 * s if i < 2 else 23 * s), 13, lambda z: dz.sans(z, "extrabold" if i < 2 else "medium"))
            dz.T(d, m, yy, line, f, NAVY)
            yy += int(f.size * 1.32)
    else:
        f = dz.fit(d, "Scan the code to reserve online", text_right - m, int(30 * s), 14, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, m, yy + 20 * s, "Scan the code to reserve online", f, NAVY)
    if spec.qr_url:
        qx = W - m - q
        _qr(canvas, spec.qr_url, qx, fy + int(14 * s), q, dark=NAVY, light=WHITE, pad=int(8 * s), plate=(255, 255, 255), radius=int(6 * s))
        dz.T(d, qx + q / 2, fy + int(14 * s) + q + int(9 * s), "SCAN TO RESERVE", dz.sans(int(13 * s), "semibold"), NAVY_SOFT, align="m", spacing=2 * s)
    _imagery_credit(canvas, has_imagery, m, H - int(70 * su), (20, 33, 61, 150), max(10, int(12 * su)), align="l")
    return dz._png(canvas)


def _heritage_landscape(spec: Spec) -> bytes:
    W, H = promo.SIZES["landscape"]
    ctx = spec.ctx
    canvas = Image.new("RGB", (W, H), PAPER)
    canvas.paste(dz.gradient(W, H, PAPER, PAPER_DEEP), (0, 0))
    _noise(canvas, 7)
    d = ImageDraw.Draw(canvas, "RGBA")
    d.rectangle([14, 14, W - 14, H - 14], outline=NAVY + (255,), width=2)
    d.rectangle([22, 22, W - 22, H - 22], outline=BRASS + (255,), width=1)
    left = 660
    frame = (left + 12, 44, W - 44, H - 44)
    _shadow(canvas, frame, radius=3, blur=16, alpha=80, dy=8)
    d.rectangle(frame, fill=WHITE + (255,))
    inner = (frame[0] + 10, frame[1] + 10, frame[2] - 10, frame[3] - 10)
    hero, has = spec.hero(inner[2] - inner[0], inner[3] - inner[1], ((inner[2] - inner[0]) * 0.06, (inner[3] - inner[1]) * 0.07, (inner[2] - inner[0]) * 0.94, (inner[3] - inner[1]) * 0.93))
    canvas.paste(hero, (inner[0], inner[1]))
    d.rectangle(inner, outline=NAVY + (255,), width=2)
    x = 56
    y = 50
    if ctx.logo_bytes:
        used = dz._logo_plate(canvas, ctx, x, y, 46, 220)
        y += 46 + 30
    else:
        f = dz.fit(d, ctx.organization_name.upper(), left - 120, 20, 12, lambda z: dz.sans(z, "semibold"))
        dz.T(d, x, y, ctx.organization_name.upper(), f, NAVY, spacing=4)
        y += 34
    dz.T(d, x, y, spec.kicker.upper(), dz.sans(15, "semibold"), BRASS, spacing=4)
    y += 30
    lines, tf = _wrap_title(d, spec.title, left - 110, 66, 34, lambda z: dz.serif(z, "bold"), 2)
    for line in lines:
        dz.T(d, x, y, line, tf, NAVY)
        y += int(tf.size * 1.06)
    y += 4
    gf = dz.serif_italic(22)
    for line in dz.wrap(d, spec.tagline, gf, left - 110, 2):
        dz.T(d, x, y, line, gf, NAVY_SOFT)
        y += 29
    y += 12
    d.line([(x, y), (x + 120, y)], fill=BRASS + (255,), width=2)
    y += 16
    if spec.price_value:
        dz.T(d, x, y, {"initial deposit": "INITIAL DEPOSIT", "asking price": "ASKING PRICE"}.get(spec.price_caption or "", "FROM"), dz.sans(14, "semibold"), BRASS, spacing=4)
        vf = dz.fit(d, spec.price_value, left - 110, 58, 26, lambda z: dz.serif(z, "bold"))
        dz.T(d, x, y + 22, spec.price_value, vf, NAVY)
        y += 22 + int(vf.size * 1.12)
    bits = [b for b in (spec.badge, _sizes_range(ctx) if not spec.for_plot else None) if b]
    if bits:
        dz.T(d, x, y + 4, "  ·  ".join(bits), dz.sans(19, "semibold"), NAVY_SOFT)
    lines_out = _contact_lines(ctx)
    yy = H - 60 - len(lines_out[:2]) * 26
    for line in lines_out[:2]:
        dz.T(d, x, yy, line, dz.fit(d, line, left - 200, 22, 12, lambda z: dz.sans(z, "extrabold")), NAVY)
        yy += 26
    if spec.qr_url:
        q = 92
        _qr(canvas, spec.qr_url, left - 30 - q, H - 44 - q - 6, q, dark=NAVY, pad=6, plate=(255, 255, 255), radius=4)
    _imagery_credit(canvas, has, W - 50, H - 36, (20, 33, 61, 150), 10)
    return dz._png(canvas)


# ════════════════════════════════════════════════════════════════════════════════════════════
# BOLD
# ════════════════════════════════════════════════════════════════════════════════════════════
def _bold_field(W: int, H: int, accent: tuple) -> Image.Image:
    deep = _mix(accent, (0, 0, 0), 0.35)
    canvas = dz.gradient(W, H, _mix(accent, (255, 255, 255), 0.06), deep)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    step = max(28, W // 24)
    for x in range(-H, W + H, step * 2):
        d.line([(x, 0), (x + H * 0.55, H)], fill=(255, 255, 255, 12), width=max(2, step // 6))
    d.ellipse([W * 0.55, -W * 0.35, W * 1.35, W * 0.45], outline=(255, 255, 255, 30), width=max(3, W // 140))
    d.ellipse([-W * 0.3, H * 0.28, W * 0.34, H * 0.28 + W * 0.64], outline=(255, 255, 255, 22), width=max(3, W // 160))
    dz.over(canvas, layer)
    return canvas


def _sticker(canvas: Image.Image, cx: int, cy: int, radius: int, top: str, bottom: str, ink: tuple, fill: tuple, angle: float = -10) -> None:
    size = radius * 2 + 40
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = size / 2
    points = []
    spikes = 22
    for i in range(spikes * 2):
        r = radius if i % 2 == 0 else radius * 0.9
        a = math.pi * i / spikes
        points.append((c + r * math.cos(a), c + r * math.sin(a)))
    d.polygon(points, fill=fill + (255,))
    d.ellipse([c - radius * 0.8, c - radius * 0.8, c + radius * 0.8, c + radius * 0.8], outline=ink + (255,), width=max(2, radius // 30))
    tf = dz.fit(d, top, radius * 1.15, int(radius * 0.56), 14, lambda z: dz.sans(z, "extrabold"))
    dz.T(d, c, c - tf.size * 0.78, top, tf, ink, align="m")
    bf = dz.fit(d, bottom, radius * 1.15, int(radius * 0.22), 9, lambda z: dz.sans(z, "extrabold"))
    dz.T(d, c, c + tf.size * 0.32, bottom, bf, ink, align="m", spacing=1)
    layer = layer.rotate(angle, resample=Image.BICUBIC)
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.paste((10, 20, 30, 110), mask=layer.getchannel("A"))
    canvas.paste(Image.new("RGB", layer.size, (0, 0, 0)), (int(cx - size / 2 + radius * 0.06), int(cy - size / 2 + radius * 0.1)), shadow.filter(ImageFilter.GaussianBlur(radius // 8)).getchannel("A").point(lambda v: int(v * 0.7)))
    canvas.paste(layer, (int(cx - size / 2), int(cy - size / 2)), layer)


def _bold_vertical(spec: Spec, W: int, H: int) -> bytes:
    ctx = spec.ctx
    su = W / 1080
    tall = H / W
    k = 0.84 if tall < 1.3 else 0.93 if tall < 1.5 else 1.0
    s = su * k
    accent = spec.accent
    canvas = _bold_field(W, H, accent).convert("RGB")
    d = ImageDraw.Draw(canvas, "RGBA")
    m = int(58 * su)
    y = int(54 * s)
    plate_w = dz._logo_plate(canvas, ctx, m, y, int(64 * s), int(330 * s)) if ctx.logo_bytes else 0
    if not plate_w:
        name = ctx.organization_name.upper()
        f = dz.fit(d, name, W * 0.5, int(28 * s), 14, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, m, y + 8 * s, name, f, WHITE, spacing=5 * s)
    # availability pill (right)
    pf = dz.sans(int(23 * s), "extrabold")
    label = spec.badge.upper()
    pw = int(dz.measure(d, label, pf, 2 * s)) + int(56 * s)
    ph = int(50 * s)
    box = (W - m - pw, y + int(4 * s), W - m, y + int(4 * s) + ph)
    d.rounded_rectangle(box, radius=ph // 2, fill=YELLOW + (255,))
    d.ellipse([box[0] + 20 * s, box[1] + ph / 2 - 6 * s, box[0] + 32 * s, box[1] + ph / 2 + 6 * s], fill=(214, 30, 46, 255))
    dz.T(d, box[0] + 42 * s, box[1] + (ph - pf.getmetrics()[0]) // 2 - 2, label, pf, (30, 30, 30), spacing=2 * s)
    y += int(64 * s) + int(50 * s)

    city = (_city(ctx) or "").upper()
    kick = f"OWN LAND IN {city}" if (city and not spec.for_plot) else spec.kicker.upper()
    kf = dz.fit(d, kick, W - 2 * m, int(40 * s), 20, lambda z: dz.sans(z, "extrabold"))
    dz.T(d, m, y, kick, kf, WHITE + (215,), spacing=4 * s)
    y += int(kf.size * 1.35)
    lines, tf = _wrap_title(d, spec.title.upper() if len(spec.title) < 26 else spec.title, W - 2 * m, int(150 * s), int(62 * s), lambda z: dz.sans(z, "extrabold"), 2)
    for line in lines:
        dz.T(d, m, y, line, tf, WHITE)
        y += int(tf.size * 1.0)
    y += int(46 * s)
    # price block
    if spec.price_value:
        cap = (spec.price_caption or "").upper()
        cap = {"INITIAL DEPOSIT": "INITIAL DEPOSIT", "STARTING PRICE": "PRICES FROM", "ASKING PRICE": "ASKING PRICE"}.get(cap, cap)
        if cap:
            dz.T(d, m, y, cap, dz.sans(int(24 * s), "extrabold"), YELLOW, spacing=5 * s)
            y += int(38 * s)
        vf = dz.fit(d, spec.price_value, W - 2 * m - 20 * s, int(210 * s), 60, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, m, y, spec.price_value, vf, YELLOW)
        y += int(vf.size * 1.02)

    # map panel with a slanted top edge
    bar_h = int(150 * s)
    map_top = y + int(34 * s)
    map_bottom = H - bar_h
    if map_bottom - map_top < int(300 * s):
        map_top = map_bottom - int(300 * s)
    mh = map_bottom - map_top
    hero, has_imagery = spec.hero(W, mh, (W * 0.04, mh * 0.16, W * 0.96, mh * 0.94))
    slant = int(70 * s)
    mask = Image.new("L", (W, mh), 0)
    ImageDraw.Draw(mask).polygon([(0, slant), (W, 0), (W, mh), (0, mh)], fill=255)
    edge = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(edge).polygon([(0, map_top + slant), (W, map_top), (W, map_top + 12 * s), (0, map_top + slant + 12 * s)], fill=YELLOW + (255,))
    canvas.paste(hero, (0, map_top), mask)
    dz.over(canvas, edge)
    # size pills on the map
    pills = spec.cards[:3]
    if pills:
        pw2 = (W - 2 * m - (len(pills) - 1) * 14 * s) / len(pills)
        py = map_bottom - int(132 * s)
        for i, (label2, value2, cap2) in enumerate(pills):
            px = int(m + i * (pw2 + 14 * s))
            dz.glass(canvas, (px, py, int(px + pw2), py + int(96 * s)), radius=int(18 * s), tint=(6, 16, 26), alpha=150, blur=14)
            d = ImageDraw.Draw(canvas, "RGBA")
            dz.T(d, px + pw2 / 2, py + int(10 * s), label2, dz.sans(int(24 * s), "extrabold"), YELLOW, align="m")
            vf2 = dz.fit(d, value2, pw2 - 20 * s, int(32 * s), 14, lambda z: dz.sans(z, "extrabold"))
            dz.T(d, px + pw2 / 2, py + int(42 * s), value2, vf2, WHITE, align="m")
    # sticker
    if ctx.available and not spec.for_plot:
        _sticker(canvas, int(W - m - 120 * s), int(map_top + slant / 2 + 40 * s), int(118 * s), f"{ctx.available}", "PLOTS LEFT" if ctx.available <= 25 else "PLOTS OPEN", (30, 30, 30), YELLOW)
    elif spec.for_plot:
        _sticker(canvas, int(W - m - 120 * s), int(map_top + slant / 2 + 40 * s), int(118 * s), "PLOT", spec.focus.plot_number[:8].upper(), (30, 30, 30), YELLOW)
    d = ImageDraw.Draw(canvas, "RGBA")

    # contact bar
    d.rectangle([0, H - bar_h, W, H], fill=(14, 22, 30, 255))
    d.rectangle([0, H - bar_h, W, H - bar_h + int(6 * s)], fill=accent + (255,))
    q = int(bar_h - 36 * s)
    text_right = W - m - (q + 28 * s if spec.qr_url else 0)
    lines_out = _contact_lines(ctx)
    yy = H - bar_h + int(24 * s)
    if lines_out:
        dz.T(d, m, yy, "RESERVE OR BOOK A VISIT", dz.sans(int(18 * s), "extrabold"), YELLOW, spacing=4 * s)
        yy += int(30 * s)
        first = "  ·  ".join(lines_out[:2])
        f = dz.fit(d, first, text_right - m, int(34 * s), 14, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, m, yy, first, f, WHITE)
        yy += int(f.size * 1.3)
        if len(lines_out) > 2:
            dz.T(d, m, yy, lines_out[2], dz.fit(d, lines_out[2], text_right - m, int(24 * s), 12, lambda z: dz.sans(z, "semibold")), WHITE + (210,))
    else:
        dz.T(d, m, yy + 14 * s, "Scan the code to reserve online", dz.fit(d, "Scan the code to reserve online", text_right - m, int(34 * s), 14, lambda z: dz.sans(z, "extrabold")), WHITE)
    if spec.qr_url:
        _qr(canvas, spec.qr_url, W - m - q, H - bar_h + int(18 * s) + 6, q - 12, dark=(14, 22, 30), pad=int(9 * s), plate=(255, 255, 255), radius=int(10 * s))
    _imagery_credit(canvas, has_imagery, W - 20, map_bottom - 26, (255, 255, 255, 180), max(10, int(12 * su)))
    return dz._png(canvas)


def _bold_landscape(spec: Spec) -> bytes:
    W, H = promo.SIZES["landscape"]
    ctx = spec.ctx
    canvas = _bold_field(W, H, spec.accent).convert("RGB")
    d = ImageDraw.Draw(canvas, "RGBA")
    split = 590
    hero, has = spec.hero(W - split + 40, H, ((W - split + 40) * 0.05, H * 0.08, (W - split + 40) * 0.95, H * 0.92))
    mask = Image.new("L", (W - split + 40, H), 0)
    ImageDraw.Draw(mask).polygon([(40, 0), (W - split + 40, 0), (W - split + 40, H), (0, H)], fill=255)
    canvas.paste(hero, (split - 40, 0), mask)
    d.polygon([(split, 0), (split + 12, 0), (split - 28, H), (split - 40, H)], fill=YELLOW + (255,))
    x, y = 44, 36
    if ctx.logo_bytes:
        dz._logo_plate(canvas, ctx, x, y, 44, 210)
        y += 44 + 34
    else:
        dz.T(d, x, y, ctx.organization_name.upper(), dz.fit(d, ctx.organization_name.upper(), split - 120, 20, 12, lambda z: dz.sans(z, "extrabold")), WHITE, spacing=4)
        y += 36
    kick = f"OWN LAND IN {(_city(ctx) or '').upper()}" if (_city(ctx) and not spec.for_plot) else spec.kicker.upper()
    dz.T(d, x, y, kick, dz.fit(d, kick, split - 110, 20, 12, lambda z: dz.sans(z, "extrabold")), WHITE + (220,), spacing=3)
    y += 32
    lines, tf = _wrap_title(d, spec.title.upper() if len(spec.title) < 24 else spec.title, split - 120, 74, 32, lambda z: dz.sans(z, "extrabold"), 2)
    for line in lines:
        dz.T(d, x, y, line, tf, WHITE)
        y += tf.size
    y += 26
    if spec.price_value:
        dz.T(d, x, y, (spec.price_caption or "from").upper(), dz.sans(15, "extrabold"), YELLOW, spacing=4)
        vf = dz.fit(d, spec.price_value, split - 110, 84, 30, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, x, y + 22, spec.price_value, vf, YELLOW)
    lines_out = _contact_lines(ctx)
    d.rectangle([0, H - 66, split - 34, H], fill=(14, 22, 30, 255))
    if lines_out:
        dz.T(d, x, H - 46, "  ·  ".join(lines_out[:2]), dz.fit(d, "  ·  ".join(lines_out[:2]), split - 130, 22, 12, lambda z: dz.sans(z, "extrabold")), WHITE)
    if spec.qr_url:
        _qr(canvas, spec.qr_url, W - 122, H - 122, 96, dark=(14, 22, 30), pad=7, plate=(255, 255, 255), radius=8)
    pf = dz.sans(17, "extrabold")
    label = spec.badge.upper()
    pw = int(dz.measure(d, label, pf, 2)) + 40
    d.rounded_rectangle([W - 24 - pw, 22, W - 24, 58], radius=18, fill=YELLOW + (255,))
    dz.T(d, W - 24 - pw + 20, 30, label, pf, (30, 30, 30), spacing=2)
    _imagery_credit(canvas, has, W - 16, H - 22, (255, 255, 255, 170), 10)
    return dz._png(canvas)


# ════════════════════════════════════════════════════════════════════════════════════════════
# BLUEPRINT
# ════════════════════════════════════════════════════════════════════════════════════════════
def _bp_background(W: int, H: int, su: float) -> Image.Image:
    canvas = dz.gradient(W, H, BP_TOP, BP_BOTTOM)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    minor = max(16, int(24 * su))
    for x in range(0, W, minor):
        major = (x // minor) % 5 == 0
        d.line([(x, 0), (x, H)], fill=CYAN + ((34 if major else 15),), width=1)
    for y in range(0, H, minor):
        major = (y // minor) % 5 == 0
        d.line([(0, y), (W, y)], fill=CYAN + ((34 if major else 15),), width=1)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([-W * 0.2, -H * 0.1, W * 0.8, H * 0.5], fill=(40, 120, 200, 46))
    dz.over(canvas, glow.filter(ImageFilter.GaussianBlur(max(W, H) // 8)))
    dz.over(canvas, layer)
    return canvas


def _corner_marks(d: ImageDraw.ImageDraw, box: tuple, length: int, colour: tuple, width: int = 2) -> None:
    x0, y0, x1, y1 = box
    for (cx, cy, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        d.line([(cx, cy), (cx + dx * length, cy)], fill=colour, width=width)
        d.line([(cx, cy), (cx, cy + dy * length)], fill=colour, width=width)


def _bp_map(spec: Spec, w: int, h: int) -> tuple[Image.Image, bool]:
    """Survey-drawing map: the satellite view washed into navy, plots outlined in cyan, status by tint."""
    ctx = spec.ctx
    rect = (w * 0.06, h * 0.08, w * 0.94, h * 0.92)
    bounds = dz.plot_focus_bounds(spec.focus) if spec.focus is not None else dz.estate_bounds(ctx)
    view = dz.fit_view(bounds, w, h, rect, max_zoom=20.4 if spec.focus is not None else 19.6)
    imagery = dz.fetch_satellite(view)
    has = imagery is not None
    if has:
        gray = imagery.convert("L")
        base = Image.merge("RGB", (gray.point(lambda v: int(BP_TOP[0] + v * 0.16)), gray.point(lambda v: int(BP_TOP[1] + v * 0.42)), gray.point(lambda v: int(BP_TOP[2] + v * 0.74))))
    else:
        base = dz.gradient(w, h, BP_TOP, BP_BOTTOM)
    ss = 2
    overlay = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay, "RGBA")
    focus_id = getattr(spec.focus, "id", None)
    for plot in ctx.plots:
        pts = [(x * ss, y * ss) for x, y in dz._ring_px(view, plot)]
        status = plot.commercial_status
        if spec.focus is not None:
            fill, line, lw = ((CYAN + (120,)), WHITE + (255,), 5) if plot.id == focus_id else ((8, 24, 44, 90), CYAN_SOFT + (110,), 2)
        elif status == "available":
            fill, line, lw = CYAN + (70,), CYAN + (255,), 3
        elif status == "reserved":
            fill, line, lw = AMBER + (85,), AMBER + (255,), 3
        else:
            fill, line, lw = SLATE + (70,), SLATE + (200,), 2
        d.polygon(pts, fill=fill)
        d.line(pts + [pts[0]], fill=line, width=lw, joint="curve")
    if ctx.estate.boundary is not None and spec.focus is None:
        from geoalchemy2.shape import to_shape

        ring = [(x * ss, y * ss) for x, y in (view.project(lon, lat) for lon, lat in to_shape(ctx.estate.boundary).exterior.coords)]
        dash = 26 * ss
        for i in range(len(ring) - 1):
            (x0, y0), (x1, y1) = ring[i], ring[i + 1]
            length = math.hypot(x1 - x0, y1 - y0) or 1
            t = 0.0
            while t < length:
                t2 = min(t + dash, length)
                d.line([(x0 + (x1 - x0) * t / length, y0 + (y1 - y0) * t / length), (x0 + (x1 - x0) * t2 / length, y0 + (y1 - y0) * t2 / length)], fill=WHITE + (235,), width=4)
                t += dash * 1.8
    focus_labels = [spec.focus] if spec.focus is not None else [p for p in ctx.plots if p.commercial_status in {"available", "reserved"}]
    for plot in focus_labels:
        ring = dz._ring_px(view, plot)
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        pw2, ph2 = (max(xs) - min(xs)) * ss, (max(ys) - min(ys)) * ss
        text = dz.short_label(plot.plot_number)
        size = int(max(min(min(pw2, ph2) * 0.4, (34 if spec.focus is not None else 22) * ss), 0))
        while size >= 9 * ss:
            fnt = dz.sans(size, "bold")
            if d.textlength(text, font=fnt) <= pw2 * 0.9 and size * 1.15 <= ph2:
                d.text(((min(xs) + max(xs)) * ss / 2, (min(ys) + max(ys)) * ss / 2), text, font=fnt, fill=WHITE + (255,), anchor="mm", stroke_width=max(2, size // 9), stroke_fill=(4, 16, 32, 210))
                break
            size -= 2
    overlay = overlay.resize((w, h), Image.LANCZOS)
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB"), has


def _north_arrow(d: ImageDraw.ImageDraw, x: float, y: float, size: float, colour: tuple) -> None:
    d.polygon([(x, y - size), (x + size * 0.42, y + size * 0.6), (x, y + size * 0.28), (x - size * 0.42, y + size * 0.6)], fill=colour)
    d.ellipse([x - size * 0.9, y - size * 0.9 + size * 0.15, x + size * 0.9, y + size * 0.9 + size * 0.15], outline=colour, width=max(1, int(size / 14)))
    dz.T(d, x, y - size * 2.05, "N", dz.sans(int(size * 0.85), "extrabold"), colour, align="m")


def _dim_line(d: ImageDraw.ImageDraw, x0: float, x1: float, y: float, label: str, colour: tuple, size: int, vertical: bool = False) -> None:
    tick = size * 0.55
    if not vertical:
        d.line([(x0, y), (x1, y)], fill=colour, width=max(1, size // 12))
        for x in (x0, x1):
            d.line([(x, y - tick), (x, y + tick)], fill=colour, width=max(1, size // 12))
        f = dz.sans(size, "semibold")
        w = dz.measure(d, label, f, 2)
        mid = (x0 + x1) / 2
        d.rectangle([mid - w / 2 - 12, y - size * 0.7, mid + w / 2 + 12, y + size * 0.7], fill=(9, 28, 54, 255))
        dz.T(d, mid, y - f.getmetrics()[0] * 0.55, label, f, colour, align="m", spacing=2)


def _blueprint_vertical(spec: Spec, W: int, H: int) -> bytes:
    ctx = spec.ctx
    su = W / 1080
    tall = H / W
    k = 0.86 if tall < 1.3 else 0.94 if tall < 1.5 else 1.0
    s = su * k
    canvas = _bp_background(W, H, su).convert("RGB")
    d = ImageDraw.Draw(canvas, "RGBA")
    m = int(64 * su)
    _corner_marks(d, (int(28 * su), int(28 * su), W - int(28 * su), H - int(28 * su)), int(46 * su), CYAN + (255,), max(2, int(3 * su)))
    lat, lon = _centre(spec)
    coord = f"{_fmt_deg(lat, 'N', 'S')}   {_fmt_deg(lon, 'E', 'W')}"
    y = int(58 * s)
    plate = dz._logo_plate(canvas, ctx, m, y, int(60 * s), int(300 * s)) if ctx.logo_bytes else 0
    d = ImageDraw.Draw(canvas, "RGBA")
    if not plate:
        name = ctx.organization_name.upper()
        f = dz.fit(d, name, W * 0.5, int(26 * s), 13, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, m, y + 8 * s, name, f, WHITE, spacing=5 * s)
    tf0 = dz.sans(int(16 * s), "semibold")
    dz.T(d, W - m, y + 4 * s, "LIVE AVAILABILITY", tf0, CYAN, align="r", spacing=4 * s)
    dz.T(d, W - m, y + 4 * s + int(26 * s), _sun_meta(spec), tf0, CYAN_SOFT + (200,), align="r", spacing=3 * s)
    y += int(96 * s)
    dz.T(d, m, y, "ESTATE LAYOUT PLAN" if not spec.for_plot else "PLOT PARTICULARS", dz.sans(int(21 * s), "semibold"), CYAN, spacing=6 * s)
    y += int(34 * s)
    lines, tf = _wrap_title(d, spec.title, W - 2 * m, int(118 * s), int(54 * s), lambda z: dz.sans(z, "extrabold"), 2)
    for line in lines:
        dz.T(d, m, y, line, tf, WHITE)
        y += int(tf.size * 1.04)
    y += int(22 * s)
    sub = spec.kicker if spec.for_plot else (spec.location or spec.tagline)
    sf = dz.fit(d, sub, W - 2 * m, int(30 * s), 15, lambda z: dz.sans(z, "medium"))
    dz.T(d, m, y, sub, sf, CYAN_SOFT)
    y += int(sf.size * 1.9)

    # bottom: specification and contact
    facts = [f for f in spec.facts if f[0] != "Location"][:4]
    row_h = int(46 * s)
    spec_h = int(len(facts) * row_h + 44 * s)
    contact_h = int(150 * s)
    map_top = y
    map_bottom = H - int(40 * su) - contact_h - spec_h - int(122 * s)
    map_bottom = max(map_bottom, map_top + int(300 * s))
    frame = (m, map_top, W - m, map_bottom)
    fw, fh = frame[2] - frame[0], frame[3] - frame[1]
    mp, has_imagery = _bp_map(spec, fw, fh)
    canvas.paste(mp, (frame[0], frame[1]))
    d = ImageDraw.Draw(canvas, "RGBA")
    d.rectangle(frame, outline=CYAN + (255,), width=max(2, int(2 * su)))
    _corner_marks(d, (frame[0] - int(10 * su), frame[1] - int(10 * su), frame[2] + int(10 * su), frame[3] + int(10 * su)), int(26 * su), WHITE + (255,), max(2, int(3 * su)))
    _north_arrow(d, frame[2] - 54 * s, frame[1] + 80 * s, 24 * s, WHITE + (255,))
    wm, hm = _span_metres(spec)
    if wm > 0:
        _dim_line(d, frame[0] + 6, frame[2] - 6, frame[3] + int(28 * s), f"≈ {wm:,} m", CYAN_SOFT + (255,), int(18 * s))
    y = frame[3] + int(64 * s)
    # legend
    lx = m
    for label, colour in (("Available", CYAN), ("Reserved", AMBER), ("Sold", SLATE)):
        d.rectangle([lx, y + 3 * s, lx + 16 * s, y + 19 * s], fill=colour + (200,), outline=colour + (255,))
        lf = dz.sans(int(19 * s), "medium")
        dz.T(d, lx + 26 * s, y, label, lf, WHITE + (215,))
        lx += 26 * s + dz.measure(d, label, lf) + 30 * s
    coord_f = dz.sans(int(16 * s), "medium")
    dz.T(d, W - m, y + 2 * s, coord, dz.fit(d, coord, W - lx - m - 20, int(16 * s), 10, lambda z: dz.sans(z, "medium")), CYAN + (210,), align="r", spacing=2 * s)
    y += int(42 * s)

    # specification table + price
    right_w = int(W * 0.34)
    tx1 = W - m - right_w - int(24 * s)
    dz.T(d, m, y, "SPECIFICATION", dz.sans(int(17 * s), "semibold"), CYAN, spacing=5 * s)
    yy = y + int(34 * s)
    for label, value in facts:
        d.line([(m, yy - 5 * s), (tx1, yy - 5 * s)], fill=CYAN + (60,), width=1)
        dz.T(d, m, yy + 4 * s, label, dz.sans(int(20 * s), "medium"), WHITE + (185,))
        vf = dz.fit(d, value, (tx1 - m) * 0.62, int(24 * s), 11, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, tx1, yy + 2 * s, value, vf, WHITE, align="r")
        yy += row_h
    px0 = tx1 + int(24 * s)
    if spec.price_value:
        box = (px0, y, W - m, y + int(len(facts) * row_h + 34 * s))
        d.rectangle(box, outline=CYAN + (255,), width=max(2, int(2 * su)))
        d.rectangle([box[0], box[1], box[2], box[1] + int(8 * s)], fill=CYAN + (255,))
        cap = {"initial deposit": "INITIAL DEPOSIT", "starting price": "PLOTS FROM", "asking price": "ASKING PRICE"}.get(spec.price_caption or "", (spec.price_caption or "").upper())
        dz.T(d, (box[0] + box[2]) / 2, box[1] + int(28 * s), cap, dz.sans(int(16 * s), "semibold"), CYAN, align="m", spacing=4 * s)
        vf = dz.fit(d, spec.price_value, box[2] - box[0] - 30 * s, int(74 * s), 24, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, (box[0] + box[2]) / 2, box[1] + int(74 * s), spec.price_value, vf, WHITE, align="m")
        dz.T(d, (box[0] + box[2]) / 2, box[3] - int(46 * s), spec.badge.upper(), dz.fit(d, spec.badge.upper(), box[2] - box[0] - 26 * s, int(16 * s), 9, lambda z: dz.sans(z, "semibold")), AMBER, align="m", spacing=2 * s)
    y = H - int(40 * su) - contact_h + int(4 * s)
    d.line([(m, y), (W - m, y)], fill=CYAN + (255,), width=max(1, int(2 * su)))
    q = int(112 * s)
    text_right = W - m - (q + int(34 * s) if spec.qr_url else 0)
    yy = y + int(22 * s)
    lines_out = _contact_lines(ctx)
    dz.T(d, m, yy, "ENQUIRIES & RESERVATIONS", dz.sans(int(16 * s), "semibold"), CYAN, spacing=5 * s)
    yy += int(30 * s)
    if lines_out:
        for i, line in enumerate(lines_out):
            f = dz.fit(d, line, text_right - m, int(30 * s if i < 2 else 22 * s), 13, lambda z: dz.sans(z, "extrabold" if i < 2 else "medium"))
            dz.T(d, m, yy, line, f, WHITE if i < 2 else WHITE + (200,))
            yy += int(f.size * 1.3)
    else:
        dz.T(d, m, yy, "Scan the code to reserve online", dz.fit(d, "Scan the code to reserve online", text_right - m, int(30 * s), 14, lambda z: dz.sans(z, "extrabold")), WHITE)
    if spec.qr_url:
        qx = W - m - q
        _corner_marks(d, (qx - 12, y + 10, qx + q + 12, y + 10 + q + 24), int(16 * s), CYAN + (255,), 2)
        _qr(canvas, spec.qr_url, qx, y + 22, q, dark=BP_TOP, light=WHITE, pad=int(6 * s), plate=(255, 255, 255), radius=2)
    _imagery_credit(canvas, has_imagery, frame[2] - 8, frame[3] - 22, (255, 255, 255, 190), max(10, int(11 * su)))
    return dz._png(canvas)


def _blueprint_landscape(spec: Spec) -> bytes:
    W, H = promo.SIZES["landscape"]
    ctx = spec.ctx
    canvas = _bp_background(W, H, 1.0).convert("RGB")
    d = ImageDraw.Draw(canvas, "RGBA")
    _corner_marks(d, (14, 14, W - 14, H - 14), 30, CYAN + (255,), 2)
    map_box = (640, 44, W - 40, H - 60)
    mw, mh = map_box[2] - map_box[0], map_box[3] - map_box[1]
    mp, has = _bp_map(spec, mw, mh)
    canvas.paste(mp, (map_box[0], map_box[1]))
    d.rectangle(map_box, outline=CYAN + (255,), width=2)
    _corner_marks(d, (map_box[0] - 8, map_box[1] - 8, map_box[2] + 8, map_box[3] + 8), 18, WHITE + (255,), 2)
    _north_arrow(d, map_box[2] - 36, map_box[1] + 52, 15, WHITE + (255,))
    wm, _hm = _span_metres(spec)
    if wm > 0:
        _dim_line(d, map_box[0] + 4, map_box[2] - 4, map_box[3] + 24, f"≈ {wm:,} m", CYAN_SOFT + (255,), 14)
    x, y = 50, 40
    if ctx.logo_bytes:
        dz._logo_plate(canvas, ctx, x, y, 40, 190)
        y += 40 + 30
    else:
        dz.T(d, x, y, ctx.organization_name.upper(), dz.fit(d, ctx.organization_name.upper(), 480, 18, 12, lambda z: dz.sans(z, "extrabold")), WHITE, spacing=4)
        y += 34
    dz.T(d, x, y, "ESTATE LAYOUT PLAN" if not spec.for_plot else "PLOT PARTICULARS", dz.sans(15, "semibold"), CYAN, spacing=5)
    y += 28
    lines, tf = _wrap_title(d, spec.title, 550, 60, 30, lambda z: dz.sans(z, "extrabold"), 2)
    for line in lines:
        dz.T(d, x, y, line, tf, WHITE)
        y += int(tf.size * 1.04)
    sub = spec.kicker if spec.for_plot else (spec.location or spec.tagline)
    dz.T(d, x, y + 14, sub, dz.fit(d, sub, 550, 20, 11, lambda z: dz.sans(z, "medium")), CYAN_SOFT)
    y += 58
    if spec.price_value:
        dz.T(d, x, y, {"initial deposit": "INITIAL DEPOSIT", "starting price": "PLOTS FROM", "asking price": "ASKING PRICE"}.get(spec.price_caption or "", ""), dz.sans(14, "semibold"), CYAN, spacing=4)
        vf = dz.fit(d, spec.price_value, 540, 70, 28, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, x, y + 20, spec.price_value, vf, WHITE)
        y += 20 + int(vf.size * 1.25)
    for label, value in [f for f in spec.facts if f[0] != "Location"][:3]:
        dz.T(d, x, y, f"{label.upper()}", dz.sans(13, "semibold"), CYAN + (230,), spacing=3)
        dz.T(d, x + 190, y - 2, value, dz.fit(d, value, 360, 19, 11, lambda z: dz.sans(z, "extrabold")), WHITE)
        y += 27
    lines_out = _contact_lines(ctx)
    if lines_out:
        d.line([(x, H - 64), (600, H - 64)], fill=CYAN + (255,), width=1)
        dz.T(d, x, H - 48, "  ·  ".join(lines_out[:2]), dz.fit(d, "  ·  ".join(lines_out[:2]), 530, 21, 11, lambda z: dz.sans(z, "extrabold")), WHITE)
    if spec.qr_url:
        _qr(canvas, spec.qr_url, 50 + 520 - 84, H - 60 - 84 - 8, 84, dark=BP_TOP, pad=5, plate=(255, 255, 255), radius=2)
    _imagery_credit(canvas, has, map_box[2] - 6, map_box[3] - 18, (255, 255, 255, 190), 10)
    return dz._png(canvas)


# ── Public entry points ──────────────────────────────────────────────────────────────────────
def _compose(spec: Spec, name: str, kind: str) -> bytes:
    if kind == "landscape":
        return {"heritage": _heritage_landscape, "bold": _bold_landscape, "blueprint": _blueprint_landscape}[name](spec)
    W, H = promo.SIZES[kind]
    return {"heritage": _heritage_vertical, "bold": _bold_vertical, "blueprint": _blueprint_vertical}[name](spec, W, H)


def estate_design(ctx: "mr.MarketingContext", name: str, kind: str, *, qr: bool = True) -> bytes:
    return _compose(estate_spec(ctx, qr=qr), name, kind)


def plot_design(ctx: "mr.MarketingContext", plot: Any, name: str, kind: str, *, qr: bool = True) -> bytes:
    return _compose(plot_spec(ctx, plot, qr=qr), name, kind)
