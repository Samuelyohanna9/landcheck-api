from __future__ import annotations

"""Live marketing material for an Estate: layout maps, social ads, per-plot ads and share cards.

Everything here is rendered on demand from current database state, so a flyer downloaded today
already reflects the plots sold yesterday - nothing is stored or goes stale. Rendering uses Pillow
and matplotlib's thread-safe object API (never pyplot), because FastAPI runs these sync endpoints
in a thread pool.
"""

import io
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import quote

import matplotlib
import requests
from geoalchemy2.shape import to_shape
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Polygon as MplPolygon
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from shapely.geometry import Polygon, box, mapping
from shapely.ops import transform as shapely_transform
from sqlalchemy.orm import Session

from app.models.estate_foundation import (
    Estate,
    EstateOrganization,
    EstateOrganizationMember,
    EstatePlot,
    EstateQrCampaign,
    EstateSpatialFeature,
)
from app.routers.plots import _metric_epsg_for_wgs84_polygon
from app.services.estates.documents import read_private_estate_file
from app.services.estates.marketing_common import (
    agent_member_for,
    normalize_phone_digits,
    public_page_url,
    resolve_campaign,
    share_page_url,
)

# ── Brand palette (matches the Estate dashboard) ─────────────────────────────────────────────
BRAND = (26, 143, 90)
BRAND_DARK = (15, 110, 68)
DEEP = (9, 52, 36)
DEEPER = (5, 33, 23)
TINT = (230, 246, 236)
INK = (16, 24, 39)
MUTED = (107, 118, 133)
WHITE = (255, 255, 255)
AMBER = (214, 145, 0)
BLUE = (42, 120, 214)
SLATE = (102, 112, 133)
MINT = (168, 232, 196)

STATUS_RGB = {
    "available": BRAND,
    "reserved": AMBER,
    "allocated": BLUE,
    "developed": (15, 139, 141),
    "under_survey": (217, 98, 42),
    "under_staking": (96, 70, 168),
    "on_hold": SLATE,
}
STATUS_LABEL = {
    "available": "Available",
    "reserved": "Reserved",
    "allocated": "Sold",
    "developed": "Sold",
    "under_survey": "Sold",
    "under_staking": "Sold",
    "on_hold": "Not available",
}
VISIBLE_STATUSES = {"available", "reserved", "allocated", "on_hold", "under_survey", "under_staking", "developed"}
SOLD_STATUSES = {"allocated", "developed", "under_survey", "under_staking"}


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


# ── Fonts ────────────────────────────────────────────────────────────────────────────────────
_FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
REGULAR_TTF = os.path.join(_FONT_DIR, "DejaVuSans.ttf")
BOLD_TTF = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(BOLD_TTF if bold else REGULAR_TTF, int(size))


# ── Formatting ───────────────────────────────────────────────────────────────────────────────
def naira(value: Any) -> str:
    try:
        return f"₦{Decimal(str(value)):,.0f}"
    except Exception:
        return "Price on request"


def naira_short(value: Any) -> str:
    try:
        amount = float(value)
    except Exception:
        return "Price on request"
    if amount >= 1_000_000_000:
        return f"₦{amount / 1_000_000_000:.1f}B".replace(".0B", "B")
    if amount >= 1_000_000:
        return f"₦{amount / 1_000_000:.1f}M".replace(".0M", "M")
    if amount >= 1_000:
        return f"₦{amount / 1_000:.0f}K"
    return f"₦{amount:,.0f}"


def area_text(area_sqm: Any) -> str:
    try:
        return f"{float(area_sqm):,.0f} m²"
    except Exception:
        return "Area on request"


def _natural_key(value: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value or ""))]


# ── Context ──────────────────────────────────────────────────────────────────────────────────
@dataclass
class MarketingContext:
    estate: Estate
    organization_name: str
    plots: list[EstatePlot]
    features: list[Any]
    counts: dict[str, int]
    available_plots: list[EstatePlot]
    show_prices: bool
    min_price: Decimal | None
    max_price: Decimal | None
    payment_plan: list[dict]
    location: str | None
    source: str | None
    agent_name: str | None
    contact_phone: str | None
    whatsapp_digits: str | None
    page_url: str
    logo_bytes: bytes | None
    forecast: dict | None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def sold(self) -> int:
        return sum(self.counts.get(status, 0) for status in SOLD_STATUSES)

    @property
    def available(self) -> int:
        return self.counts.get("available", 0)

    @property
    def reserved(self) -> int:
        return self.counts.get("reserved", 0)

    @property
    def stamp(self) -> str:
        return self.generated_at.strftime("%d %b %Y")

    def plot_url(self, plot: EstatePlot) -> str:
        return public_page_url(self.estate, source=self.source, plot_id=plot.id)


