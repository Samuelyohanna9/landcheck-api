from __future__ import annotations

"""Site-progress media: private storage, EXIF location/time extraction, and checking a capture
location against the plot or estate geometry so buyers can see whether media was really shot on
the land."""

import io
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from geoalchemy2.shape import to_shape
from PIL import Image, ImageOps
from pyproj import CRS, Transformer
from shapely.geometry import Point
from shapely.ops import transform as shapely_transform

from app.utils.r2_objects import build_r2_settings, create_r2_client

MAX_PHOTO_BYTES = max(int(os.getenv("ESTATE_DOCUMENT_MAX_BYTES", str(15 * 1024 * 1024))), 1)
MAX_VIDEO_BYTES = max(int(os.getenv("ESTATE_PROGRESS_VIDEO_MAX_BYTES", str(60 * 1024 * 1024))), 1)

VERIFICATION_LABELS = {
    "on_plot": "Captured on the plot",
    "on_estate": "Captured inside the estate",
    "near_estate": "Captured near the estate",
    "outside": "Captured away from the estate",
    "unverified": "Location not verified",
}


def detect_media(data: bytes) -> tuple[str, str]:
    """Returns (kind, mime) from magic bytes - never trust the client's content type."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "video", "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "video/webm"
    raise HTTPException(422, "Upload a JPG or PNG photo, or an MP4 / WebM video")


def _dms_to_degrees(values: Any, ref: Any) -> float | None:
    try:
        degrees = float(values[0]) + float(values[1]) / 60 + float(values[2]) / 3600
    except Exception:
        return None
    ref_text = ref.decode() if isinstance(ref, bytes) else str(ref or "")
    return -degrees if ref_text.upper() in {"S", "W"} else degrees


def extract_exif(data: bytes) -> dict[str, Any]:
    """Best-effort GPS position and capture time from a photo. Empty dict when absent."""
    result: dict[str, Any] = {}
    try:
        image = Image.open(io.BytesIO(data))
        exif = image.getexif()
        gps = exif.get_ifd(0x8825)
        if gps:
            lat = _dms_to_degrees(gps.get(2), gps.get(1))
            lon = _dms_to_degrees(gps.get(4), gps.get(3))
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0):
                result["lat"], result["lon"] = lat, lon
        stamp = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        if stamp:
            parsed = datetime.strptime(str(stamp).strip(), "%Y:%m:%d %H:%M:%S")
            # EXIF has no zone; Nigerian field staff shoot in local time (UTC+1).
            result["captured_at"] = (parsed - timedelta(hours=1)).replace(tzinfo=timezone.utc)
    except Exception:
        return result
    return result


def process_photo(data: bytes) -> bytes:
    """Re-encode as a web-sized JPEG. This also drops the EXIF block, so device details are never
    served publicly once the location has been read."""
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1920, 1920), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=84, optimize=True)
        return buffer.getvalue()
    except Exception as exc:
        raise HTTPException(422, "That photo could not be read") from exc


def store_progress_media(*, organization_uid: str, estate_uid: str, mime: str, data: bytes) -> str:
    settings = build_r2_settings(prefix="R2")
    if not settings:
        raise HTTPException(503, "Private media storage is not configured")
    extension = {"image/jpeg": "jpg", "video/mp4": "mp4", "video/webm": "webm"}.get(mime, "bin")
    key = f"estates/org_{organization_uid}/progress/{estate_uid}/{uuid.uuid4().hex}.{extension}"
    create_r2_client(settings).put_object(Bucket=settings["bucket"], Key=key, Body=data, ContentType=mime, CacheControl="public, max-age=86400")
    return key


def read_progress_media(key: str) -> tuple[bytes, str]:
    settings = build_r2_settings(prefix="R2")
    if not settings:
        raise HTTPException(503, "Private media storage is not configured")
    response = create_r2_client(settings).get_object(Bucket=settings["bucket"], Key=key)
    return response["Body"].read(), str(response.get("ContentType") or "application/octet-stream")


def verify_location(*, lat: float, lon: float, accuracy_m: float | None, plots: list[Any], boundary: Any, focus_plot: Any | None) -> tuple[str, float]:
    """Compare a capture point with the plot (when one was named) and the estate. Distances are
    measured in a local metric projection centred on the capture point."""
    tolerance = max(8.0, min(float(accuracy_m or 0.0), 40.0))
    local = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m")
    forward = Transformer.from_crs("EPSG:4326", local, always_xy=True).transform
    origin = Point(0, 0)

    def distance_to(geometry: Any) -> float:
        shape = shapely_transform(forward, to_shape(geometry))
        return 0.0 if shape.contains(origin) else float(shape.distance(origin))

    if focus_plot is not None and focus_plot.geometry is not None:
        plot_distance = distance_to(focus_plot.geometry)
        if plot_distance <= tolerance:
            return "on_plot", plot_distance
    if boundary is not None:
        estate_distance = distance_to(boundary)
    else:
        distances = [distance_to(plot.geometry) for plot in plots if plot.geometry is not None]
        estate_distance = min(distances) if distances else float("inf")
    if estate_distance <= tolerance:
        return "on_estate", estate_distance
    if estate_distance <= 300:
        return "near_estate", estate_distance
    return "outside", estate_distance if estate_distance != float("inf") else 0.0


def clean_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(value or "")).strip(".-") or "media"
