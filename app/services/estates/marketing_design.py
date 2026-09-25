from __future__ import annotations

"""Premium visual layer for the Estate marketing material: satellite hero imagery with the plots
drawn over it, glass panels, serif display type and gold accents.

Satellite imagery comes from the Mapbox Static Images API. We request a plain (overlay-free) image
at an exact centre and zoom, then project every plot ourselves with Web Mercator so the plots line
up pixel-perfectly and stay crisp at any size. Without a Mapbox token (or if the request fails) the
same drawing runs over a generated dark backdrop, so nothing ever breaks.
"""

import io
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import matplotlib
import requests
from geoalchemy2.shape import to_shape
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from app.services.estates import marketing_render as mr

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "assets", "fonts")
_DEJAVU_BOLD = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans-Bold.ttf")
_DEJAVU = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans.ttf")

SANS_FILES = {
    "regular": "Manrope-Regular.ttf",
    "medium": "Manrope-Medium.ttf",
    "semibold": "Manrope-SemiBold.ttf",
    "bold": "Manrope-Bold.ttf",
    "extrabold": "Manrope-ExtraBold.ttf",
}
SERIF_FILES = {"semibold": "PlayfairDisplay-SemiBold.ttf", "bold": "PlayfairDisplay-Bold.ttf"}

# ── Palette ──────────────────────────────────────────────────────────────────────────────────
NIGHT = (6, 28, 20)
NIGHT_DEEP = (4, 19, 14)
EMERALD = (13, 59, 42)
GOLD = (222, 184, 98)
GOLD_SOFT = (242, 214, 150)
IVORY = (248, 244, 233)
MIST = (190, 214, 201)
WHITE = (255, 255, 255)
INK = (10, 30, 22)


def _font_path(directory_file: str, fallback: str) -> str:
    path = os.path.join(FONT_DIR, directory_file)
    return path if os.path.exists(path) else fallback


@lru_cache(maxsize=128)
def sans(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_font_path(SANS_FILES.get(weight, SANS_FILES["regular"]), _DEJAVU_BOLD if weight in {"bold", "extrabold", "semibold"} else _DEJAVU), int(size))


@lru_cache(maxsize=64)
def serif(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_font_path(SERIF_FILES.get(weight, SERIF_FILES["bold"]), _DEJAVU_BOLD), int(size))


@lru_cache(maxsize=32)
def serif_italic(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_font_path("PlayfairDisplay-Italic-Medium.ttf", _DEJAVU_BOLD), int(size))


@lru_cache(maxsize=64)
def _naira_font(size: int) -> ImageFont.FreeTypeFont:
    # Neither bundled family carries the naira sign, so it is drawn from a fallback face.
    return ImageFont.truetype(_DEJAVU_BOLD, max(8, int(size * 0.92)))


# ── Text (naira-aware) ───────────────────────────────────────────────────────────────────────
def _runs(text: str) -> list[tuple[str, bool]]:
    parts: list[tuple[str, bool]] = []
    for chunk in re.split("(₦)", str(text)):
        if chunk:
            parts.append((chunk, chunk == "₦"))
    return parts


def measure(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, spacing: float = 0) -> float:
    total = 0.0
    for chunk, is_naira in _runs(text):
        face = _naira_font(fnt.size) if is_naira else fnt
        total += draw.textlength(chunk, font=face) + spacing * len(chunk)
    return total


def T(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, fnt: ImageFont.FreeTypeFont, fill: tuple, *, align: str = "l", spacing: float = 0, stroke: tuple | None = None, stroke_width: int = 0) -> float:
    """Draw text with its top at y. align is l/m/r about x. Returns the drawn width."""
    text = str(text)
    width = measure(draw, text, fnt, spacing)
    cursor = x if align == "l" else x - width / 2 if align == "m" else x - width
    baseline = y + fnt.getmetrics()[0]
    for chunk, is_naira in _runs(text):
        face = _naira_font(fnt.size) if is_naira else fnt
        if spacing:
            for char in chunk:
                draw.text((cursor, baseline), char, font=face, fill=fill, anchor="ls", stroke_width=stroke_width, stroke_fill=stroke)
                cursor += draw.textlength(char, font=face) + spacing
        else:
            draw.text((cursor, baseline), chunk, font=face, fill=fill, anchor="ls", stroke_width=stroke_width, stroke_fill=stroke)
            cursor += draw.textlength(chunk, font=face)
    return width


def fit(draw: ImageDraw.ImageDraw, text: str, max_width: float, start: int, minimum: int, maker) -> ImageFont.FreeTypeFont:
    size = start
    while size > minimum and measure(draw, text, maker(size)) > max_width:
        size -= 2
    return maker(size)


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_width: float, max_lines: int) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if measure(draw, trial, fnt) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and measure(draw, lines[-1] + "...", fnt) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "..."
    return lines


# ── Surfaces ─────────────────────────────────────────────────────────────────────────────────
def gradient(width: int, height: int, top: tuple, bottom: tuple) -> Image.Image:
    strip = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(height - 1, 1)
        strip.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return strip.resize((width, height))