@lru_cache(maxsize=32)
def _logo_bytes(object_key: str) -> bytes | None:
    try:
        data, _mime = read_private_estate_file(object_key)
        return data
    except Exception:
        return None


def build_context(db: Session, estate: Estate, *, source: str | None = None) -> MarketingContext:
    organization = db.get(EstateOrganization, estate.organization_id)
    plots = [
        plot
        for plot in db.query(EstatePlot)
        .filter(EstatePlot.estate_id == estate.id, EstatePlot.geometry_status == "approved")
        .all()
        if plot.geometry is not None and plot.commercial_status in VISIBLE_STATUSES
    ]
    plots.sort(key=lambda plot: _natural_key(plot.plot_number))
    features = db.query(EstateSpatialFeature).filter(EstateSpatialFeature.estate_id == estate.id).all()
    counts: dict[str, int] = {}
    for plot in plots:
        counts[plot.commercial_status] = counts.get(plot.commercial_status, 0) + 1
    available_plots = [plot for plot in plots if plot.commercial_status == "available"]
    show_prices = bool(estate.public_show_prices)
    prices = [Decimal(str(plot.asking_price)) for plot in available_plots if plot.asking_price is not None]
    campaign = resolve_campaign(db, estate.id, source)
    member = agent_member_for(db, campaign)
    agent_name = member.subject_id if member else None
    whatsapp = normalize_phone_digits((member.contact_phone if member and member.contact_phone else None) or estate.public_whatsapp_number or estate.public_contact_phone)
    forecast = estate.public_development_forecast if isinstance(estate.public_development_forecast, dict) and estate.public_development_forecast.get("published") else None
    return MarketingContext(
        estate=estate,
        organization_name=organization.name if organization else "Estate team",
        plots=plots,
        features=features,
        counts=counts,
        available_plots=available_plots,
        show_prices=show_prices,
        min_price=min(prices) if (prices and show_prices) else None,
        max_price=max(prices) if (prices and show_prices) else None,
        payment_plan=list(estate.public_payment_plan or []),
        location=estate.location_text or ", ".join(filter(None, [estate.locality, estate.state])) or None,
        source=campaign.code if campaign else None,
        agent_name=agent_name,
        contact_phone=(member.contact_phone if member and member.contact_phone else None) or estate.public_contact_phone,
        whatsapp_digits=whatsapp,
        page_url=public_page_url(estate, source=campaign.code if campaign else None),
        logo_bytes=_logo_bytes(estate.public_logo_object_key) if estate.public_logo_object_key else None,
        forecast=forecast,
    )


# ── Small render cache ───────────────────────────────────────────────────────────────────────
_CACHE: dict[Any, tuple[float, bytes]] = {}
_CACHE_LOCK = threading.Lock()


def cached_render(key: Any, ttl_seconds: int, builder: Callable[[], bytes]) -> bytes:
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl_seconds:
            return hit[1]
    data = builder()
    with _CACHE_LOCK:
        if len(_CACHE) > 200:
            for stale in [item for item, value in _CACHE.items() if now - value[0] > ttl_seconds]:
                _CACHE.pop(stale, None)
            if len(_CACHE) > 200:
                _CACHE.clear()
        _CACHE[key] = (now, data)
    return data


def context_signature(ctx: MarketingContext) -> tuple:
    return (
        ctx.estate.id,
        tuple(sorted(ctx.counts.items())),
        ctx.min_price,
        ctx.estate.updated_at.isoformat() if ctx.estate.updated_at else None,
        ctx.source,
        len(ctx.plots),
    )


