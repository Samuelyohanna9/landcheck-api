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
import gc
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
    contact_email: str | None = None
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
        contact_email=(str(organization.contact_email).strip() or None) if (organization and organization.contact_email) else None,
    )


# ── Small render cache ───────────────────────────────────────────────────────────────────────
_CACHE: dict[Any, tuple[float, bytes]] = {}
_CACHE_LOCK = threading.Lock()
# Memory is the constraint, not CPU: a print-resolution page needs a few hundred MB while it renders,
# so those run one at a time and everything else two at a time.
_RENDER_SLOTS = threading.BoundedSemaphore(2)
_HEAVY_SLOTS = threading.BoundedSemaphore(1)


def cached_render(key: Any, ttl_seconds: int, builder: Callable[[], bytes], *, heavy: bool = False) -> bytes:
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl_seconds:
            return hit[1]
    # Bound concurrent renders: each is CPU heavy and may wait on a satellite fetch, and unbounded
    # parallel previews would starve every other request of worker threads.
    with (_HEAVY_SLOTS if heavy else _RENDER_SLOTS):
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and time.monotonic() - hit[0] < ttl_seconds:
                return hit[1]
        data = builder()
        if heavy:
            gc.collect()  # hand the big page buffers back before the next request starts
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


# ── Premium composition (see marketing_design) ───────────────────────────────────────────────
AD_KINDS = {"status", "post", "landscape", "poster", "poster_hd"}
AD_STYLES = {"luxury", "promo"}
PORTRAIT_SIZES = {"status": (1080, 1920), "post": (1080, 1350)}
LANDSCAPE_SIZE = (1200, 630)


def _check(kind: str, style: str) -> None:
    if kind not in AD_KINDS:
        raise ValueError("Unknown ad format")
    if style not in AD_STYLES:
        raise ValueError("Unknown design style")
    if kind in {"poster", "poster_hd"} and style != "promo":
        raise ValueError("The print poster is only available in the promo style")


def compose_estate_ad(ctx: MarketingContext, kind: str, *, qr: bool = True, style: str = "luxury") -> bytes:
    _check(kind, style)
    if style == "promo":
        from app.services.estates import marketing_promo

        return marketing_promo.estate_promo(ctx, kind, qr=qr)
    from app.services.estates import marketing_design

    return marketing_design.estate_ad(ctx, kind, qr=qr)


def compose_plot_ad(ctx: MarketingContext, plot: EstatePlot, kind: str, *, qr: bool = True, style: str = "luxury") -> bytes:
    _check(kind, style)
    if style == "promo":
        from app.services.estates import marketing_promo

        return marketing_promo.plot_promo(ctx, plot, kind, qr=qr)
    from app.services.estates import marketing_design

    return marketing_design.plot_ad(ctx, plot, kind, qr=qr)


def layout_png_for_documents(ctx: MarketingContext, width: int = 1500, height: int = 1000, *, cover_page: bool = False) -> bytes:
    from app.services.estates import marketing_design

    return marketing_design.document_map(ctx, width, height, cover_page=cover_page)
