from __future__ import annotations

"""The bright "promo poster" design family: airy sky backdrop, a bold two-tone estate name, a red
price ribbon, size/price cards and address bars - the look Nigerian land promotions use - built
entirely from the estate's own data. The house render of a typical poster is replaced by the live
satellite map of the estate, which is the one thing a land developer can show that is real."""

import io
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFilter

from app.services.estates import marketing_design as dz
from app.services.estates import marketing_render as mr

DEFAULT_ACCENT = (24, 148, 88)
RIBBON = (214, 30, 46)
RIBBON_DARK = (150, 16, 30)
INK = (17, 24, 21)
SOFT_INK = (66, 78, 72)
GOLDEN = (255, 214, 64)
SIZES = {"status": (1080, 1920), "post": (1080, 1350), "poster": (1240, 1754), "landscape": (1200, 630)}


def accent_from_logo(logo_bytes: bytes | None) -> tuple[int, int, int]:
    """Most characteristic saturated colour of the company logo, darkened until white text reads on it."""
    if not logo_bytes:
        return DEFAULT_ACCENT
    try:
        image = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
        image.thumbnail((96, 96))
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
        palette = flat.quantize(colors=6).convert("RGB")
        best, best_score = None, 0.0
        for count, colour in palette.getcolors(9216) or []:
            r, g, b = colour
            mx, mn = max(colour), min(colour)
            saturation = (mx - mn) / max(mx, 1)
            lightness = (mx + mn) / 510
            if saturation < 0.28 or lightness > 0.86 or lightness < 0.12:
                continue
            score = count * saturation
            if score > best_score:
                best, best_score = colour, score
        if best is None:
            return DEFAULT_ACCENT
        r, g, b = best
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        if luminance > 150:
            factor = 150 / luminance
            r, g, b = int(r * factor), int(g * factor), int(b * factor)
        return (r, g, b)
    except Exception:
        return DEFAULT_ACCENT


def _mix(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


# ── Data → poster copy ───────────────────────────────────────────────────────────────────────
def _size_cards(ctx: "mr.MarketingContext") -> list[tuple[str, str, str]]:
    """(label, value, caption) for the most common plot sizes, each with its lowest available price."""
    groups: dict[int, list[Any]] = {}
    for plot in ctx.available_plots:
        try:
            groups.setdefault(int(round(float(plot.area_sqm))), []).append(plot)
        except Exception:
            continue
    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))[:3]
    cards = []
    for area, plots in sorted(ranked, key=lambda item: item[0]):
        prices = [float(p.asking_price) for p in plots if p.asking_price is not None]
        value = mr.naira(min(prices)) if (ctx.show_prices and prices) else "On request"
        cards.append((f"{area:,} sqm", value, f"{len(plots)} plot{'s' if len(plots) != 1 else ''} available"))
    return cards


def _deposit(ctx: "mr.MarketingContext") -> tuple[str | None, str | None]:
    """Ribbon copy: an initial deposit when the payment plan defines one, else the starting price."""
    if not ctx.show_prices or not ctx.min_price:
        return None, None
    plan = ctx.payment_plan or []
    try:
        first = float(plan[0].get("percentage")) if plan else 0.0
    except (TypeError, ValueError):
        first = 0.0
    if 0 < first < 100:
        return mr.naira_short(float(ctx.min_price) * first / 100), "initial deposit"
    return mr.naira_short(ctx.min_price), "starting price"