# ── QR ───────────────────────────────────────────────────────────────────────────────────────
def qr_image(text: str, size: int, *, dark: tuple = INK, light: tuple = WHITE) -> Image.Image:
    from reportlab.graphics.barcode import qrencoder

    qr = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    qr.addData(text)
    qr.make()
    count = qr.getModuleCount()
    border = 2
    unit = max(1, size // (count + border * 2))
    dim = unit * (count + border * 2)
    image = Image.new("RGB", (dim, dim), light)
    draw = ImageDraw.Draw(image)
    for row in range(count):
        for col in range(count):
            if qr.isDark(row, col):
                x0 = (col + border) * unit
                y0 = (row + border) * unit
                draw.rectangle([x0, y0, x0 + unit - 1, y0 + unit - 1], fill=dark)
    return image.resize((size, size), Image.NEAREST)


# ── Layout map (matplotlib, thread-safe) ─────────────────────────────────────────────────────
def draw_layout_png(
    ctx: MarketingContext,
    *,
    width: int,
    height: int,
    background: tuple = WHITE,
    focus_plot: EstatePlot | None = None,
    labels: bool = True,
) -> bytes:
    """Status-coloured plot map. Available plots are vivid; sold plots recede so availability reads
    at a glance. With `focus_plot` the view zooms to that plot and its neighbours."""
    geoms = [(plot, to_shape(plot.geometry)) for plot in ctx.plots]
    if not geoms:
        raise ValueError("This Estate has no approved plots to draw yet")
    envelope = box(
        min(g.bounds[0] for _, g in geoms),
        min(g.bounds[1] for _, g in geoms),
        max(g.bounds[2] for _, g in geoms),
        max(g.bounds[3] for _, g in geoms),
    )
    epsg = _metric_epsg_for_wgs84_polygon(envelope)
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
    metric = [(plot, shapely_transform(forward, geom)) for plot, geom in geoms]
    feature_metric = []
    for feature in ctx.features:
        try:
            feature_metric.append((feature, shapely_transform(forward, to_shape(feature.geometry))))
        except Exception:
            continue

    if focus_plot is not None:
        focus_geom = next((geom for plot, geom in metric if plot.id == focus_plot.id), None)
    else:
        focus_geom = None
    if focus_geom is not None:
        cx, cy = focus_geom.centroid.x, focus_geom.centroid.y
        fw = focus_geom.bounds[2] - focus_geom.bounds[0]
        fh = focus_geom.bounds[3] - focus_geom.bounds[1]
        half = max(fw, fh, 12.0) * 1.9
        ratio = width / height
        minx, maxx = cx - half * ratio, cx + half * ratio
        miny, maxy = cy - half, cy + half
    else:
        minx = min(geom.bounds[0] for _, geom in metric)
        miny = min(geom.bounds[1] for _, geom in metric)
        maxx = max(geom.bounds[2] for _, geom in metric)
        maxy = max(geom.bounds[3] for _, geom in metric)
        span_x, span_y = maxx - minx, maxy - miny
        pad = max(span_x, span_y) * 0.05 + 1
        minx, maxx, miny, maxy = minx - pad, maxx + pad, miny - pad, maxy + pad
        ratio = width / height
        cur_ratio = (maxx - minx) / max(maxy - miny, 1e-6)
        if cur_ratio > ratio:
            extra = ((maxx - minx) / ratio - (maxy - miny)) / 2
            miny, maxy = miny - extra, maxy + extra
        else:
            extra = ((maxy - miny) * ratio - (maxx - minx)) / 2
            minx, maxx = minx - extra, maxx + extra

    dpi = 100
    fig = Figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor=_hex(background))
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(_hex(background))
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.axis("off")
    window = box(minx, miny, maxx, maxy)
    ppm = width / max(maxx - minx, 1e-6)

    for feature, geom in feature_metric:
        if not geom.intersects(window):
            continue
        kind = str(getattr(feature, "feature_type", "") or "")
        face = "#bcdcee" if kind == "drainage" else "#cdeedb" if kind == "open_space" else "#dfe3e8"
        parts = list(geom.geoms) if geom.geom_type.startswith("Multi") else [geom]
        for part in parts:
            if part.geom_type == "Polygon":
                ax.add_patch(MplPolygon(list(part.exterior.coords), closed=True, facecolor=face, edgecolor="none", zorder=1))
            elif part.geom_type == "LineString":
                xs, ys = zip(*part.coords)
                ax.plot(xs, ys, color="#c5cbd3", linewidth=max(1.0, 5 * ppm * 0.6), solid_capstyle="round", zorder=1)

    for plot, geom in metric:
        if not geom.intersects(window):
            continue
        status = plot.commercial_status
        is_focus = focus_plot is not None and plot.id == focus_plot.id
        if focus_plot is not None:
            face = _hex(BRAND) if is_focus else "#e4e8ec"
            alpha = 1.0
        else:
            rgb = STATUS_RGB.get(status, SLATE)
            face = _hex(rgb) if status in {"available", "reserved"} else "#cfd6de"
            alpha = 0.95
        ax.add_patch(
            MplPolygon(list(geom.exterior.coords), closed=True, facecolor=face, edgecolor="white" if not is_focus else _hex(AMBER), linewidth=0.8 if not is_focus else 2.6, alpha=alpha, zorder=3 if not is_focus else 5)
        )
        if labels and (focus_plot is None or is_focus):
            size_px = math.sqrt(max(geom.area, 0.0)) * ppm
            if size_px >= 30:
                fontsize = max(5.5, min(17.0, size_px * 0.27 / (dpi / 72)))
                colour = "white" if (status in {"available", "reserved"} or is_focus) else "#6b7685"
                ax.text(geom.centroid.x, geom.centroid.y, str(plot.plot_number), ha="center", va="center", fontsize=fontsize, color=colour, fontweight="bold", zorder=6, clip_on=True)

    if ctx.estate.boundary is not None and focus_plot is None:
        try:
            boundary = shapely_transform(forward, to_shape(ctx.estate.boundary))
            ax.add_patch(MplPolygon(list(boundary.exterior.coords), closed=True, facecolor="none", edgecolor="#d1332b", linewidth=1.6, zorder=7))
        except Exception:
            pass

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=dpi, facecolor=_hex(background))
    return buffer.getvalue()