def alpha_gradient(width: int, height: int, colour: tuple, start_alpha: int, end_alpha: int, *, horizontal: bool = False) -> Image.Image:
    length = width if horizontal else height
    strip = Image.new("L", (length, 1) if horizontal else (1, length))
    for i in range(length):
        t = i / max(length - 1, 1)
        value = int(start_alpha + (end_alpha - start_alpha) * t)
        if horizontal:
            strip.putpixel((i, 0), value)
        else:
            strip.putpixel((0, i), value)
    mask = strip.resize((width, height))
    layer = Image.new("RGBA", (width, height), colour + (0,))
    layer.putalpha(mask)
    return layer


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def over(canvas: Image.Image, layer: Image.Image, pos: tuple[int, int] = (0, 0)) -> None:
    """Alpha-composite an RGBA layer onto an RGB canvas."""
    canvas.paste(layer.convert("RGB"), pos, layer.getchannel("A"))


def glass(canvas: Image.Image, box: tuple[int, int, int, int], *, radius: int = 34, tint: tuple = (8, 44, 31), alpha: int = 118, blur: int = 26) -> None:
    x0, y0, x1, y1 = box
    region = canvas.crop(box).filter(ImageFilter.GaussianBlur(blur)).convert("RGBA")
    region = Image.alpha_composite(region, Image.new("RGBA", region.size, tint + (alpha,)))
    sheen = alpha_gradient(region.width, region.height, (255, 255, 255), 30, 0)
    region = Image.alpha_composite(region, sheen)
    canvas.paste(region.convert("RGB"), (x0, y0), rounded_mask(region.size, radius))
    d = ImageDraw.Draw(canvas, "RGBA")
    d.rounded_rectangle(box, radius=radius, outline=(255, 255, 255, 46), width=2)


def cover(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


# ── Web Mercator + satellite ─────────────────────────────────────────────────────────────────
def merc(lon: float, lat: float) -> tuple[float, float]:
    lat = max(-85.05, min(85.05, lat))
    s = math.sin(math.radians(lat))
    return (lon + 180.0) / 360.0, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def unmerc(nx: float, ny: float) -> tuple[float, float]:
    return nx * 360.0 - 180.0, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ny))))


@dataclass
class View:
    width: int
    height: int
    centre_nx: float
    centre_ny: float
    zoom: float

    @property
    def scale(self) -> float:
        # final pixels per unit of normalised mercator (512px tiles, @2x)
        return 1024.0 * (2 ** self.zoom)

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        nx, ny = merc(lon, lat)
        return self.width / 2 + (nx - self.centre_nx) * self.scale, self.height / 2 + (ny - self.centre_ny) * self.scale

    def metres_per_pixel(self) -> float:
        lat = unmerc(self.centre_nx, self.centre_ny)[1]
        return 40075016.686 * math.cos(math.radians(lat)) / self.scale


def fit_view(bounds: tuple[float, float, float, float], width: int, height: int, rect: tuple[float, float, float, float], *, max_zoom: float = 19.6) -> View:
    """View whose imagery covers width x height while the given lon/lat bounds fill `rect`."""
    min_lon, min_lat, max_lon, max_lat = bounds
    x0, y0 = merc(min_lon, max_lat)
    x1, y1 = merc(max_lon, min_lat)
    dx, dy = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    rw, rh = max(rect[2] - rect[0], 10), max(rect[3] - rect[1], 10)
    scale = min(rw / dx, rh / dy)
    zoom = max(0.0, min(max_zoom, math.log2(scale / 1024.0)))
    scale = 1024.0 * (2 ** zoom)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    return View(width, height, cx - (rx - width / 2) / scale, cy - (ry - height / 2) / scale, zoom)


_SAT_CACHE: dict[tuple, tuple[float, bytes]] = {}
_SAT_LOCK = threading.Lock()


def _token() -> str:
    return str(os.getenv("MAPBOX_ACCESS_TOKEN") or os.getenv("MAPBOX_TOKEN") or "").strip()


def fetch_satellite(view: View) -> Image.Image | None:
    token = _token()
    if not token:
        return None
    lon, lat = unmerc(view.centre_nx, view.centre_ny)
    lw, lh = (view.width + 1) // 2, (view.height + 1) // 2
    if lw > 1280 or lh > 1280:
        return None
    key = (round(lon, 6), round(lat, 6), round(view.zoom, 3), lw, lh)
    now = time.monotonic()
    with _SAT_LOCK:
        hit = _SAT_CACHE.get(key)
        if hit and now - hit[0] < 6 * 3600:
            return Image.open(io.BytesIO(hit[1])).convert("RGB")
    url = (
        f"https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static/{lon:.6f},{lat:.6f},{view.zoom:.3f},0,0/{lw}x{lh}@2x"
        f"?attribution=false&logo=false&access_token={token}"
    )
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200 or "image" not in str(response.headers.get("content-type", "")):
            return None
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception:
        return None
    with _SAT_LOCK:
        if len(_SAT_CACHE) > 40:
            _SAT_CACHE.clear()
        _SAT_CACHE[key] = (now, response.content)
    if image.size != (view.width, view.height):
        image = image.resize((view.width, view.height), Image.LANCZOS)
    return image