# ── Drawing pieces ───────────────────────────────────────────────────────────────────────────
def _sky(W: int, H: int) -> Image.Image:
    base = dz.gradient(W, H, (232, 243, 250), (250, 252, 252))
    clouds = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    d = ImageDraw.Draw(clouds)
    for cx, cy, rx, ry, a in [(0.18, 0.06, 0.34, 0.06, 235), (0.82, 0.03, 0.30, 0.05, 220), (0.06, 0.24, 0.26, 0.05, 210), (0.92, 0.30, 0.24, 0.05, 200), (0.5, 0.14, 0.42, 0.05, 150)]:
        d.ellipse([W * (cx - rx), H * (cy - ry), W * (cx + rx), H * (cy + ry)], fill=(255, 255, 255, a))
    clouds = clouds.filter(ImageFilter.GaussianBlur(max(W, H) // 22))
    return Image.alpha_composite(base.convert("RGBA"), clouds).convert("RGB")


def _pin(d: ImageDraw.ImageDraw, cx: float, cy: float, size: float, colour: tuple) -> None:
    r = size * 0.5
    d.polygon([(cx - r * 0.86, cy + r * 0.32), (cx + r * 0.86, cy + r * 0.32), (cx, cy + r * 2.05)], fill=colour)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    d.ellipse([cx - r * 0.42, cy - r * 0.42, cx + r * 0.42, cy + r * 0.42], fill=(255, 255, 255))


def _ribbon(target: Image.Image, cx: float, top: float, width: float, height: float) -> tuple[float, float, float, float]:
    """Red banner with folded tails, drawn into an RGBA layer. Returns the body rectangle."""
    shadow = Image.new("RGBA", target.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse([cx - width * 0.46, top + height * 0.98, cx + width * 0.46, top + height * 1.22], fill=(20, 30, 40, 90))
    target.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(int(height * 0.12))))
    layer = Image.new("RGBA", target.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    tail = height * 0.34
    x0, x1 = cx - width / 2, cx + width / 2
    bx0, bx1 = x0 + tail * 1.25, x1 - tail * 1.25
    ty0, ty1 = top + height * 0.24, top + height * 1.04
    d.polygon([(x0, ty0), (bx0 + tail * 0.35, ty0), (bx0 + tail * 0.35, ty1), (x0, ty1), (x0 + tail * 0.55, (ty0 + ty1) / 2)], fill=RIBBON_DARK + (255,))
    d.polygon([(x1, ty0), (bx1 - tail * 0.35, ty0), (bx1 - tail * 0.35, ty1), (x1, ty1), (x1 - tail * 0.55, (ty0 + ty1) / 2)], fill=RIBBON_DARK + (255,))
    d.polygon([(bx0, top + height), (bx0 + tail * 0.35, top + height), (bx0 + tail * 0.35, ty1)], fill=(96, 8, 18, 255))
    d.polygon([(bx1, top + height), (bx1 - tail * 0.35, top + height), (bx1 - tail * 0.35, ty1)], fill=(96, 8, 18, 255))
    d.rounded_rectangle([bx0, top, bx1, top + height], radius=int(height * 0.08), fill=RIBBON + (255,))
    sheen = dz.alpha_gradient(int(bx1 - bx0), int(height * 0.5), (255, 255, 255), 60, 0)
    layer.alpha_composite(sheen, (int(bx0), int(top)))
    target.alpha_composite(layer)
    return bx0, top, bx1, top + height


# ── Poster ───────────────────────────────────────────────────────────────────────────────────
@dataclass
class PromoSpec:
    ctx: "mr.MarketingContext"
    headline: str
    ribbon_value: str | None
    ribbon_caption: str | None
    cards: list[tuple[str, str, str]]
    badge: str
    hero_builder: Callable[[int, int, tuple[float, float, float, float]], tuple[Image.Image, bool]]
    qr_url: str | None
    accent: tuple[int, int, int]


def _title(d: ImageDraw.ImageDraw, name: str, cx: float, y: float, max_w: float, start: int, accent: tuple) -> int:
    """Two-tone estate name: first word heavy, the rest light, like a wordmark. Returns height used."""
    words = name.split()
    if len(words) >= 2:
        first, rest = words[0], " ".join(words[1:])
        size = start
        while size > 70:
            w = dz.measure(d, first, dz.sans(size, "extrabold")) + dz.measure(d, " " + rest, dz.sans(size, "regular"))
            if w <= max_w:
                break
            size -= 4
        f1, f2 = dz.sans(size, "extrabold"), dz.sans(size, "regular")
        total = dz.measure(d, first, f1) + dz.measure(d, " " + rest, f2)
        x = cx - total / 2
        dz.T(d, x, y, first, f1, accent)
        dz.T(d, x + dz.measure(d, first, f1), y, " " + rest, f2, accent)
        return int(size * 1.25)
    f = dz.fit(d, name, max_w, start, 70, lambda s: dz.sans(s, "extrabold"))
    dz.T(d, cx, y, name, f, accent, align="m")
    return int(f.size * 1.25)


def _headline(d: ImageDraw.ImageDraw, text: str, cx: float, y: float, max_w: float, start: int) -> int:
    """Italic serif headline; any word containing a digit is set in the sans face, because the serif
    uses old-style numerals ("Plot 13" reads like "Plot ı3"). Returns the height used."""
    words = text.split()

    def face(word: str, size: int):
        return dz.sans(int(size * 0.88), "bold") if any(ch.isdigit() for ch in word) else dz.serif_italic(size)

    def width_of(seq: list[str], size: int) -> float:
        return sum(dz.measure(d, word, face(word, size)) for word in seq) + dz.measure(d, " ", dz.serif_italic(size)) * max(len(seq) - 1, 0)

    size = start
    while size > 26 and width_of(words, size) > max_w * 1.9:
        size -= 2
    lines: list[list[str]] = [[]]
    for word in words:
        if lines[-1] and width_of(lines[-1] + [word], size) > max_w:
            lines.append([])
        lines[-1].append(word)
    lines = lines[:2]
    used = 0
    for line in lines:
        total = width_of(line, size)
        x = cx - total / 2
        space = dz.measure(d, " ", dz.serif_italic(size))
        for word in line:
            f = face(word, size)
            x += dz.T(d, x, y + used, word, f, INK + (255,)) + space
        used += int(size * 1.3)
    return used


def compose_poster(spec: PromoSpec, kind: str) -> bytes:
    W, H = SIZES[kind]
    ctx = spec.ctx
    accent = spec.accent
    ratio = H / W
    k = 0.84 if ratio < 1.3 else 0.92 if ratio < 1.5 else 1.0
    s = (W / 1080) * k
    su = W / 1080  # unscaled, for margins and bars
    sky = _sky(W, H)
    content = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(content)
    margin = int(48 * su)
    cx = W / 2

    y = int(40 * su)
    logo = None
    if ctx.logo_bytes:
        try:
            logo = Image.open(io.BytesIO(ctx.logo_bytes)).convert("RGBA")
            logo.thumbnail((int(420 * s), int(120 * s)), Image.LANCZOS)
        except Exception:
            logo = None
    if logo is not None:
        content.paste(logo, (int(cx - logo.width / 2), y), logo)
        y += logo.height + int(10 * s)
    else:
        name = ctx.organization_name.upper()
        f = dz.fit(d, name, W - 2 * margin, int(34 * s), 16, lambda z: dz.sans(z, "extrabold"))
        w = dz.T(d, cx, y, name, f, accent + (255,), align="m", spacing=5 * s)
        for sign in (-1, 1):
            xa = cx + sign * (w / 2 + 20 * s)
            xb = cx + sign * (w / 2 + 70 * s)
            d.line([(xa, y + f.size * 0.6), (xb, y + f.size * 0.6)], fill=accent + (255,), width=max(2, int(3 * s)))
        y += int(f.size * 1.7)

    y += _title(d, ctx.estate.name, cx, y, W - 2 * margin, int(164 * s), accent + (255,))
    by_text = f"by {ctx.organization_name}"
    by = dz.fit(d, by_text, W - 2 * margin, int(50 * s), 22, lambda z: dz.sans(z, "extrabold"))
    dz.T(d, cx, y - int(8 * s), by_text, by, INK + (255,), align="m")
    y += int(by.size * 1.4)
    if ctx.location:
        lf = dz.sans(int(25 * s), "bold")
        lines = dz.wrap(d, ctx.location, lf, W - 2 * margin - 110 * su, 2)
        block_w = max(dz.measure(d, line, lf) for line in lines)
        _pin(d, cx - block_w / 2 - 26 * s, y + 12 * s, 20 * s, accent + (255,))
        for line in lines:
            dz.T(d, cx - block_w / 2 + 4 * s, y, line, lf, INK + (255,), align="l")
            y += int(lf.size * 1.3)
        y += int(8 * s)

    y += int(10 * s)
    y += _headline(d, spec.headline, cx, y, W - 2 * margin, int(46 * s))
    y += int(12 * s)

    if spec.ribbon_value:
        rh = int(134 * s)
        rw = int(min(W - 2 * margin, 640 * su))
        bx0, by0, bx1, by1 = _ribbon(content, cx, y, rw, rh)
        d = ImageDraw.Draw(content)
        vf = dz.fit(d, spec.ribbon_value, (bx1 - bx0) - 40 * s, int(rh * 0.52), 34, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, cx, by0 + rh * 0.05, spec.ribbon_value, vf, (255, 255, 255, 255), align="m")
        if spec.ribbon_caption:
            cf = dz.serif_italic(int(rh * 0.2))
            dz.T(d, cx, by0 + rh * 0.05 + vf.size * 1.2, spec.ribbon_caption, cf, (255, 255, 255, 255), align="m")
        y += rh + int(30 * s)

    cards_top = y
    ch = int((154 if len(spec.cards) < 3 else 140) * s)
    if spec.cards:
        n = len(spec.cards)
        gap = 0 if n == 2 else int(14 * su)
        cw = (W - 2 * margin - gap * (n - 1)) / n
        palettes = [((48, 52, 56), (34, 38, 42)), ((132, 135, 140), (108, 111, 116)), (_mix(accent, (0, 0, 0), 0.25), _mix(accent, (0, 0, 0), 0.5))]
        for i, (label, value, caption) in enumerate(spec.cards):
            x0 = margin + i * (cw + gap)
            shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rectangle([x0 + 6, cards_top + 14, x0 + cw - 2, cards_top + ch + 12], fill=(10, 20, 30, 95))
            content.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(int(10 * su))))
            content.paste(dz.gradient(int(cw), ch, *palettes[i % 3]).convert("RGBA"), (int(x0), cards_top))
            d = ImageDraw.Draw(content)
            lf = dz.fit(d, label, cw - 40 * s, int(32 * s), 18, lambda z: dz.sans(z, "extrabold"))
            dz.T(d, x0 + 24 * s, cards_top + 12 * s, label, lf, GOLDEN + (255,))
            vf = dz.fit(d, value, cw - 40 * s, int((58 if n < 3 else 44) * s), 22, lambda z: dz.sans(z, "extrabold"))
            dz.T(d, x0 + 24 * s, cards_top + 12 * s + lf.size * 1.2, value, vf, (255, 255, 255, 255))
            dz.T(d, x0 + 24 * s, cards_top + ch - 28 * s, caption, dz.sans(int(18 * s), "semibold"), (255, 255, 255, 190))
            if n == 2 and i == 0:
                mid = x0 + cw
                d.polygon([(mid, cards_top + ch * 0.36), (mid - 20 * su, cards_top + ch * 0.5), (mid, cards_top + ch * 0.64)], fill=(132, 135, 140, 255))
        y = cards_top + ch

    bar_h = int(96 * su)
    gap = int(14 * su)
    bars_bottom = H - int(56 * su)
    contact_top = bars_bottom - bar_h
    address_top = contact_top - gap - bar_h
    window_top = y + int(22 * su)
    window_bottom = address_top - int(22 * su)

    image_top = max(cards_top - int(80 * su), int(H * 0.34))
    region_h = H - image_top
    rect = (margin, window_top - image_top, W - margin, max(window_bottom - image_top, window_top - image_top + 140))
    hero, has_imagery = spec.hero_builder(W, region_h, rect)
    fade = int(190 * su)
    mask = Image.new("L", (W, region_h), 255)
    md = ImageDraw.Draw(mask)
    for row in range(fade):
        md.line([(0, row), (W, row)], fill=int(255 * (row / fade) ** 1.4))
    canvas = sky
    below = canvas.crop((0, image_top, W, H))
    below.paste(hero.convert("RGB"), (0, 0), mask)
    canvas.paste(below, (0, image_top))
    canvas.paste(content.convert("RGB"), (0, 0), content.getchannel("A"))
    d = ImageDraw.Draw(canvas, "RGBA")

    if spec.badge:
        bf = dz.sans(int(23 * su), "extrabold")
        bw = int(dz.measure(d, spec.badge, bf) + 60 * su)
        bh = int(48 * su)
        bx, by_ = margin, window_top
        d.rounded_rectangle([bx, by_, bx + bw, by_ + bh], radius=bh // 2, fill=(255, 255, 255, 236))
        d.ellipse([bx + 20 * su, by_ + bh / 2 - 7 * su, bx + 34 * su, by_ + bh / 2 + 7 * su], fill=(46, 204, 113, 255))
        dz.T(d, bx + 46 * su, by_ + (bh - bf.getmetrics()[0]) / 2 - 2, spec.badge, bf, INK)

    def bar(top: int) -> tuple[int, int, int, int]:
        box = (margin, top, W - margin, top + bar_h)
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle([box[0] + 4, box[1] + 12, box[2] - 4, box[3] + 10], radius=int(28 * su), fill=(8, 16, 22, 90))
        dz.over(canvas, shadow.filter(ImageFilter.GaussianBlur(int(12 * su))))
        ImageDraw.Draw(canvas, "RGBA").rounded_rectangle(box, radius=int(26 * su), fill=(255, 255, 255, 246))
        return box

    a = bar(address_top)
    d = ImageDraw.Draw(canvas, "RGBA")
    _pin(d, a[0] + 42 * su, a[1] + bar_h * 0.34, 24 * su, accent)
    tf = dz.sans(int(24 * su), "extrabold")
    dz.T(d, a[0] + 84 * su, a[1] + 16 * su, "Location: ", tf, INK)
    lw = dz.measure(d, "Location: ", tf)
    loc = ctx.location or ctx.estate.name
    dz.T(d, a[0] + 84 * su + lw, a[1] + 16 * su, loc, dz.fit(d, loc, a[2] - a[0] - 120 * su - lw, int(24 * su), 14, lambda z: dz.sans(z, "semibold")), SOFT_INK)
    url = re.sub(r"^https?://", "", ctx.page_url).split("?")[0]
    lead = dz.sans(int(22 * su), "extrabold")
    dz.T(d, a[0] + 84 * su, a[1] + bar_h - 40 * su, "See the live map & reserve: ", lead, INK)
    dz.T(d, a[0] + 84 * su + dz.measure(d, "See the live map & reserve: ", lead), a[1] + bar_h - 40 * su, url, dz.fit(d, url, a[2] - a[0] - 470 * su, int(22 * su), 12, lambda z: dz.sans(z, "semibold")), accent)

    c = bar(contact_top)
    d = ImageDraw.Draw(canvas, "RGBA")
    dz.T(d, c[0] + 30 * su, c[1] + 12 * su, "Contact", dz.sans(int(26 * su), "extrabold"), accent)
    reach = ctx.contact_phone or (f"+{ctx.whatsapp_digits}" if ctx.whatsapp_digits else None)
    bits = [b for b in [ctx.agent_name, reach] if b]
    room = c[2] - c[0] - 260 * su
    line_y = c[1] + bar_h - 50 * su
    if bits:
        with_email = "  \u00b7  ".join(bits + ([ctx.contact_email] if ctx.contact_email else []))
        f = dz.fit(d, with_email, room, int(30 * su), 14, lambda z: dz.sans(z, "extrabold"))
        if ctx.contact_email and f.size < int(21 * su):
            with_email = "  \u00b7  ".join(bits)
            f = dz.fit(d, with_email, room, int(30 * su), 16, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, c[0] + 30 * su, line_y, with_email, f, INK)
    elif ctx.contact_email:
        dz.T(d, c[0] + 30 * su, line_y, ctx.contact_email, dz.fit(d, ctx.contact_email, room, int(30 * su), 14, lambda z: dz.sans(z, "extrabold")), INK)
    else:
        dz.T(d, c[0] + 30 * su, line_y, "Scan the code to reserve online", dz.fit(d, "Scan the code to reserve online", room, int(28 * su), 14, lambda z: dz.sans(z, "extrabold")), INK)
    if spec.qr_url:
        q = int(bar_h - 22 * su)
        canvas.paste(mr.qr_image(spec.qr_url, q, dark=INK).convert("RGB"), (int(c[2] - q - 16 * su), int(c[1] + 11 * su)))
        dz.T(d, c[2] - q - 34 * su, c[1] + 30 * su, "Scan to reserve", dz.sans(int(19 * su), "bold"), SOFT_INK, align="r")
    credit = "Imagery \u00a9 Mapbox \u00a9 OpenStreetMap \u00a9 Maxar   \u00b7   Powered by LandCheck Estates" if has_imagery else "Powered by LandCheck Estates"
    dz.T(d, cx, H - int(38 * su), credit, dz.sans(int(14 * su), "medium"), (255, 255, 255, 230) if has_imagery else SOFT_INK, align="m", stroke=(0, 0, 0, 130) if has_imagery else None, stroke_width=1 if has_imagery else 0)
    return dz._png(canvas)


def compose_promo_landscape(spec: PromoSpec) -> bytes:
    W, H = SIZES["landscape"]
    accent = spec.accent
    ctx = spec.ctx
    canvas = _sky(W, H)
    hero, has_imagery = spec.hero_builder(W, H, (520, 60, W - 44, H - 60))
    mask = Image.new("L", (W, H), 255)
    md = ImageDraw.Draw(mask)
    for col in range(520):
        md.line([(col, 0), (col, H)], fill=int(255 * (col / 520) ** 1.5))
    canvas.paste(hero.convert("RGB"), (0, 0), mask)
    d = ImageDraw.Draw(canvas, "RGBA")
    left = 48
    y = 34
    name = ctx.organization_name.upper()
    nf = dz.fit(d, name, 420, 22, 12, lambda z: dz.sans(z, "extrabold"))
    dz.T(d, left, y, name, nf, accent, spacing=4)
    y += 46
    words = ctx.estate.name.split()
    size = 84
    while size > 44 and dz.measure(d, ctx.estate.name, dz.sans(size, "extrabold")) > 470:
        size -= 4
    if len(words) >= 2:
        f1, f2 = dz.sans(size, "extrabold"), dz.sans(size, "regular")
        w1 = dz.T(d, left, y, words[0], f1, accent)
        dz.T(d, left + w1, y, " " + " ".join(words[1:]), f2, accent)
    else:
        dz.T(d, left, y, ctx.estate.name, dz.sans(size, "extrabold"), accent)
    y += int(size * 1.22)
    dz.T(d, left, y, f"by {ctx.organization_name}", dz.fit(d, f"by {ctx.organization_name}", 440, 30, 16, lambda z: dz.sans(z, "extrabold")), INK)
    y += 44
    if ctx.location:
        _pin(d, left + 10, y + 10, 16, accent)
        dz.T(d, left + 32, y, ctx.location, dz.fit(d, ctx.location, 430, 20, 13, lambda z: dz.sans(z, "bold")), INK)
        y += 40
    if spec.ribbon_value:
        rib = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        bx0, by0, bx1, by1 = _ribbon(rib, left + 190, y + 8, 380, 92)
        dz.over(canvas, rib)
        d = ImageDraw.Draw(canvas, "RGBA")
        vf = dz.fit(d, spec.ribbon_value, 200, 46, 26, lambda z: dz.sans(z, "extrabold"))
        dz.T(d, left + 190, by0 + 4, spec.ribbon_value, vf, (255, 255, 255), align="m")
        if spec.ribbon_caption:
            dz.T(d, left + 190, by0 + 4 + vf.size * 1.18, spec.ribbon_caption, dz.serif_italic(17), (255, 255, 255), align="m")
    if spec.cards:
        top = H - 110
        cw = 176
        for i, (label, value, caption) in enumerate(spec.cards[:2]):
            x0 = left + i * (cw + 8)
            d.rectangle([x0, top, x0 + cw, top + 78], fill=((48, 52, 56, 255) if i == 0 else (132, 135, 140, 255)))
            dz.T(d, x0 + 12, top + 6, label, dz.sans(17, "extrabold"), GOLDEN)
            dz.T(d, x0 + 12, top + 28, value, dz.fit(d, value, cw - 24, 34, 16, lambda z: dz.sans(z, "extrabold")), (255, 255, 255))
    if spec.qr_url:
        q = 96
        px = W - 44 - q - 20
        d.rounded_rectangle([px - 10, H - 44 - q - 30, W - 34, H - 34], radius=22, fill=(255, 255, 255, 240))
        qr = mr.qr_image(spec.qr_url, q, dark=INK).convert("RGB")
        canvas.paste(qr, (px, H - 44 - q - 20))
    else:
        label = "Open the live map"
        f = dz.sans(20, "extrabold")
        w = int(dz.measure(d, label, f) + 60)
        d.rounded_rectangle([W - 44 - w, H - 84, W - 44, H - 34], radius=25, fill=accent + (255,))
        dz.T(d, W - 44 - w / 2, H - 84 + 13, label, f, (255, 255, 255), align="m")
    if has_imagery:
        dz.T(d, W - 20, 12, "Imagery © Mapbox © OpenStreetMap © Maxar", dz.sans(12, "medium"), (255, 255, 255, 190), align="r", stroke=(0, 0, 0, 110), stroke_width=1)
    return dz._png(canvas)


# ── Public entry points ──────────────────────────────────────────────────────────────────────
def estate_promo(ctx: "mr.MarketingContext", kind: str, *, qr: bool = True) -> bytes:
    value, caption = _deposit(ctx)
    if not ctx.available:
        value, caption = "Sold out", "join the waiting list"
    spec = PromoSpec(
        ctx=ctx,
        headline=f"Own a piece of {ctx.estate.name} with as low as" if (caption == "initial deposit") else f"Own a piece of {ctx.estate.name}",
        ribbon_value=value, ribbon_caption=caption, cards=_size_cards(ctx) if ctx.available else [],
        badge=f"{ctx.available} plot{'s' if ctx.available != 1 else ''} available now",
        hero_builder=lambda w, h, rect: dz.render_map(ctx, w, h, rect=rect, labels=False),
        qr_url=ctx.page_url if qr else None, accent=accent_from_logo(ctx.logo_bytes),
    )
    return compose_promo_landscape(spec) if kind == "landscape" else compose_poster(spec, kind)


def plot_promo(ctx: "mr.MarketingContext", plot: Any, kind: str, *, qr: bool = True) -> bytes:
    status = plot.commercial_status
    price = mr.naira_short(plot.asking_price) if (ctx.show_prices and plot.asking_price is not None) else None
    label = mr.STATUS_LABEL.get(status, "Not available")
    cards = [(mr.area_text(plot.area_sqm).replace("m²", "sqm"), mr.naira(plot.asking_price) if price else "On request", "Plot size & price")]
    dep_value, dep_caption = _deposit(ctx)
    if dep_value and dep_caption == "initial deposit" and plot.asking_price is not None and ctx.payment_plan:
        try:
            pct = float(ctx.payment_plan[0].get("percentage"))
            cards.append(("Initial deposit", mr.naira(float(plot.asking_price) * pct / 100), f"{pct:g}% to secure it"))
        except (TypeError, ValueError):
            pass
    spec = PromoSpec(
        ctx=ctx, headline=f"Secure Plot {plot.plot_number} at {ctx.estate.name}" if status == "available" else f"Plot {plot.plot_number} at {ctx.estate.name}",
        ribbon_value=price or label, ribbon_caption="asking price" if price else None, cards=cards,
        badge="Available now" if status == "available" else label,
        hero_builder=lambda w, h, rect: dz.render_map(ctx, w, h, rect=rect, focus=plot),
        qr_url=ctx.plot_url(plot) if qr else None, accent=accent_from_logo(ctx.logo_bytes),
    )
    return compose_promo_landscape(spec) if kind == "landscape" else compose_poster(spec, kind)