# ── Satellite crop (Mapbox static) ───────────────────────────────────────────────────────────
def _mapbox_token() -> str:
    return str(os.getenv("MAPBOX_ACCESS_TOKEN") or os.getenv("MAPBOX_TOKEN") or "").strip()


def fetch_plot_satellite(plot: EstatePlot, width: int, height: int) -> Image.Image | None:
    token = _mapbox_token()
    if not token:
        return None
    try:
        polygon = to_shape(plot.geometry)
        minx, miny, maxx, maxy = polygon.bounds
        lat = (miny + maxy) / 2
        m_per_deg_lat = 111_320.0
        m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(lat)), 0.2)
        w_m = (maxx - minx) * m_per_deg_lon
        h_m = (maxy - miny) * m_per_deg_lat
        aspect = width / height
        half_h = max(h_m, w_m / aspect, 14.0) * 1.7 / 2 * 2
        half_w = half_h * aspect
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
        bbox = [cx - half_w / m_per_deg_lon, cy - half_h / m_per_deg_lat, cx + half_w / m_per_deg_lon, cy + half_h / m_per_deg_lat]
        overlay = {
            "type": "Feature",
            "properties": {"stroke": "#ffffff", "stroke-width": 3, "stroke-opacity": 1, "fill": "#1a8f5a", "fill-opacity": 0.32},
            "geometry": {"type": "Polygon", "coordinates": [[[round(x, 6), round(y, 6)] for x, y in polygon.exterior.coords]]},
        }
        bbox_text = "[" + ",".join(f"{value:.6f}" for value in bbox) + "]"
        scale_w, scale_h = min(width, 1280), min(height, 1280)
        url = (
            "https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static/"
            f"geojson({quote(json.dumps(overlay, separators=(',', ':')), safe='')})/{bbox_text}/{scale_w}x{scale_h}@2x"
            f"?padding=0&access_token={token}"
        )
        response = requests.get(url, timeout=8)
        if response.status_code != 200 or "image" not in str(response.headers.get("content-type", "")):
            return None
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
        return image.resize((width, height), Image.LANCZOS)
    except Exception:
        return None


# ── Drawing primitives ───────────────────────────────────────────────────────────────────────
def _gradient(width: int, height: int, top: tuple, bottom: tuple) -> Image.Image:
    base = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(base)
    for y in range(height):
        t = y / max(height - 1, 1)
        draw.line([(0, y), (width, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return base


def _rounded_paste(canvas: Image.Image, image: Image.Image, box_xy: tuple[int, int, int, int], radius: int) -> None:
    x0, y0, x1, y1 = box_xy
    image = image.resize((x1 - x0, y1 - y0), Image.LANCZOS)
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, image.size[0] - 1, image.size[1] - 1], radius=radius, fill=255)
    canvas.paste(image, (x0, y0), mask)


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int, max_lines: int) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "...", font=fnt) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "..."
    return lines


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start: int, minimum: int, bold: bool = True):
    size = start
    while size > minimum and draw.textlength(text, font=font(size, bold)) > max_width:
        size -= 2
    return font(size, bold)