def _grade(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Contrast(image).enhance(1.1)
    image = ImageEnhance.Color(image).enhance(1.06)
    return ImageEnhance.Brightness(image).enhance(0.94)


def _fallback_backdrop(width: int, height: int) -> Image.Image:
    base = gradient(width, height, (12, 52, 38), (5, 24, 17)).convert("RGBA")
    grid = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(grid, "RGBA")
    step = 72
    for x in range(0, width, step):
        d.line([(x, 0), (x, height)], fill=(255, 255, 255, 10), width=1)
    for y in range(0, height, step):
        d.line([(0, y), (width, y)], fill=(255, 255, 255, 10), width=1)
    return Image.alpha_composite(base, grid).convert("RGB")


def short_label(number: str) -> str:
    match = re.fullmatch(r"([A-Za-z]+[-_ ]?)0*(\d+)", str(number).strip())
    return f"{match.group(1)}{match.group(2)}" if match else str(number)


STATUS_STYLE = {
    "available": ((34, 214, 128, 118), (255, 255, 255, 235), 3),
    "reserved": ((255, 186, 60, 138), (255, 255, 255, 225), 3),
    "allocated": ((12, 20, 28, 108), (255, 255, 255, 92), 2),
    "developed": ((12, 20, 28, 108), (255, 255, 255, 92), 2),
    "under_survey": ((12, 20, 28, 108), (255, 255, 255, 92), 2),
    "under_staking": ((12, 20, 28, 108), (255, 255, 255, 92), 2),
    "on_hold": ((110, 122, 138, 96), (255, 255, 255, 100), 2),
}


def _ring_px(view: View, plot: Any) -> list[tuple[float, float]]:
    geom = to_shape(plot.geometry)
    return [view.project(lon, lat) for lon, lat in list(geom.exterior.coords)]


def estate_bounds(ctx: "mr.MarketingContext") -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for plot in ctx.plots:
        b = to_shape(plot.geometry).bounds
        xs += [b[0], b[2]]
        ys += [b[1], b[3]]
    if not xs:
        raise ValueError("This Estate has no approved plots to draw yet")
    plots_box = (min(xs), min(ys), max(xs), max(ys))
    if ctx.estate.boundary is not None:
        b = to_shape(ctx.estate.boundary).bounds
        plots_area = max((plots_box[2] - plots_box[0]) * (plots_box[3] - plots_box[1]), 1e-14)
        # Frame the boundary when it hugs the plots; a far larger boundary would waste the frame.
        if (b[2] - b[0]) * (b[3] - b[1]) <= plots_area * 2.4:
            plots_box = (min(plots_box[0], b[0]), min(plots_box[1], b[1]), max(plots_box[2], b[2]), max(plots_box[3], b[3]))
    return plots_box


def plot_focus_bounds(plot: Any) -> tuple[float, float, float, float]:
    b = to_shape(plot.geometry).bounds
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    nx0, ny0 = merc(b[0], b[3])
    nx1, ny1 = merc(b[2], b[1])
    half = max(nx1 - nx0, ny1 - ny0, 1e-7) * 1.75
    lon0, lat1 = unmerc((nx0 + nx1) / 2 - half, (ny0 + ny1) / 2 - half)
    lon1, lat0 = unmerc((nx0 + nx1) / 2 + half, (ny0 + ny1) / 2 + half)
    return lon0, lat0, lon1, lat1


def render_map(ctx: "mr.MarketingContext", width: int, height: int, *, rect: tuple[float, float, float, float] | None = None, focus: Any | None = None, labels: bool = True) -> tuple[Image.Image, bool]:
    """Satellite (or generated) backdrop with the plots drawn on top. Returns (image, has_imagery)."""
    bounds = plot_focus_bounds(focus) if focus is not None else estate_bounds(ctx)
    rect = rect or (width * 0.06, height * 0.06, width * 0.94, height * 0.94)
    view = fit_view(bounds, width, height, rect, max_zoom=20.4 if focus is not None else 19.6)
    imagery = fetch_satellite(view)
    has_imagery = imagery is not None
    base = _grade(imagery) if has_imagery else _fallback_backdrop(width, height)

    ss = 2
    overlay = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay, "RGBA")

    if not has_imagery:
        for feature in ctx.features:
            try:
                geom = to_shape(feature.geometry)
            except Exception:
                continue
            kind = str(getattr(feature, "feature_type", "") or "")
            parts = list(geom.geoms) if geom.geom_type.startswith("Multi") else [geom]
            for part in parts:
                if part.geom_type == "Polygon":
                    pts = [(x * ss, y * ss) for x, y in (view.project(lon, lat) for lon, lat in part.exterior.coords)]
                    d.polygon(pts, fill=(120, 220, 170, 50) if kind == "open_space" else (140, 190, 235, 46) if kind == "drainage" else (255, 255, 255, 26))
                elif part.geom_type == "LineString":
                    pts = [(x * ss, y * ss) for x, y in (view.project(lon, lat) for lon, lat in part.coords)]
                    d.line(pts, fill=(255, 255, 255, 60), width=max(2, int(view.scale * 4e-8 * ss)))

    focus_id = getattr(focus, "id", None)
    for plot in ctx.plots:
        ring = _ring_px(view, plot)
        pts = [(x * ss, y * ss) for x, y in ring]
        if focus is not None:
            if plot.id == focus_id:
                d.polygon(pts, fill=(34, 214, 128, 150))
                d.line(pts + [pts[0]], fill=(255, 255, 255, 84), width=20, joint="curve")
                d.line(pts + [pts[0]], fill=(255, 255, 255, 255), width=5, joint="curve")
            else:
                d.polygon(pts, fill=(4, 16, 12, 62))
                d.line(pts + [pts[0]], fill=(255, 255, 255, 120), width=2, joint="curve")
            continue
        fill, outline, line_width = STATUS_STYLE.get(plot.commercial_status, STATUS_STYLE["on_hold"])
        d.polygon(pts, fill=fill)
        d.line(pts + [pts[0]], fill=outline, width=line_width, joint="curve")

    if ctx.estate.boundary is not None and focus is None:
        ring = [(x * ss, y * ss) for x, y in (view.project(lon, lat) for lon, lat in to_shape(ctx.estate.boundary).exterior.coords)]
        d.line(ring + [ring[0]], fill=(222, 184, 98, 70), width=14, joint="curve")
        d.line(ring + [ring[0]], fill=GOLD + (255,), width=5, joint="curve")

    if labels:
        candidates = [plot for plot in ctx.plots if (plot.id == focus_id if focus is not None else plot.commercial_status in {"available", "reserved"})]
        for plot in candidates:
            ring = _ring_px(view, plot)
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            pw, ph = (max(xs) - min(xs)) * ss, (max(ys) - min(ys)) * ss
            text = short_label(plot.plot_number)
            size = int(max(min(min(pw, ph) * 0.4, (34 if focus is not None else 24) * ss), 0))
            while size >= 9 * ss:
                fnt = sans(size, "bold")
                if d.textlength(text, font=fnt) <= pw * 0.9 and size * 1.15 <= ph:
                    cx, cy = (min(xs) + max(xs)) * ss / 2, (min(ys) + max(ys)) * ss / 2
                    d.text((cx, cy), text, font=fnt, fill=(255, 255, 255, 255), anchor="mm", stroke_width=max(2, size // 9), stroke_fill=(0, 20, 12, 190))
                    break
                size -= 2

    overlay = overlay.resize((width, height), Image.LANCZOS)
    composed = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    return composed, has_imagery


# ── Layout ───────────────────────────────────────────────────────────────────────────────────
@dataclass
class Spec:
    ctx: "mr.MarketingContext"
    title: str
    kicker: str | None
    tagline: str | None
    location: str | None
    stats: list[tuple[str, str, tuple]]
    price_label: str | None
    price_value: str | None
    chips: list[str]
    cta_title: str
    cta_note: str
    pill_text: str
    pill_colour: tuple
    qr_url: str | None
    hero_builder: Any  # callable(width, height, rect) -> (Image, has_imagery)
    contact: list[str] = field(default_factory=list)
    title_serif: bool = True


def _logo_plate(canvas: Image.Image, ctx: "mr.MarketingContext", x: int, y: int, max_h: int, max_w: int) -> int:
    if not ctx.logo_bytes:
        return 0
    try:
        logo = Image.open(io.BytesIO(ctx.logo_bytes)).convert("RGBA")
        logo.thumbnail((max_w, max_h), Image.LANCZOS)
    except Exception:
        return 0
    plate = Image.new("RGBA", (logo.width + 30, logo.height + 20), (255, 255, 255, 240))
    canvas.paste(plate, (x, y), rounded_mask(plate.size, 16))
    canvas.paste(logo, (x + 15, y + 10), logo)
    return plate.width


def _brand(canvas: Image.Image, d: ImageDraw.ImageDraw, spec: Spec, x: int, y: int, max_w: int, height: int) -> None:
    used = _logo_plate(canvas, spec.ctx, x, y, height, max_w)
    if not used:
        name = spec.ctx.organization_name.upper()
        fnt = fit(d, name, max_w, 34, 20, lambda s: sans(s, "extrabold"))
        T(d, x, y + 6, name, fnt, IVORY, spacing=3)


def _pill(canvas: Image.Image, spec: Spec, right: int, y: int, size: int) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    fnt = sans(size, "bold")
    text = spec.pill_text
    w = int(measure(d, text, fnt, 2)) + 66
    h = int(size * 2.05)
    box = (right - w, y, right, y + h)
    glass(canvas, box, radius=h // 2, tint=(4, 30, 21), alpha=140, blur=14)
    d = ImageDraw.Draw(canvas, "RGBA")
    d.ellipse([box[0] + 22, y + h / 2 - 7, box[0] + 36, y + h / 2 + 7], fill=spec.pill_colour + (255,))
    T(d, box[0] + 46, y + (h - fnt.getmetrics()[0]) / 2 - 3, text, fnt, IVORY, spacing=2)


def _finish(canvas: Image.Image, spec: Spec, has_imagery: bool, footer_y: int, W: int) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    credit = "Imagery © Mapbox © OpenStreetMap © Maxar   ·   " if has_imagery else ""
    T(d, W / 2, footer_y, f"{credit}Availability as of {spec.ctx.stamp}   ·   Powered by LandCheck Estates", sans(19, "medium"), MIST + (170,), align="m")


def _cta(canvas: Image.Image, spec: Spec, box: tuple[int, int, int, int], *, big: bool) -> None:
    x0, y0, x1, y1 = box
    glass(canvas, box, radius=38, tint=(6, 38, 27), alpha=150, blur=28)
    d = ImageDraw.Draw(canvas, "RGBA")
    pad = 40
    d.rounded_rectangle([x0 + pad, y0 + 34, x0 + pad + 64, y0 + 38], radius=2, fill=GOLD + (255,))
    qr_size = 0
    if spec.qr_url:
        qr_size = min(y1 - y0 - 2 * 34, 250 if big else 190)
        plate = Image.new("RGBA", (qr_size + 28, qr_size + 28), (255, 255, 255, 255))
        px = x1 - pad + 6 - plate.width
        py = y0 + (y1 - y0 - plate.height) // 2
        canvas.paste(plate, (px, py), rounded_mask(plate.size, 22))
        canvas.paste(mr.qr_image(spec.qr_url, qr_size, dark=(6, 40, 28)).convert("RGB"), (px + 14, py + 14))
        text_w = px - (x0 + pad) - 28
    else:
        text_w = x1 - x0 - 2 * pad
    ty = y0 + 54
    title_font = fit(d, spec.cta_title, text_w, 46 if big else 38, 26, lambda s: serif(s, "semibold"))
    T(d, x0 + pad, ty, spec.cta_title, title_font, IVORY)
    ty += int(title_font.size * 1.3)
    note_font = sans(26 if big else 22, "medium")
    for line in wrap(d, spec.cta_note, note_font, text_w, 2 if big else 1):
        T(d, x0 + pad, ty, line, note_font, MIST)
        ty += int(note_font.size * 1.45)
    ty += 4
    limit = y1 - 26
    for index, line in enumerate(spec.contact[:3]):
        fnt = fit(d, line, text_w, 30 if index == 0 else 25, 16, lambda s, w="bold" if index == 0 else "medium": sans(s, w))
        step = int(fnt.size * 1.42)
        if ty + step > limit:
            break
        T(d, x0 + pad, ty, line, fnt, GOLD_SOFT if index == 0 else IVORY)
        ty += step


def compose_portrait(spec: Spec, kind: str) -> bytes:
    story = kind == "status"
    W, H = (1080, 1920) if story else (1080, 1350)
    footer_h = 70 if story else 58
    cta_h = 348 if story else 268
    price_h = 112 if story else 96
    stats_h = 150 if story else 126
    margin = 64

    canvas = gradient(W, H, NIGHT, NIGHT_DEEP)
    probe = ImageDraw.Draw(canvas, "RGBA")

    footer_top = H - footer_h
    cta_box = (margin, footer_top - 8 - cta_h, W - margin, footer_top - 8)
    price_top = cta_box[1] - 22 - price_h
    stats_box = (margin, price_top - 18 - stats_h, W - margin, price_top - 18)

    # Title block, measured so the hero can be sized to leave exactly the right room.
    name_size = 108 if story else 92
    title_maker = (lambda s: serif(s, "bold")) if spec.title_serif else (lambda s: sans(s, "extrabold"))
    name_font = fit(probe, spec.title, W - 2 * margin, name_size if spec.title_serif else int(name_size * 0.88), 52, title_maker)
    title_lines = wrap(probe, spec.title, name_font, W - 2 * margin, 2)
    tag_font = sans(33 if story else 30, "medium")
    tag_lines = wrap(probe, spec.tagline or "", tag_font, W - 2 * margin, 2) if spec.tagline else []
    title_h = (44 if spec.kicker else 0) + int(len(title_lines) * name_font.size * 1.06) + (14 + int(len(tag_lines) * tag_font.size * 1.42) if tag_lines else 0) + (56 if spec.location else 0)
    title_top = stats_box[1] - 26 - title_h

    hero_h = min(title_top + 300, int(H * 0.68))
    fade = 330 if story else 270
    rect = (34, 168, W - 34, max(hero_h - fade - 6, 260))
    hero, has_imagery = spec.hero_builder(W, hero_h, rect)
    canvas.paste(hero.convert("RGB"), (0, 0))
    over(canvas, alpha_gradient(W, 260, (3, 14, 10), 175, 0))
    over(canvas, alpha_gradient(W, fade, NIGHT, 0, 255), (0, hero_h - fade))

    d = ImageDraw.Draw(canvas, "RGBA")
    _brand(canvas, d, spec, margin, 58, 480, 78 if story else 66)
    _pill(canvas, spec, W - margin, 58, 21 if story else 19)
    d = ImageDraw.Draw(canvas, "RGBA")

    y = title_top
    if spec.kicker:
        kf = sans(24, "bold")
        T(d, margin, y + 4, spec.kicker.upper(), kf, GOLD, spacing=4)
        y += 44
    for line in title_lines:
        T(d, margin, y, line, name_font, WHITE)
        y += int(name_font.size * 1.06)
    if tag_lines:
        y += 14
        for line in tag_lines:
            T(d, margin, y, line, tag_font, MIST)
            y += int(tag_font.size * 1.42)
    if spec.location:
        y += 12
        d.ellipse([margin, y + 14, margin + 16, y + 30], fill=GOLD + (255,))
        T(d, margin + 32, y + 4, spec.location, sans(28, "semibold"), IVORY)

    glass(canvas, stats_box, radius=34, tint=(6, 38, 27), alpha=132, blur=24)
    d = ImageDraw.Draw(canvas, "RGBA")
    count = max(len(spec.stats), 1)
    col_w = (stats_box[2] - stats_box[0]) / count
    for index, (value, label, colour) in enumerate(spec.stats):
        cx = stats_box[0] + col_w * (index + 0.5)
        if index:
            lx = stats_box[0] + col_w * index
            d.line([(lx, stats_box[1] + 30), (lx, stats_box[3] - 30)], fill=(255, 255, 255, 46), width=2)
        vf = fit(d, value, col_w - 34, 66 if story else 50, 26, lambda s: sans(s, "extrabold"))
        T(d, cx, stats_box[1] + (22 if story else 16), value, vf, colour, align="m")
        T(d, cx, stats_box[3] - (42 if story else 36), label.upper(), sans(19 if story else 17, "bold"), MIST, align="m", spacing=3)

    if spec.price_value:
        if spec.price_label:
            T(d, margin + 4, price_top + 6, spec.price_label.upper(), sans(21, "bold"), GOLD, spacing=4)
        pf = fit(d, spec.price_value, 520, 66 if story else 58, 34, lambda s: sans(s, "extrabold"))
        T(d, margin + 2, price_top + 34, spec.price_value, pf, GOLD_SOFT)
    chip_font = sans(21, "bold")
    right = W - margin
    chip_y = price_top + 30
    for chip in reversed(spec.chips[:3]):
        cw = int(measure(d, chip, chip_font)) + 40
        if right - cw < margin + 380:
            break
        d.rounded_rectangle([right - cw, chip_y, right, chip_y + 50], radius=25, outline=GOLD + (210,), width=2, fill=(222, 184, 98, 30))
        T(d, right - cw / 2, chip_y + 11, chip, chip_font, GOLD_SOFT, align="m")
        right -= cw + 12

    _cta(canvas, spec, cta_box, big=story)
    _finish(canvas, spec, has_imagery, footer_top + 24, W)
    return _png(canvas)


def compose_landscape(spec: Spec) -> bytes:
    W, H = 1200, 630
    canvas = gradient(W, H, NIGHT, NIGHT_DEEP)
    hero, has_imagery = spec.hero_builder(W, H, (500, 60, W - 50, H - 60))
    canvas.paste(hero.convert("RGB"), (0, 0))
    over(canvas, alpha_gradient(820, H, NIGHT_DEEP, 250, 0, horizontal=True))
    over(canvas, alpha_gradient(W, 140, (3, 14, 10), 150, 0))
    over(canvas, alpha_gradient(W, 160, (3, 14, 10), 0, 170), (0, H - 160))
    d = ImageDraw.Draw(canvas, "RGBA")

    left = 56
    _brand(canvas, d, spec, left, 40, 340, 50)
    _pill(canvas, spec, W - 44, 40, 16)
    d = ImageDraw.Draw(canvas, "RGBA")

    y = 122
    if spec.kicker:
        T(d, left, y, spec.kicker.upper(), sans(19, "bold"), GOLD, spacing=3)
        y += 34
    name_font = fit(d, spec.title, 560, 66 if spec.title_serif else 58, 34, (lambda s: serif(s, "bold")) if spec.title_serif else (lambda s: sans(s, "extrabold")))
    for line in wrap(d, spec.title, name_font, 560, 2):
        T(d, left, y, line, name_font, WHITE)
        y += int(name_font.size * 1.08)
    if spec.tagline:
        tf = sans(23, "medium")
        for line in wrap(d, spec.tagline, tf, 520, 2):
            T(d, left, y + 8, line, tf, MIST)
            y += int(tf.size * 1.4)
        y += 8
    if spec.location:
        d.ellipse([left, y + 16, left + 12, y + 28], fill=GOLD + (255,))
        T(d, left + 24, y + 8, spec.location, sans(21, "semibold"), IVORY)
        y += 40

    sx = left
    for value, label, colour in spec.stats:
        vf = sans(38, "extrabold")
        w1 = T(d, sx, H - 200, value, vf, colour)
        T(d, sx, H - 152, label.upper(), sans(14, "bold"), MIST, spacing=2)
        sx += int(max(w1, measure(d, label.upper(), sans(14, "bold"), 2)) + 34)
    if spec.price_value:
        if spec.price_label:
            T(d, left, H - 118, spec.price_label.upper(), sans(15, "bold"), GOLD, spacing=3)
        T(d, left, H - 96, spec.price_value, fit(d, spec.price_value, 320, 46, 26, lambda s: sans(s, "extrabold")), GOLD_SOFT)

    if spec.qr_url:
        qr_size = 118
        box = (W - 44 - qr_size - 28, H - 44 - qr_size - 28, W - 44, H - 44)
        glass(canvas, (box[0] - 190, box[1] - 4, box[2] + 0, box[3] + 4), radius=26, tint=(6, 38, 27), alpha=150, blur=20)
        d = ImageDraw.Draw(canvas, "RGBA")
        plate = Image.new("RGBA", (qr_size + 20, qr_size + 20), (255, 255, 255, 255))
        canvas.paste(plate, (box[2] - plate.width - 8, box[1] + 4 + (box[3] - box[1] - 8 - plate.height) // 2), rounded_mask(plate.size, 16))
        canvas.paste(mr.qr_image(spec.qr_url, qr_size, dark=(6, 40, 28)).convert("RGB"), (box[2] - plate.width - 8 + 10, box[1] + 4 + (box[3] - box[1] - 8 - plate.height) // 2 + 10))
        T(d, box[0] - 190 + 24, box[1] + 30, "Scan for the", sans(17, "semibold"), MIST)
        T(d, box[0] - 190 + 24, box[1] + 52, "live map", serif(30, "semibold"), IVORY)
        T(d, box[0] - 190 + 24, box[1] + 100, (spec.contact[0] if spec.contact else ""), sans(16, "bold"), GOLD_SOFT)
    else:
        label = "Open the live map"
        f = sans(20, "bold")
        w = int(measure(d, label, f, 1)) + 84
        box = (W - 44 - w, H - 44 - 58, W - 44, H - 44)
        glass(canvas, box, radius=29, tint=(6, 38, 27), alpha=160, blur=16)
        d = ImageDraw.Draw(canvas, "RGBA")
        T(d, box[0] + 30, box[1] + 15, label, f, IVORY, spacing=1)
        d.polygon([(box[2] - 42, box[1] + 22), (box[2] - 26, box[1] + 29), (box[2] - 42, box[1] + 36)], fill=GOLD + (255,))
    if has_imagery:
        T(d, W - 44, 14, "Imagery © Mapbox © OpenStreetMap © Maxar", sans(12, "medium"), (255, 255, 255, 130), align="r")
    return _png(canvas)


def _png(canvas: Image.Image) -> bytes:
    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# ── Public entry points ──────────────────────────────────────────────────────────────────────
def _plan_chips(ctx: "mr.MarketingContext") -> list[str]:
    percentages = [f"{float(item.get('percentage')):g}" for item in ctx.payment_plan[:4] if item.get("percentage") is not None]
    return ["Pay " + " · ".join(f"{value}%" for value in percentages)] if percentages else []


def _contact(ctx: "mr.MarketingContext") -> list[str]:
    lines = []
    if ctx.agent_name:
        lines.append(ctx.agent_name)
    if ctx.contact_phone:
        lines.append(ctx.contact_phone)
    lines.append(re.sub(r"^https?://", "", ctx.page_url).split("?")[0])
    return lines


def estate_ad(ctx: "mr.MarketingContext", kind: str, *, qr: bool = True) -> bytes:
    stats = [(str(ctx.available), "Available", (94, 232, 160)), (str(ctx.reserved), "Reserved", (255, 206, 110)), (str(ctx.sold), "Sold", (160, 200, 255))]
    spec = Spec(
        ctx=ctx, title=ctx.estate.name, kicker=None, tagline=ctx.estate.public_tagline, location=ctx.location, stats=stats,
        price_label="Plots from" if ctx.min_price else None, price_value=mr.naira_short(ctx.min_price) if (ctx.min_price and ctx.show_prices) else None,
        chips=_plan_chips(ctx), cta_title="Scan to explore the live map", cta_note="Choose your plot and reserve online.",
        pill_text="LIVE AVAILABILITY", pill_colour=(94, 232, 160), qr_url=ctx.page_url if qr else None,
        hero_builder=lambda w, h, rect: render_map(ctx, w, h, rect=rect), contact=_contact(ctx),
    )
    return compose_landscape(spec) if kind == "landscape" else compose_portrait(spec, kind)


def plot_ad(ctx: "mr.MarketingContext", plot: Any, kind: str, *, qr: bool = True) -> bytes:
    status = plot.commercial_status
    status_text = mr.STATUS_LABEL.get(status, "Not available")
    colour = {"available": (94, 232, 160), "reserved": (255, 206, 110)}.get(status, (160, 200, 255))
    price = mr.naira(plot.asking_price) if (ctx.show_prices and plot.asking_price is not None) else None
    stats = [(mr.area_text(plot.area_sqm), "Size", IVORY), (status_text, "Status", colour), (mr.naira_short(plot.asking_price) if price else "On request", "Price", GOLD_SOFT)]
    spec = Spec(
        ctx=ctx, title=f"Plot {plot.plot_number}", kicker=ctx.estate.name, tagline=(plot.public_address or ctx.estate.public_tagline), location=ctx.location, stats=stats,
        price_label="Asking price" if price else None, price_value=price,
        chips=_plan_chips(ctx), cta_title="Reserve this plot" if status == "available" else "See what is still available",
        cta_note="Scan to see it on the live map and reserve online." if status == "available" else "This plot is taken - scan to browse the plots that are open.",
        pill_text=status_text.upper(), pill_colour=colour, qr_url=ctx.plot_url(plot) if qr else None,
        hero_builder=lambda w, h, rect: render_map(ctx, w, h, rect=rect, focus=plot), contact=_contact(ctx), title_serif=False,
    )
    return compose_landscape(spec) if kind == "landscape" else compose_portrait(spec, kind)


def document_map(ctx: "mr.MarketingContext", width: int, height: int, *, cover_page: bool = False) -> bytes:
    inner = (width * 0.05, height * 0.06, width * 0.95, height * 0.94)
    image, _has = render_map(ctx, width, height, rect=inner)
    if cover_page:
        over(image, alpha_gradient(width, int(height * 0.5), NIGHT_DEEP, 0, 235), (0, height - int(height * 0.5)))
    return _png(image)


def hero_banner(ctx: "mr.MarketingContext", width: int, height: int) -> bytes:
    """Satellite banner with the estate name over it - the header of the flyer."""
    image, has_imagery = render_map(ctx, width, height, rect=(width * 0.05, height * 0.1, width * 0.95, height * 0.6), labels=False)
    canvas = image.convert("RGB")
    over(canvas, alpha_gradient(width, int(height * 0.32), (3, 14, 10), 190, 0))
    over(canvas, alpha_gradient(width, int(height * 0.62), NIGHT, 0, 250), (0, height - int(height * 0.62)))
    d = ImageDraw.Draw(canvas, "RGBA")
    sc = width / 1190
    name = ctx.organization_name.upper()
    nf = fit(d, name, width * 0.5, int(30 * sc), 14, lambda z: sans(z, "extrabold"))
    T(d, int(56 * sc), int(48 * sc), name, nf, IVORY, spacing=3 * sc)
    pf = sans(int(18 * sc), "bold")
    label = "AVAILABLE NOW"
    pw = int(measure(d, label, pf, 2 * sc)) + int(60 * sc)
    ph = int(46 * sc)
    box = (width - int(56 * sc) - pw, int(38 * sc), width - int(56 * sc), int(38 * sc) + ph)
    glass(canvas, box, radius=ph // 2, tint=(4, 30, 21), alpha=150, blur=12)
    d = ImageDraw.Draw(canvas, "RGBA")
    d.ellipse([box[0] + int(20 * sc), box[1] + ph // 2 - int(6 * sc), box[0] + int(32 * sc), box[1] + ph // 2 + int(6 * sc)], fill=(94, 232, 160, 255))
    T(d, box[0] + int(42 * sc), box[1] + (ph - pf.getmetrics()[0]) // 2 - 2, label, pf, IVORY, spacing=2 * sc)
    y = int(height * 0.66)
    tf = fit(d, ctx.estate.name, width - int(112 * sc), int(92 * sc), 40, lambda z: serif(z, "bold"))
    for line in wrap(d, ctx.estate.name, tf, width - int(112 * sc), 2):
        T(d, int(56 * sc), y, line, tf, WHITE)
        y += int(tf.size * 1.05)
    if ctx.estate.public_tagline:
        gf = sans(int(26 * sc), "medium")
        T(d, int(56 * sc), y + int(8 * sc), ctx.estate.public_tagline[:110], gf, MIST)
        y += int(gf.size * 1.6)
    if ctx.location:
        d.ellipse([int(56 * sc), y + int(14 * sc), int(56 * sc) + int(12 * sc), y + int(26 * sc)], fill=GOLD + (255,))
        T(d, int(56 * sc) + int(24 * sc), y + int(6 * sc), ctx.location, sans(int(22 * sc), "semibold"), IVORY)
    if has_imagery:
        T(d, width - int(20 * sc), height - int(26 * sc), "Imagery \u00a9 Mapbox \u00a9 OpenStreetMap \u00a9 Maxar", sans(max(10, int(12 * sc)), "medium"), (255, 255, 255, 120), align="r")
    return _png(canvas)


def cover_page(ctx: "mr.MarketingContext", width: int, height: int) -> bytes:
    """Full-bleed brochure cover: satellite estate, serif title, gold rule."""
    image, has_imagery = render_map(ctx, width, height, rect=(width * 0.07, height * 0.16, width * 0.93, height * 0.56), labels=False)
    canvas = image.convert("RGB")
    over(canvas, alpha_gradient(width, int(height * 0.22), (3, 14, 10), 200, 0))
    over(canvas, alpha_gradient(width, int(height * 0.5), NIGHT_DEEP, 0, 252), (0, height - int(height * 0.5)))
    d = ImageDraw.Draw(canvas, "RGBA")
    sc = width / 1190
    name = ctx.organization_name.upper()
    nf = fit(d, name, width * 0.6, int(34 * sc), 16, lambda z: sans(z, "extrabold"))
    T(d, int(70 * sc), int(70 * sc), name, nf, IVORY, spacing=4 * sc)
    y = int(height * 0.66)
    d.rounded_rectangle([int(70 * sc), y - int(30 * sc), int(70 * sc) + int(90 * sc), y - int(25 * sc)], radius=3, fill=GOLD + (255,))
    tf = fit(d, ctx.estate.name, width - int(140 * sc), int(128 * sc), 60, lambda z: serif(z, "bold"))
    for line in wrap(d, ctx.estate.name, tf, width - int(140 * sc), 3):
        T(d, int(70 * sc), y, line, tf, WHITE)
        y += int(tf.size * 1.05)
    if ctx.estate.public_tagline:
        gf = sans(int(34 * sc), "medium")
        for line in wrap(d, ctx.estate.public_tagline, gf, width - int(160 * sc), 2):
            T(d, int(70 * sc), y + int(14 * sc), line, gf, MIST)
            y += int(gf.size * 1.5)
    if ctx.location:
        d.ellipse([int(70 * sc), y + int(34 * sc), int(70 * sc) + int(16 * sc), y + int(50 * sc)], fill=GOLD + (255,))
        T(d, int(70 * sc) + int(32 * sc), y + int(24 * sc), ctx.location, sans(int(28 * sc), "semibold"), IVORY)
    fy = height - int(150 * sc)
    stats = [(str(ctx.available), "Available", (94, 232, 160)), (str(ctx.reserved), "Reserved", (255, 206, 110)), (str(ctx.sold), "Sold", (160, 200, 255))]
    sx = int(70 * sc)
    for value, label, colour in stats:
        vf = sans(int(54 * sc), "extrabold")
        w1 = T(d, sx, fy, value, vf, colour)
        T(d, sx, fy + int(64 * sc), label.upper(), sans(int(16 * sc), "bold"), MIST, spacing=3 * sc)
        sx += int(max(w1, measure(d, label.upper(), sans(int(16 * sc), "bold"), 3 * sc)) + 60 * sc)
    credit = "   \u00b7   Imagery \u00a9 Mapbox \u00a9 OpenStreetMap \u00a9 Maxar" if has_imagery else ""
    T(d, width - int(70 * sc), height - int(70 * sc), f"Availability as of {ctx.stamp}{credit}", sans(int(15 * sc), "medium"), MIST + (190,), align="r")
    return _png(canvas)
