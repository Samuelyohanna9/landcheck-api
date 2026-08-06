import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, model_validator
from pyproj import Transformer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.routers.plots import COORDINATE_SYSTEMS, get_db
from app.utils.r2_objects import build_r2_settings, create_r2_client, delete_object_best_effort, upload_bytes


router = APIRouter(prefix="/survey-georeference", tags=["survey-georeference"])

logger = logging.getLogger("survey_georeference")

_SCHEMA_READY = False
_SCHEMA_LOCK = Lock()

MAX_UPLOAD_BYTES = max(1024 * 1024, int(os.getenv("GEOREFERENCE_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))))
MAX_GCP_COUNT = max(3, int(os.getenv("GEOREFERENCE_MAX_GCPS", "24")))
R2_PREFIX = str(os.getenv("R2_GEOREFERENCE_PREFIX") or "survey/georeference").strip().strip("/")
R2_SETTINGS_PREFIX = str(os.getenv("R2_GEOREFERENCE_SETTINGS_PREFIX") or "R2").strip() or "R2"
RETENTION_DRAFT_DAYS = max(1, int(os.getenv("GEOREFERENCE_DRAFT_RETENTION_DAYS", "14")))
RETENTION_FINAL_DAYS = max(RETENTION_DRAFT_DAYS, int(os.getenv("GEOREFERENCE_FINAL_RETENTION_DAYS", "45")))


class GroundControlPointInput(BaseModel):
    id: str | None = None
    label: str | None = None
    image_x: float
    image_y: float
    ground_x: float
    ground_y: float
    lng: float | None = None
    lat: float | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_ground_coordinates(cls, value: Any):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        ground_x = payload.get("ground_x", payload.get("lng"))
        ground_y = payload.get("ground_y", payload.get("lat"))
        if ground_x is None or ground_y is None:
            return payload
        payload["ground_x"] = ground_x
        payload["ground_y"] = ground_y
        payload.setdefault("lng", ground_x)
        payload.setdefault("lat", ground_y)
        return payload


class SolveGeoreferenceRequest(BaseModel):
    target_coordinate_system: str = "wgs84"
    ground_control_points: list[GroundControlPointInput] = Field(default_factory=list)


class PixelPointInput(BaseModel):
    x: float
    y: float


class DigitizedFeatureInput(BaseModel):
    id: str | None = None
    label: str | None = None
    feature_type: str
    pixels: list[PixelPointInput] = Field(default_factory=list)
    is_primary: bool = False


class SaveDigitizedFeaturesRequest(BaseModel):
    features: list[DigitizedFeatureInput] = Field(default_factory=list)


def _looks_like_projected_coordinates(x: float, y: float) -> bool:
    return abs(float(x)) > 180 or abs(float(y)) > 90


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json_load(raw: Any, fallback: Any):
    if raw in (None, ""):
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _safe_slug(value: str | None, fallback: str = "raster") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-.")
    return cleaned or fallback


def _alpha_station(index: int) -> str:
    base = ""
    value = int(index)
    while True:
        base = chr(65 + (value % 26)) + base
        value = (value // 26) - 1
        if value < 0:
            break
    return base


def _coordinate_system_to_epsg(key: str) -> int:
    clean = str(key or "wgs84").strip().lower()
    epsg = COORDINATE_SYSTEMS.get(clean)
    if epsg is None:
        raise HTTPException(status_code=400, detail="Unsupported target coordinate system.")
    return int(epsg)


def _build_r2() -> dict:
    settings = build_r2_settings(prefix=R2_SETTINGS_PREFIX)
    if not settings:
        raise HTTPException(status_code=500, detail="R2 storage is not configured for georeferencing.")
    return settings


def _ensure_schema(db: Session):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS survey_georeference_sessions (
                    id TEXT PRIMARY KEY,
                    title_text TEXT,
                    status VARCHAR(40) NOT NULL DEFAULT 'draft',
                    target_coordinate_system VARCHAR(40) NOT NULL DEFAULT 'wgs84',
                    target_epsg INTEGER NOT NULL DEFAULT 4326,
                    source_file_name TEXT,
                    source_content_type TEXT,
                    source_object_key TEXT,
                    source_sha256 TEXT,
                    source_width INTEGER,
                    source_height INTEGER,
                    gcps_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    transform_json JSONB,
                    overlay_json JSONB,
                    features_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    delete_after_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finalized_at TIMESTAMPTZ
                )
                """
            )
        )
        db.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_survey_georef_delete_after ON survey_georeference_sessions(delete_after_at)"
            )
        )
        db.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_survey_georef_status_updated ON survey_georeference_sessions(status, updated_at DESC)"
            )
        )
        db.commit()
        _SCHEMA_READY = True


def _session_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    ground_control_points = _safe_json_load(row.get("gcps_json"), [])
    normalized_gcps = [
        GroundControlPointInput.model_validate(item).model_dump() for item in ground_control_points if isinstance(item, dict)
    ]
    transform = _safe_json_load(row.get("transform_json"), None)
    if isinstance(transform, dict):
        transform = {
            **transform,
            "residuals": [
                GroundControlPointInput.model_validate(item).model_dump()
                for item in list(transform.get("residuals") or [])
                if isinstance(item, dict)
            ],
        }
    return {
        "id": str(row.get("id") or ""),
        "title_text": str(row.get("title_text") or ""),
        "status": str(row.get("status") or "draft"),
        "target_coordinate_system": str(row.get("target_coordinate_system") or "wgs84"),
        "target_epsg": int(row.get("target_epsg") or 4326),
        "source_file_name": str(row.get("source_file_name") or ""),
        "source_content_type": str(row.get("source_content_type") or ""),
        "source_width": int(row.get("source_width") or 0),
        "source_height": int(row.get("source_height") or 0),
        "ground_control_points": normalized_gcps,
        "transform": transform,
        "overlay": _safe_json_load(row.get("overlay_json"), None),
        "features": _safe_json_load(row.get("features_json"), []),
        "delete_after_at": row.get("delete_after_at").isoformat() if row.get("delete_after_at") else None,
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        "finalized_at": row.get("finalized_at").isoformat() if row.get("finalized_at") else None,
        "raster_url": f"/survey-georeference/sessions/{row.get('id')}/raster",
    }


def _load_session_row(db: Session, session_id: str) -> dict[str, Any]:
    _ensure_schema(db)
    row = (
        db.execute(
            text("SELECT * FROM survey_georeference_sessions WHERE id = :session_id"),
            {"session_id": str(session_id)},
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Georeference session not found.")
    return dict(row)


def _touch_session(db: Session, session_id: str):
    db.execute(
        text(
            """
            UPDATE survey_georeference_sessions
            SET last_accessed_at = NOW(), updated_at = NOW()
            WHERE id = :session_id
            """
        ),
        {"session_id": str(session_id)},
    )
    db.commit()


def _make_transformers(target_epsg: int):
    if int(target_epsg) == 4326:
        return None, None
    return (
        Transformer.from_crs(4326, int(target_epsg), always_xy=True),
        Transformer.from_crs(int(target_epsg), 4326, always_xy=True),
    )


def _solve_affine(width: int, height: int, coordinate_system_key: str, gcps: list[GroundControlPointInput]) -> dict[str, Any]:
    if len(gcps) < 3:
        raise HTTPException(status_code=400, detail="At least 3 control points are required.")
    if len(gcps) > MAX_GCP_COUNT:
        raise HTTPException(status_code=400, detail=f"Use at most {MAX_GCP_COUNT} control points per raster.")

    target_epsg = _coordinate_system_to_epsg(coordinate_system_key)
    forward_transformer, reverse_transformer = _make_transformers(target_epsg)
    projected_pairs: list[tuple[float, float]] = []

    for gcp in gcps:
        if forward_transformer is None:
            projected_pairs.append((float(gcp.ground_x), float(gcp.ground_y)))
        elif _looks_like_projected_coordinates(gcp.ground_x, gcp.ground_y):
            projected_pairs.append((float(gcp.ground_x), float(gcp.ground_y)))
        else:
            px, py = forward_transformer.transform(float(gcp.ground_x), float(gcp.ground_y))
            projected_pairs.append((float(px), float(py)))

    a_matrix = np.asarray([[1.0, float(item.image_x), float(item.image_y)] for item in gcps], dtype=float)
    world_x = np.asarray([pair[0] for pair in projected_pairs], dtype=float)
    world_y = np.asarray([pair[1] for pair in projected_pairs], dtype=float)

    coeff_x, *_ = np.linalg.lstsq(a_matrix, world_x, rcond=None)
    coeff_y, *_ = np.linalg.lstsq(a_matrix, world_y, rcond=None)

    predicted_x = a_matrix @ coeff_x
    predicted_y = a_matrix @ coeff_y

    if target_epsg == 4326:
        meter_transformer = Transformer.from_crs(4326, 3857, always_xy=True)

    residuals: list[dict[str, Any]] = []
    squared_error = 0.0
    for idx, gcp in enumerate(gcps):
        actual_world = projected_pairs[idx]
        predicted_world = (float(predicted_x[idx]), float(predicted_y[idx]))
        if target_epsg == 4326:
            ax, ay = meter_transformer.transform(actual_world[0], actual_world[1])
            px, py = meter_transformer.transform(predicted_world[0], predicted_world[1])
            error_m = float(math.hypot(px - ax, py - ay))
        else:
            error_m = float(math.hypot(predicted_world[0] - actual_world[0], predicted_world[1] - actual_world[1]))
        squared_error += error_m ** 2
        residuals.append(
            {
                "id": gcp.id or f"gcp-{idx + 1}",
                "label": gcp.label or f"GCP {idx + 1}",
                "image_x": float(gcp.image_x),
                "image_y": float(gcp.image_y),
                "ground_x": float(gcp.ground_x),
                "ground_y": float(gcp.ground_y),
                "lng": float(gcp.ground_x),
                "lat": float(gcp.ground_y),
                "error_m": round(error_m, 3),
            }
        )

    rms_error = math.sqrt(squared_error / max(1, len(gcps)))
    condition_number = float(np.linalg.cond(a_matrix))

    if rms_error <= 1.5 and len(gcps) >= 4:
        quality = "strong"
    elif rms_error <= 5:
        quality = "usable"
    else:
        quality = "weak"

    def apply_pixel(pixel_x: float, pixel_y: float) -> tuple[float, float]:
        x_world = float(coeff_x[0] + coeff_x[1] * pixel_x + coeff_x[2] * pixel_y)
        y_world = float(coeff_y[0] + coeff_y[1] * pixel_x + coeff_y[2] * pixel_y)
        return x_world, y_world

    overlay_corners = []
    for pixel_x, pixel_y in [(0, 0), (width, 0), (width, height), (0, height)]:
        world_x_val, world_y_val = apply_pixel(float(pixel_x), float(pixel_y))
        if reverse_transformer is None:
            lng, lat = world_x_val, world_y_val
        else:
            lng, lat = reverse_transformer.transform(world_x_val, world_y_val)
        overlay_corners.append([round(float(lng), 8), round(float(lat), 8)])

    return {
        "target_coordinate_system": coordinate_system_key,
        "target_epsg": target_epsg,
        "coefficients": {
            "x": [round(float(value), 10) for value in coeff_x.tolist()],
            "y": [round(float(value), 10) for value in coeff_y.tolist()],
        },
        "rms_error_m": round(float(rms_error), 3),
        "condition_number": round(float(condition_number), 4),
        "quality": quality,
        "points_used": len(gcps),
        "residuals": residuals,
        "overlay_corners": overlay_corners,
    }


def _apply_transform_to_pixel(transform: dict[str, Any], pixel_x: float, pixel_y: float) -> tuple[float, float]:
    coeff_x = list(((transform or {}).get("coefficients") or {}).get("x") or [])
    coeff_y = list(((transform or {}).get("coefficients") or {}).get("y") or [])
    if len(coeff_x) != 3 or len(coeff_y) != 3:
        raise HTTPException(status_code=400, detail="Georeference transform is incomplete.")
    x_world = float(coeff_x[0] + coeff_x[1] * float(pixel_x) + coeff_x[2] * float(pixel_y))
    y_world = float(coeff_y[0] + coeff_y[1] * float(pixel_x) + coeff_y[2] * float(pixel_y))
    return x_world, y_world


def _feature_to_saved_payload(feature: DigitizedFeatureInput, transform: dict[str, Any]) -> dict[str, Any]:
    feature_type = str(feature.feature_type or "").strip().lower()
    if feature_type not in {"point", "line", "polygon"}:
        raise HTTPException(status_code=400, detail="Feature type must be point, line, or polygon.")
    pixels = [{"x": float(point.x), "y": float(point.y)} for point in feature.pixels]
    if feature_type == "point" and len(pixels) != 1:
        raise HTTPException(status_code=400, detail="Point features require exactly 1 pixel coordinate.")
    if feature_type == "line" and len(pixels) < 2:
        raise HTTPException(status_code=400, detail="Line features require at least 2 pixel coordinates.")
    if feature_type == "polygon" and len(pixels) < 3:
        raise HTTPException(status_code=400, detail="Polygon features require at least 3 pixel coordinates.")

    target_epsg = int(transform.get("target_epsg") or 4326)
    _, reverse_transformer = _make_transformers(target_epsg)

    target_coordinates = []
    wgs84_coordinates = []
    for point in pixels:
        world_x, world_y = _apply_transform_to_pixel(transform, point["x"], point["y"])
        target_coordinates.append([round(world_x, 6), round(world_y, 6)])
        if reverse_transformer is None:
            lng, lat = world_x, world_y
        else:
            lng, lat = reverse_transformer.transform(world_x, world_y)
        wgs84_coordinates.append([round(float(lng), 8), round(float(lat), 8)])

    if feature_type == "polygon" and wgs84_coordinates[0] != wgs84_coordinates[-1]:
        wgs84_coordinates.append(list(wgs84_coordinates[0]))
        target_coordinates.append(list(target_coordinates[0]))

    return {
        "id": str(feature.id or uuid.uuid4().hex),
        "label": str(feature.label or feature_type.title()).strip() or feature_type.title(),
        "feature_type": feature_type,
        "is_primary": bool(feature.is_primary),
        "pixels": pixels,
        "target_coordinates": target_coordinates,
        "wgs84_coordinates": wgs84_coordinates,
    }


def _build_staking_rows(features: list[dict[str, Any]], target_coordinate_system: str) -> list[dict[str, Any]]:
    if not features:
        raise HTTPException(status_code=400, detail="No digitized features are saved for this session.")

    primary_polygon = next(
        (
            feature
            for feature in features
            if str(feature.get("feature_type") or "") == "polygon" and bool(feature.get("is_primary"))
        ),
        None,
    )
    if primary_polygon is None:
        primary_polygon = next((feature for feature in features if str(feature.get("feature_type") or "") == "polygon"), None)

    rows: list[dict[str, Any]] = []
    if primary_polygon is not None:
        target_points = list(primary_polygon.get("target_coordinates") or [])
        wgs84_points = list(primary_polygon.get("wgs84_coordinates") or [])
        if len(target_points) and target_points[0] == target_points[-1]:
            target_points = target_points[:-1]
        if len(wgs84_points) and wgs84_points[0] == wgs84_points[-1]:
            wgs84_points = wgs84_points[:-1]
        for idx, target_point in enumerate(target_points):
            lng, lat = wgs84_points[idx]
            rows.append(
                {
                    "station": _alpha_station(idx),
                    "feature": str(primary_polygon.get("label") or "Primary polygon"),
                    "coordinate_system": target_coordinate_system,
                    "easting": round(float(target_point[0]), 4),
                    "northing": round(float(target_point[1]), 4),
                    "longitude": round(float(lng), 8),
                    "latitude": round(float(lat), 8),
                }
            )
        return rows

    point_index = 1
    for feature in features:
        if str(feature.get("feature_type") or "") != "point":
            continue
        target_point = list(feature.get("target_coordinates") or [[0, 0]])[0]
        wgs_point = list(feature.get("wgs84_coordinates") or [[0, 0]])[0]
        rows.append(
            {
                "station": f"P{point_index}",
                "feature": str(feature.get("label") or f"Point {point_index}"),
                "coordinate_system": target_coordinate_system,
                "easting": round(float(target_point[0]), 4),
                "northing": round(float(target_point[1]), 4),
                "longitude": round(float(wgs_point[0]), 8),
                "latitude": round(float(wgs_point[1]), 8),
            }
        )
        point_index += 1
    if not rows:
        raise HTTPException(status_code=400, detail="Add a primary polygon or point features before exporting CSV.")
    return rows


def _raster_object_key(session_id: str, filename: str) -> str:
    stamp = _now_utc().strftime("%Y%m%dT%H%M%SZ")
    safe_name = _safe_slug(filename, "raster")
    return "/".join([R2_PREFIX, _now_utc().strftime("%Y"), _now_utc().strftime("%m"), f"{session_id}_{stamp}_{safe_name}"])


def cleanup_expired_georeference_sessions(db: Session) -> dict[str, int]:
    _ensure_schema(db)
    rows = (
        db.execute(
            text(
                """
                SELECT id, source_object_key
                FROM survey_georeference_sessions
                WHERE delete_after_at IS NOT NULL
                  AND delete_after_at <= NOW()
                ORDER BY delete_after_at ASC
                LIMIT 200
                """
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return {"sessions_deleted": 0, "r2_objects_deleted": 0}

    settings = build_r2_settings(prefix=R2_SETTINGS_PREFIX)
    r2_deleted = 0
    deleted_sessions = 0
    for row in rows:
        if settings and row.get("source_object_key"):
            if delete_object_best_effort(settings, str(row.get("source_object_key") or "")):
                r2_deleted += 1
        db.execute(text("DELETE FROM survey_georeference_sessions WHERE id = :session_id"), {"session_id": str(row.get("id") or "")})
        deleted_sessions += 1
    db.commit()
    return {"sessions_deleted": deleted_sessions, "r2_objects_deleted": r2_deleted}


def run_georeference_retention_cleanup() -> dict[str, int]:
    db = SessionLocal()
    try:
        result = cleanup_expired_georeference_sessions(db)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/sessions")
async def create_georeference_session(
    file: UploadFile = File(...),
    title_text: str | None = Form(None),
    target_coordinate_system: str = Form("wgs84"),
    db: Session = Depends(get_db),
):
    _ensure_schema(db)
    settings = _build_r2()

    content_type = str(file.content_type or "").strip().lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="Upload a JPEG, PNG, or WEBP raster.")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="The raster file is empty.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"Raster exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.")

    try:
        image = Image.open(io.BytesIO(payload))
        # Browsers auto-rotate <img> elements per EXIF orientation, so every pixel coordinate the
        # frontend records (control points, digitized features) is captured against the ROTATED
        # frame - but Image.size here would otherwise report the raw, pre-rotation dimensions.
        # That mismatch doesn't affect the fitted transform itself (it's just linear regression
        # over whatever pixel coordinates it's given), but it does corrupt the raster overlay's
        # corner box on the map, which this endpoint computes from width/height directly. Baking
        # the rotation into the pixel data up front keeps every consumer (browser display, GCP
        # capture, overlay corners) working from the exact same frame.
        orientation_tag = int((image.getexif() or {}).get(0x0112, 1) or 1)
        if orientation_tag != 1:
            image = ImageOps.exif_transpose(image)
            buffer = io.BytesIO()
            if content_type == "image/jpeg":
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.save(buffer, format="JPEG", quality=95)
            elif content_type == "image/png":
                image.save(buffer, format="PNG")
            else:
                image.save(buffer, format="WEBP", quality=95)
            payload = buffer.getvalue()
        width, height = image.size
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to read raster image: {exc}") from exc

    session_id = uuid.uuid4().hex
    object_key = _raster_object_key(session_id, str(file.filename or "raster"))
    upload_meta = upload_bytes(settings, object_key, payload, content_type=content_type)
    delete_after_at = _now_utc() + timedelta(days=RETENTION_DRAFT_DAYS)
    target_epsg = _coordinate_system_to_epsg(target_coordinate_system)

    db.execute(
        text(
            """
            INSERT INTO survey_georeference_sessions (
                id,
                title_text,
                status,
                target_coordinate_system,
                target_epsg,
                source_file_name,
                source_content_type,
                source_object_key,
                source_sha256,
                source_width,
                source_height,
                gcps_json,
                features_json,
                delete_after_at
            )
            VALUES (
                :session_id,
                :title_text,
                'draft',
                :target_coordinate_system,
                :target_epsg,
                :source_file_name,
                :source_content_type,
                :source_object_key,
                :source_sha256,
                :source_width,
                :source_height,
                '[]'::jsonb,
                '[]'::jsonb,
                :delete_after_at
            )
            """
        ),
        {
            "session_id": session_id,
            "title_text": str(title_text or file.filename or "Scanned raster"),
            "target_coordinate_system": str(target_coordinate_system or "wgs84").strip().lower(),
            "target_epsg": target_epsg,
            "source_file_name": str(file.filename or "raster"),
            "source_content_type": content_type,
            "source_object_key": str(upload_meta.get("object_key") or ""),
            "source_sha256": hashlib.sha256(payload).hexdigest(),
            "source_width": int(width),
            "source_height": int(height),
            "delete_after_at": delete_after_at,
        },
    )
    db.commit()
    row = _load_session_row(db, session_id)
    return {"ok": True, "session": _session_to_payload(row)}


@router.get("/sessions/{session_id}")
def get_georeference_session(session_id: str, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    _touch_session(db, session_id)
    return {"ok": True, "session": _session_to_payload(row)}


@router.get("/sessions/{session_id}/raster")
def stream_georeference_raster(session_id: str, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    settings = _build_r2()
    object_key = str(row.get("source_object_key") or "")
    if not object_key:
        raise HTTPException(status_code=404, detail="Raster source is no longer available.")
    try:
        client = create_r2_client(settings)
        obj = client.get_object(Bucket=settings["bucket"], Key=object_key)
        body = obj["Body"]
        _touch_session(db, session_id)
        return StreamingResponse(body.iter_chunks(chunk_size=1024 * 512), media_type=str(row.get("source_content_type") or "application/octet-stream"))
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unable to stream raster from storage: {exc}") from exc


@router.post("/sessions/{session_id}/solve")
def solve_georeference_session(session_id: str, payload: SolveGeoreferenceRequest, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    width = int(row.get("source_width") or 0)
    height = int(row.get("source_height") or 0)
    transform = _solve_affine(width, height, payload.target_coordinate_system, payload.ground_control_points)
    delete_after_at = _now_utc() + timedelta(days=RETENTION_FINAL_DAYS)
    db.execute(
        text(
            """
            UPDATE survey_georeference_sessions
            SET status = 'georeferenced',
                target_coordinate_system = :target_coordinate_system,
                target_epsg = :target_epsg,
                gcps_json = CAST(:gcps_json AS JSONB),
                transform_json = CAST(:transform_json AS JSONB),
                overlay_json = CAST(:overlay_json AS JSONB),
                delete_after_at = :delete_after_at,
                updated_at = NOW(),
                last_accessed_at = NOW()
            WHERE id = :session_id
            """
        ),
        {
            "session_id": session_id,
            "target_coordinate_system": str(payload.target_coordinate_system or "wgs84").strip().lower(),
            "target_epsg": int(transform.get("target_epsg") or 4326),
            "gcps_json": json.dumps([item.model_dump() for item in payload.ground_control_points]),
            "transform_json": json.dumps(transform),
            "overlay_json": json.dumps({"corners": transform.get("overlay_corners") or []}),
            "delete_after_at": delete_after_at,
        },
    )
    db.commit()
    updated = _load_session_row(db, session_id)
    return {"ok": True, "session": _session_to_payload(updated)}


@router.post("/sessions/{session_id}/features")
def save_georeference_features(session_id: str, payload: SaveDigitizedFeaturesRequest, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    transform = _safe_json_load(row.get("transform_json"), None)
    if not isinstance(transform, dict):
        raise HTTPException(status_code=400, detail="Georeference the raster first before digitizing features.")

    features = [_feature_to_saved_payload(feature, transform) for feature in payload.features]
    delete_after_at = _now_utc() + timedelta(days=RETENTION_FINAL_DAYS)
    db.execute(
        text(
            """
            UPDATE survey_georeference_sessions
            SET status = 'digitized',
                features_json = CAST(:features_json AS JSONB),
                finalized_at = NOW(),
                delete_after_at = :delete_after_at,
                updated_at = NOW(),
                last_accessed_at = NOW()
            WHERE id = :session_id
            """
        ),
        {
            "session_id": session_id,
            "features_json": json.dumps(features),
            "delete_after_at": delete_after_at,
        },
    )
    db.commit()
    updated = _load_session_row(db, session_id)
    return {"ok": True, "session": _session_to_payload(updated)}


@router.get("/sessions/{session_id}/exports/staking.csv")
def export_georeference_staking_csv(session_id: str, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    features = _safe_json_load(row.get("features_json"), [])
    rows = _build_staking_rows(features, str(row.get("target_coordinate_system") or "wgs84"))

    csv_buffer = io.StringIO(newline="")
    csv_buffer.write("sep=,\r\n")
    writer = csv.writer(csv_buffer, lineterminator="\r\n")
    writer.writerow(["Station", "Feature", "Coordinate System", "Easting (m)", "Northing (m)", "Longitude", "Latitude"])
    for item in rows:
        writer.writerow(
            [
                item["station"],
                item["feature"],
                str(item["coordinate_system"]).upper(),
                item["easting"],
                item["northing"],
                item["longitude"],
                item["latitude"],
            ]
        )
    _touch_session(db, session_id)
    return Response(
        content="\ufeff" + csv_buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="georeference_{session_id}_staking.csv"'},
    )


@router.delete("/sessions/{session_id}")
def delete_georeference_session(session_id: str, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    settings = build_r2_settings(prefix=R2_SETTINGS_PREFIX)
    if settings and row.get("source_object_key"):
        delete_object_best_effort(settings, str(row.get("source_object_key") or ""))
    db.execute(text("DELETE FROM survey_georeference_sessions WHERE id = :session_id"), {"session_id": session_id})
    db.commit()
    return {"ok": True}