def _pill(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, fnt, fill: tuple, ink: tuple, pad_x: int = 22, pad_y: int = 10, anchor_right: bool = False) -> tuple[int, int, int, int]:
    width = int(draw.textlength(text, font=fnt)) + pad_x * 2
    ascent, descent = fnt.getmetrics()
    height = ascent + descent + pad_y * 2
    x, y = xy
    if anchor_right:
        x -= width
    draw.rounded_rectangle([x, y, x + width, y + height], radius=height // 2, fill=fill)
    draw.text((x + pad_x, y + pad_y), text, font=fnt, fill=ink)
    return (x, y, x + width, y + height)


def _logo_image(ctx: MarketingContext, max_h: int, max_w: int) -> Image.Image | None:
    if not ctx.logo_bytes:
        return None
    try:
        logo = Image.open(io.BytesIO(ctx.logo_bytes)).convert("RGBA")
        logo.thumbnail((max_w, max_h), Image.LANCZOS)
        return logo
    except Exception:
        return None


def _brand_row(canvas: Image.Image, draw: ImageDraw.ImageDraw, ctx: MarketingContext, x: int, y: int, max_w: int, height: int) -> None:
    logo = _logo_image(ctx, height, max_w)
    if logo is not None:
        plate = Image.new("RGBA", (logo.width + 24, logo.height + 16), (255, 255, 255, 255))
        mask = Image.new("L", plate.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, plate.width - 1, plate.height - 1], radius=14, fill=255)
        canvas.paste(plate.convert("RGB"), (x, y), mask)
        canvas.paste(logo, (x + 12, y + 8), logo)
    else:
        fnt = _fit_font(draw, ctx.organization_name, max_w, 40, 24)
        draw.text((x, y + 4), ctx.organization_name, font=fnt, fill=WHITE)


def _contact_line(ctx: MarketingContext) -> str:
    parts = []
    if ctx.agent_name:
        parts.append(ctx.agent_name)
    if ctx.contact_phone:
        parts.append(ctx.contact_phone)
    return "  ·  ".join(parts) if parts else ctx.organization_name


def _short_url(ctx: MarketingContext) -> str:
    return re.sub(r"^https?://", "", ctx.page_url).split("?")[0]


PORTRAIT_SIZES = {"status": (1080, 1920), "post": (1080, 1350)}
LANDSCAPE_SIZE = (1200, 630)
AD_KINDS = {"status", "post", "landscape"}


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _stat_card(draw: ImageDraw.ImageDraw, box_xy: tuple[int, int, int, int], value: str, label: str, accent: tuple, value_size: int, label_size: int) -> None:
    x0, y0, x1, y1 = box_xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=26, fill=(17, 84, 58))
    vf = font(value_size, True)
    lf = font(label_size)
    draw.text(((x0 + x1) / 2, y0 + (y1 - y0) * 0.36), value, font=vf, fill=accent, anchor="mm")
    draw.text(((x0 + x1) / 2, y0 + (y1 - y0) * 0.76), label, font=lf, fill=MINT, anchor="mm")


# ── Estate ad (status / post / landscape) ────────────────────────────────────────────────────
def compose_estate_ad(ctx: MarketingContext, kind: str, *, qr: bool = True) -> bytes:
    if kind not in AD_KINDS:
        raise ValueError("Unknown ad format")
    if kind == "landscape":
        return _estate_ad_landscape(ctx, qr=qr)
    width, height = PORTRAIT_SIZES[kind]
    canvas = _gradient(width, height, DEEP, DEEPER)
    draw = ImageDraw.Draw(canvas)
    margin = 64
    story = kind == "status"
    y = 84 if story else 60

    _brand_row(canvas, draw, ctx, margin, y, 460, 84 if story else 72)
    _pill(draw, (width - margin, y + 8), "LIVE AVAILABILITY", font(26, True), (24, 112, 76), MINT, anchor_right=True)
    y += (140 if story else 118)

    name_font = _fit_font(draw, ctx.estate.name, width - margin * 2, 92 if story else 80, 44)
    for line in _wrap(draw, ctx.estate.name, name_font, width - margin * 2, 2):
        draw.text((margin, y), line, font=name_font, fill=WHITE)
        y += int(name_font.size * 1.14)
    if ctx.estate.public_tagline:
        y += 8
        for line in _wrap(draw, ctx.estate.public_tagline, font(34 if story else 30), width - margin * 2, 2):
            draw.text((margin, y), line, font=font(34 if story else 30), fill=MINT)
            y += 46
    if ctx.location:
        y += 10
        draw.ellipse([margin, y + 10, margin + 18, y + 28], fill=AMBER)
        draw.text((margin + 32, y), ctx.location, font=font(30 if story else 27), fill=(220, 236, 227))
        y += 52
    y += 22

    cta_h = 420 if story else 330
    stats_h = 150 if story else 128
    footer_h = 64
    map_bottom = height - footer_h - cta_h - stats_h - 60
    map_h = max(map_bottom - y, 260)
    map_box = (margin, y, width - margin, y + map_h)
    try:
        layout = Image.open(io.BytesIO(draw_layout_png(ctx, width=map_box[2] - map_box[0], height=map_h, background=(246, 248, 250)))).convert("RGB")
        _rounded_paste(canvas, layout, map_box, 34)
    except ValueError:
        draw.rounded_rectangle(map_box, radius=34, fill=(17, 84, 58))
    y = map_box[3] + 26

    gap = 18
    card_w = (width - margin * 2 - gap * 2) // 3
    for index, (value, label, colour) in enumerate(
        [(str(ctx.available), "Available", (255, 255, 255)), (str(ctx.reserved), "Reserved", (255, 214, 120)), (str(ctx.sold), "Sold", (160, 200, 255))]
    ):
        x0 = margin + index * (card_w + gap)
        _stat_card(draw, (x0, y, x0 + card_w, y + stats_h), value, label, colour, 64 if story else 54, 26)
    y += stats_h + 26

    cta_box = (margin, y, width - margin, y + cta_h - 26)
    draw.rounded_rectangle(cta_box, radius=34, fill=WHITE)
    inner = 30
    qr_size = (cta_box[3] - cta_box[1]) - inner * 2 if qr else 0
    qr_size = min(qr_size, 300)
    text_x = cta_box[0] + inner
    if qr:
        canvas.paste(qr_image(ctx.page_url, qr_size), (cta_box[2] - inner - qr_size, cta_box[1] + inner))
    text_w = cta_box[2] - inner - (qr_size + 24 if qr else 0) - text_x
    ty = cta_box[1] + inner
    draw.text((text_x, ty), "Scan to see the live plot map", font=_fit_font(draw, "Scan to see the live plot map", text_w, 38 if story else 34, 22), fill=INK)
    ty += 56
    if ctx.min_price and ctx.show_prices:
        draw.text((text_x, ty), f"Plots from {naira_short(ctx.min_price)}", font=font(44 if story else 38, True), fill=BRAND_DARK)
        ty += 62
    if ctx.payment_plan:
        plan = " / ".join(f"{item.get('percentage')}%" for item in ctx.payment_plan[:4])
        draw.text((text_x, ty), f"Flexible payment: {plan}", font=font(26), fill=MUTED)
        ty += 40
    draw.text((text_x, ty), _contact_line(ctx), font=_fit_font(draw, _contact_line(ctx), text_w, 28, 18, bold=True), fill=INK)
    ty += 40
    draw.text((text_x, ty), _short_url(ctx), font=_fit_font(draw, _short_url(ctx), text_w, 22, 14), fill=MUTED)

    draw.text((width / 2, height - footer_h / 2 - 6), f"Availability as of {ctx.stamp}  ·  Powered by LandCheck Estates", font=font(22), fill=(150, 196, 171), anchor="mm")
    return _png(canvas)


def _estate_ad_landscape(ctx: MarketingContext, *, qr: bool) -> bytes:
    width, height = LANDSCAPE_SIZE
    canvas = _gradient(width, height, DEEP, DEEPER)
    draw = ImageDraw.Draw(canvas)
    margin = 40
    map_box = (margin, margin, 600, height - margin)
    try:
        layout = Image.open(io.BytesIO(draw_layout_png(ctx, width=map_box[2] - map_box[0], height=map_box[3] - map_box[1], background=(246, 248, 250)))).convert("RGB")
        _rounded_paste(canvas, layout, map_box, 26)
    except ValueError:
        draw.rounded_rectangle(map_box, radius=26, fill=(17, 84, 58))
    x = 636
    max_w = width - x - margin
    _brand_row(canvas, draw, ctx, x, 40, 300, 52)
    y = 116
    name_font = _fit_font(draw, ctx.estate.name, max_w, 50, 30)
    for line in _wrap(draw, ctx.estate.name, name_font, max_w, 2):
        draw.text((x, y), line, font=name_font, fill=WHITE)
        y += int(name_font.size * 1.15)
    if ctx.location:
        draw.text((x, y + 4), ctx.location, font=font(22), fill=MINT)
        y += 38
    y += 16
    gap = 12
    card_w = (max_w - gap * 2) // 3
    for index, (value, label, colour) in enumerate(
        [(str(ctx.available), "Available", WHITE), (str(ctx.reserved), "Reserved", (255, 214, 120)), (str(ctx.sold), "Sold", (160, 200, 255))]
    ):
        x0 = x + index * (card_w + gap)
        _stat_card(draw, (x0, y, x0 + card_w, y + 96), value, label, colour, 40, 18)
    y += 118
    if ctx.min_price and ctx.show_prices:
        draw.text((x, y), f"Plots from {naira_short(ctx.min_price)}", font=font(34, True), fill=WHITE)
        y += 48
    if qr:
        qr_size = 132
        canvas.paste(qr_image(ctx.page_url, qr_size), (width - margin - qr_size, height - margin - qr_size))
        draw.text((x, height - margin - 96), "Scan for the live map", font=font(24, True), fill=WHITE)
        draw.text((x, height - margin - 62), _contact_line(ctx), font=_fit_font(draw, _contact_line(ctx), max_w - qr_size - 20, 22, 14, bold=False), fill=MINT)
    else:
        draw.text((x, height - margin - 70), "View the live map and reserve a plot", font=font(26, True), fill=WHITE)
        draw.text((x, height - margin - 32), _contact_line(ctx), font=_fit_font(draw, _contact_line(ctx), max_w, 22, 14, bold=False), fill=MINT)
    return _png(canvas)


# ── Plot ad / share card ─────────────────────────────────────────────────────────────────────
def compose_plot_ad(ctx: MarketingContext, plot: EstatePlot, kind: str, *, qr: bool = True) -> bytes:
    if kind not in AD_KINDS:
        raise ValueError("Unknown ad format")
    status = plot.commercial_status
    status_rgb = STATUS_RGB.get(status, SLATE)
    status_text = STATUS_LABEL.get(status, "Not available")
    price_text = naira(plot.asking_price) if (ctx.show_prices and plot.asking_price is not None) else "Price on request"

    if kind == "landscape":
        width, height = LANDSCAPE_SIZE
        canvas = _gradient(width, height, DEEP, DEEPER)
        draw = ImageDraw.Draw(canvas)
        margin = 36
        map_box = (margin, margin, 610, height - margin)
        pane_w, pane_h = map_box[2] - map_box[0], map_box[3] - map_box[1]
    else:
        width, height = PORTRAIT_SIZES[kind]
        canvas = _gradient(width, height, DEEP, DEEPER)
        draw = ImageDraw.Draw(canvas)
        margin = 64
        story = kind == "status"
        top = 84 if story else 60
        pane_w = width - margin * 2
        pane_h = int(height * (0.36 if story else 0.34))
        map_box = (margin, top + 330 if story else top + 300, margin + pane_w, (top + 330 if story else top + 300) + pane_h)

    satellite = fetch_plot_satellite(plot, pane_w, pane_h)
    if satellite is None:
        try:
            satellite = Image.open(io.BytesIO(draw_layout_png(ctx, width=pane_w, height=pane_h, background=(246, 248, 250), focus_plot=plot))).convert("RGB")
        except ValueError:
            satellite = Image.new("RGB", (pane_w, pane_h), (17, 84, 58))
    _rounded_paste(canvas, satellite, map_box, 30 if kind != "landscape" else 26)
    draw = ImageDraw.Draw(canvas)
    if satellite is not None:
        _pill(draw, (map_box[0] + 18, map_box[1] + 18), status_text.upper(), font(24 if kind == "landscape" else 28, True), status_rgb, WHITE)

    if kind == "landscape":
        x = 646
        max_w = width - x - margin
        _brand_row(canvas, draw, ctx, x, 36, 300, 46)
        draw = ImageDraw.Draw(canvas)
        y = 104
        draw.text((x, y), ctx.estate.name, font=_fit_font(draw, ctx.estate.name, max_w, 30, 20, bold=False), fill=MINT)
        y += 42
        plot_font = _fit_font(draw, f"PLOT {plot.plot_number}", max_w, 84, 40)
        draw.text((x, y), f"PLOT {plot.plot_number}", font=plot_font, fill=WHITE)
        y += int(plot_font.size * 1.2)
        draw.text((x, y), area_text(plot.area_sqm), font=font(32), fill=(220, 236, 227))
        y += 52
        draw.text((x, y), price_text, font=_fit_font(draw, price_text, max_w, 44, 26), fill=(255, 214, 120))
        y += 66
        if ctx.location:
            draw.text((x, y), ctx.location, font=_fit_font(draw, ctx.location, max_w, 22, 14, bold=False), fill=MINT)
        if qr:
            size = 120
            canvas.paste(qr_image(ctx.plot_url(plot), size), (width - margin - size, height - margin - size))
            draw.text((x, height - margin - 74), "Scan for live map", font=font(22, True), fill=WHITE)
        else:
            draw.text((x, height - margin - 74), "Tap to view on the live map", font=font(24, True), fill=WHITE)
        draw.text((x, height - margin - 36), _contact_line(ctx), font=_fit_font(draw, _contact_line(ctx), max_w - (140 if qr else 0), 22, 14, bold=False), fill=MINT)
        return _png(canvas)

    story = kind == "status"
    top = 84 if story else 60
    _brand_row(canvas, draw, ctx, margin, top, 460, 84 if story else 72)
    draw = ImageDraw.Draw(canvas)
    y = top + (116 if story else 100)
    draw.text((margin, y), ctx.estate.name, font=_fit_font(draw, ctx.estate.name, width - margin * 2, 46, 26, bold=False), fill=MINT)
    y += 62
    plot_font = _fit_font(draw, f"PLOT {plot.plot_number}", width - margin * 2, 120 if story else 104, 60)
    draw.text((margin, y), f"PLOT {plot.plot_number}", font=plot_font, fill=WHITE)
    y = map_box[3] + 34
    draw.text((margin, y), area_text(plot.area_sqm), font=font(52 if story else 46, True), fill=WHITE)
    _pill(draw, (width - margin, y + 4), status_text.upper(), font(28, True), status_rgb, WHITE, anchor_right=True)
    y += 76
    draw.text((margin, y), price_text, font=_fit_font(draw, price_text, width - margin * 2, 72 if story else 62, 34), fill=(255, 214, 120))
    y += 96 if story else 82
    if ctx.payment_plan:
        plan = " / ".join(f"{item.get('percentage')}% {item.get('label')}" for item in ctx.payment_plan[:3])
        for line in _wrap(draw, f"Pay in stages: {plan}", font(28), width - margin * 2, 2):
            draw.text((margin, y), line, font=font(28), fill=MINT)
            y += 38
    cta_h = 250 if story else 210
    cta_box = (margin, height - 64 - cta_h, width - margin, height - 64)
    draw.rounded_rectangle(cta_box, radius=30, fill=WHITE)
    size = cta_h - 44
    if qr:
        canvas.paste(qr_image(ctx.plot_url(plot), size), (cta_box[2] - 22 - size, cta_box[1] + 22))
    text_x = cta_box[0] + 30
    text_w = cta_box[2] - 30 - (size + 46 if qr else 0) - text_x
    ty = cta_box[1] + 30
    draw.text((text_x, ty), "Reserve this plot", font=_fit_font(draw, "Reserve this plot", text_w, 40, 24), fill=INK)
    ty += 56
    draw.text((text_x, ty), _contact_line(ctx), font=_fit_font(draw, _contact_line(ctx), text_w, 28, 16), fill=BRAND_DARK)
    ty += 42
    draw.text((text_x, ty), _short_url(ctx), font=_fit_font(draw, _short_url(ctx), text_w, 22, 14, bold=False), fill=MUTED)
    draw.text((width / 2, height - 32), f"Availability as of {ctx.stamp}  ·  Powered by LandCheck Estates", font=font(22), fill=(150, 196, 171), anchor="mm")
    return _png(canvas)


def layout_png_for_documents(ctx: MarketingContext, width: int = 1500, height: int = 1000) -> bytes:
    return draw_layout_png(ctx, width=width, height=height, background=(246, 248, 250))
