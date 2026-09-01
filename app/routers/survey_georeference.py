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
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, model_validator
from pyproj import Transformer
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db import SessionLocal
from app.routers.plots import COORDINATE_SYSTEMS, get_db, _safe_filename_fragment
from app.utils.survey_auth_security import require_survey_session, resolve_survey_session
from app.utils.survey_activity import ensure_survey_activity_table, log_survey_activity
from app.utils.coordinate_converter import is_nigeria_auto_utm_coordinate_system, resolve_coordinate_system_key
from app.utils.r2_objects import build_r2_settings, create_r2_client, delete_object_best_effort, upload_bytes
from app.utils.plan_reader import consume_daily_reading
from app.utils.georeference_ai_digitize import (
    AI_DIGITIZE_DAILY_LIMIT,
    GeoreferenceAiDigitizeError,
    detect_digitized_points,
)


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
PREVIEW_MIN_DIMENSION = max(720, int(os.getenv("GEOREFERENCE_PREVIEW_MIN_DIMENSION", "960")))
PREVIEW_MAX_DIMENSION = max(PREVIEW_MIN_DIMENSION, int(os.getenv("GEOREFERENCE_PREVIEW_MAX_DIMENSION", "1800")))


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


def _resolve_target_coordinate_system(key: str, gcps: list[GroundControlPointInput] | None = None) -> str:
    clean = str(key or "wgs84").strip().lower()
    if not is_nigeria_auto_utm_coordinate_system(clean):
        return clean
    for gcp in gcps or []:
        try:
            raw_x = float(gcp.ground_x)
            raw_y = float(gcp.ground_y)
        except (TypeError, ValueError):
            continue
        return resolve_coordinate_system_key(clean, raw_x, raw_y)
    return resolve_coordinate_system_key(clean)


def _coordinate_system_to_epsg(key: str, gcps: list[GroundControlPointInput] | None = None) -> int:
    clean = _resolve_target_coordinate_system(key, gcps)
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
            text("ALTER TABLE survey_georeference_sessions ADD COLUMN IF NOT EXISTS owner_user_id BIGINT")
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
        db.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_survey_georef_owner ON survey_georeference_sessions(owner_user_id)"
            )
        )
        db.commit()
        _SCHEMA_READY = True


def _normalize_session_transform(
    row: dict[str, Any],
    transform: Any,
    gcps: list[GroundControlPointInput] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(transform, dict):
        return None
    has_map_transform = isinstance(transform.get("map_coefficients"), dict) or (
        isinstance(transform.get("map_homography"), list) and len(list(transform.get("map_homography") or [])) == 9
    )
    has_overlay_corners = isinstance(transform.get("overlay_corners"), list) and len(list(transform.get("overlay_corners") or [])) == 4
    if has_map_transform and has_overlay_corners:
        return transform
    normalized_gcps = gcps or [
        GroundControlPointInput.model_validate(item)
        for item in list(_safe_json_load(row.get("gcps_json"), []))
        if isinstance(item, dict)
    ]
    if len(normalized_gcps) < 3:
        return transform
    try:
        return _solve_affine(
            int(row.get("source_width") or 0),
            int(row.get("source_height") or 0),
            str(row.get("target_coordinate_system") or "wgs84"),
            normalized_gcps,
        )
    except Exception:
        return transform


def _session_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    ground_control_points = _safe_json_load(row.get("gcps_json"), [])
    normalized_gcps = [
        GroundControlPointInput.model_validate(item).model_dump() for item in ground_control_points if isinstance(item, dict)
    ]
    stored_overlay = _safe_json_load(row.get("overlay_json"), {}) or {}
    transform = _normalize_session_transform(
        row,
        _safe_json_load(row.get("transform_json"), None),
        [
            GroundControlPointInput.model_validate(item)
            for item in ground_control_points
            if isinstance(item, dict)
        ],
    )
    if isinstance(transform, dict):
        transform = {
            **transform,
            "residuals": [
                GroundControlPointInput.model_validate(item).model_dump()
                for item in list(transform.get("residuals") or [])
                if isinstance(item, dict)
            ],
        }
    overlay_payload = dict(stored_overlay) if isinstance(stored_overlay, dict) else {}
    if isinstance(transform, dict) and not overlay_payload.get("corners"):
        overlay_payload["corners"] = list(transform.get("overlay_corners") or [])
    if overlay_payload:
        updated_at = row.get("updated_at")
        cache_bust = int(updated_at.timestamp()) if updated_at else int(_now_utc().timestamp())
        overlay_payload["raster_url"] = f"/survey-georeference/sessions/{row.get('id')}/overlay-raster?_ts={cache_bust}"
        overlay_payload["is_warped_preview"] = bool(overlay_payload.get("preview_object_key"))
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
        "overlay": overlay_payload or None,
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


def _rebuild_overlay_preview(db: Session, row: dict[str, Any]) -> dict[str, Any]:
    """Regenerates and persists the warped overlay preview unconditionally - used both when no
    preview has ever been built (_ensure_overlay_preview_for_row's job) and when a previously
    recorded preview object can no longer be fetched from storage (stream_georeference_overlay_
    raster's self-heal path). Splitting this out means overlay_json's corners always describe
    whatever raster bytes actually end up getting streamed back, never a stale/mismatched
    footprint left over from a preview object that has since gone missing.
    """
    ground_control_points = [
        GroundControlPointInput.model_validate(item)
        for item in list(_safe_json_load(row.get("gcps_json"), []))
        if isinstance(item, dict)
    ]
    if len(ground_control_points) < 4:
        return row
    transform = _normalize_session_transform(row, _safe_json_load(row.get("transform_json"), None), ground_control_points)
    if not isinstance(transform, dict):
        return row
    try:
        next_overlay = _build_overlay_payload(row, transform, ground_control_points)
        if not str((next_overlay or {}).get("preview_object_key") or "").strip():
            return row
        db.execute(
            text(
                """
                UPDATE survey_georeference_sessions
                SET overlay_json = CAST(:overlay_json AS JSONB),
                    updated_at = NOW(),
                    last_accessed_at = NOW()
                WHERE id = :session_id
                """
            ),
            {
                "session_id": str(row.get("id") or ""),
                "overlay_json": json.dumps(next_overlay),
            },
        )
        db.commit()
        return _load_session_row(db, str(row.get("id") or ""))
    except Exception as exc:
        logger.warning("Unable to rebuild overlay preview for session %s: %s", row.get("id"), exc)
        return row


def _ensure_overlay_preview_for_row(db: Session, row: dict[str, Any]) -> dict[str, Any]:
    overlay_payload = _safe_json_load(row.get("overlay_json"), {}) or {}
    if str((overlay_payload or {}).get("preview_object_key") or "").strip():
        return row
    return _rebuild_overlay_preview(db, row)


def _make_transformers(target_epsg: int):
    if int(target_epsg) == 4326:
        return None, None
    return (
        Transformer.from_crs(4326, int(target_epsg), always_xy=True),
        Transformer.from_crs(int(target_epsg), 4326, always_xy=True),
    )


MIN_GCP_BBOX_COVERAGE_RATIO = 0.15
MAX_GCP_CENTERED_CONDITION_NUMBER = 1e6


def _evaluate_gcp_pixel_geometry(gcps: list[GroundControlPointInput], width: int, height: int) -> None:
    """Reject control-point configurations too collinear or too clustered to trust.

    An affine fit from 3 points is always mathematically exact at those 3 points
    regardless of how poorly they're placed, so a near-zero RMS alone can't catch a
    bad configuration. Two independent, scale/translation-invariant checks are used
    instead: (1) bounding-box coverage - do the points span a meaningful fraction of
    the raster, and (2) condition number of the *centered* design matrix - are the
    points meaningfully non-collinear. Thresholds were derived empirically against
    synthetic well-spread/collinear/clustered point sets across several raster sizes.
    """
    xs = np.asarray([float(g.image_x) for g in gcps], dtype=float)
    ys = np.asarray([float(g.image_y) for g in gcps], dtype=float)

    bbox_diag = float(math.hypot(float(xs.max() - xs.min()), float(ys.max() - ys.min())))
    raster_diag = float(math.hypot(max(width, 1), max(height, 1)))
    coverage_ratio = bbox_diag / raster_diag if raster_diag > 0 else 0.0

    centered_matrix = np.column_stack([np.ones_like(xs), xs - xs.mean(), ys - ys.mean()])
    try:
        condition_number = float(np.linalg.cond(centered_matrix))
    except np.linalg.LinAlgError:
        condition_number = float("inf")

    if coverage_ratio < MIN_GCP_BBOX_COVERAGE_RATIO or condition_number > MAX_GCP_CENTERED_CONDITION_NUMBER:
        raise HTTPException(
            status_code=400,
            detail="Control points are too close together or nearly in a line. Spread them across "
            "different parts of the plan - including areas near opposite corners - and try again.",
        )


def _solve_planar_model(
    gcps: list[GroundControlPointInput],
    world_pairs: list[tuple[float, float]],
) -> dict[str, Any]:
    a_matrix = np.asarray([[1.0, float(item.image_x), float(item.image_y)] for item in gcps], dtype=float)
    world_x = np.asarray([pair[0] for pair in world_pairs], dtype=float)
    world_y = np.asarray([pair[1] for pair in world_pairs], dtype=float)

    coeff_x, *_ = np.linalg.lstsq(a_matrix, world_x, rcond=None)
    coeff_y, *_ = np.linalg.lstsq(a_matrix, world_y, rcond=None)

    predicted_x = a_matrix @ coeff_x
    predicted_y = a_matrix @ coeff_y

    projective_matrix = None
    projective_condition_number = None
    if len(gcps) >= 4:
        projective_rows: list[list[float]] = []
        projective_targets: list[float] = []
        for gcp, (target_x, target_y) in zip(gcps, world_pairs):
            pixel_x = float(gcp.image_x)
            pixel_y = float(gcp.image_y)
            projective_rows.append(
                [
                    pixel_x,
                    pixel_y,
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    -pixel_x * float(target_x),
                    -pixel_y * float(target_x),
                ]
            )
            projective_targets.append(float(target_x))
            projective_rows.append(
                [
                    0.0,
                    0.0,
                    0.0,
                    pixel_x,
                    pixel_y,
                    1.0,
                    -pixel_x * float(target_y),
                    -pixel_y * float(target_y),
                ]
            )
            projective_targets.append(float(target_y))
        try:
            projective_matrix_a = np.asarray(projective_rows, dtype=float)
            projective_matrix_b = np.asarray(projective_targets, dtype=float)
            projective_solution, *_ = np.linalg.lstsq(projective_matrix_a, projective_matrix_b, rcond=None)
            projective_matrix = np.asarray(
                [
                    [projective_solution[0], projective_solution[1], projective_solution[2]],
                    [projective_solution[3], projective_solution[4], projective_solution[5]],
                    [projective_solution[6], projective_solution[7], 1.0],
                ],
                dtype=float,
            )
            projective_condition_number = float(np.linalg.cond(projective_matrix_a))
        except Exception:
            projective_matrix = None
            projective_condition_number = None

    def apply_affine(pixel_x: float, pixel_y: float) -> tuple[float, float]:
        x_world = float(coeff_x[0] + coeff_x[1] * pixel_x + coeff_x[2] * pixel_y)
        y_world = float(coeff_y[0] + coeff_y[1] * pixel_x + coeff_y[2] * pixel_y)
        return x_world, y_world

    def apply_projective(pixel_x: float, pixel_y: float) -> tuple[float, float]:
        if projective_matrix is None:
            return apply_affine(pixel_x, pixel_y)
        denominator = float(projective_matrix[2, 0] * pixel_x + projective_matrix[2, 1] * pixel_y + projective_matrix[2, 2])
        if abs(denominator) < 1e-9:
            return apply_affine(pixel_x, pixel_y)
        x_world = float((projective_matrix[0, 0] * pixel_x + projective_matrix[0, 1] * pixel_y + projective_matrix[0, 2]) / denominator)
        y_world = float((projective_matrix[1, 0] * pixel_x + projective_matrix[1, 1] * pixel_y + projective_matrix[1, 2]) / denominator)
        return x_world, y_world

    return {
        "coeff_x": coeff_x,
        "coeff_y": coeff_y,
        "predicted_x": predicted_x,
        "predicted_y": predicted_y,
        "projective_matrix": projective_matrix,
        "transform_type": "projective" if projective_matrix is not None else "affine",
        "condition_number": float(projective_condition_number if projective_condition_number is not None else np.linalg.cond(a_matrix)),
        "apply": apply_projective if projective_matrix is not None else apply_affine,
    }


def _solve_affine(width: int, height: int, coordinate_system_key: str, gcps: list[GroundControlPointInput]) -> dict[str, Any]:
    if len(gcps) < 3:
        raise HTTPException(status_code=400, detail="At least 3 control points are required.")
    if len(gcps) > MAX_GCP_COUNT:
        raise HTTPException(status_code=400, detail=f"Use at most {MAX_GCP_COUNT} control points per raster.")

    _evaluate_gcp_pixel_geometry(gcps, width, height)

    resolved_coordinate_system = _resolve_target_coordinate_system(coordinate_system_key, gcps)
    target_epsg = _coordinate_system_to_epsg(resolved_coordinate_system)
    forward_transformer, reverse_transformer = _make_transformers(target_epsg)
    mercator_forward = Transformer.from_crs(4326, 3857, always_xy=True)
    mercator_reverse = Transformer.from_crs(3857, 4326, always_xy=True)
    projected_pairs: list[tuple[float, float]] = []
    wgs84_pairs: list[tuple[float, float]] = []

    for gcp in gcps:
        if forward_transformer is None:
            lng = float(gcp.ground_x)
            lat = float(gcp.ground_y)
            projected_pairs.append((lng, lat))
            wgs84_pairs.append((lng, lat))
        elif _looks_like_projected_coordinates(gcp.ground_x, gcp.ground_y):
            projected_x = float(gcp.ground_x)
            projected_y = float(gcp.ground_y)
            projected_pairs.append((projected_x, projected_y))
            lng, lat = reverse_transformer.transform(projected_x, projected_y)
            wgs84_pairs.append((float(lng), float(lat)))
        else:
            lng = float(gcp.ground_x)
            lat = float(gcp.ground_y)
            px, py = forward_transformer.transform(lng, lat)
            projected_pairs.append((float(px), float(py)))
            wgs84_pairs.append((lng, lat))

    mercator_pairs = [
        tuple(float(value) for value in mercator_forward.transform(float(lng), float(lat)))
        for lng, lat in wgs84_pairs
    ]

    target_model = _solve_planar_model(gcps, projected_pairs)
    map_model = _solve_planar_model(gcps, mercator_pairs)

    if target_epsg == 4326:
        meter_transformer = Transformer.from_crs(4326, 3857, always_xy=True)
    transform_kind = str(target_model["transform_type"])
    transform_applier = target_model["apply"]

    residuals: list[dict[str, Any]] = []
    squared_error = 0.0
    for idx, gcp in enumerate(gcps):
        actual_world = projected_pairs[idx]
        if transform_kind == "projective":
            predicted_world = transform_applier(float(gcp.image_x), float(gcp.image_y))
        else:
            predicted_world = (
                float(target_model["predicted_x"][idx]),
                float(target_model["predicted_y"][idx]),
            )
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

    if rms_error <= 1.5 and len(gcps) >= 4:
        quality = "strong"
    elif rms_error <= 5:
        quality = "usable"
    else:
        quality = "weak"

    raster_max_x = max(float(max(width - 1, 1)), 1.0)
    raster_max_y = max(float(max(height - 1, 1)), 1.0)
    overlay_corners = []
    for pixel_x, pixel_y in [(0.0, 0.0), (raster_max_x, 0.0), (raster_max_x, raster_max_y), (0.0, raster_max_y)]:
        world_x_val, world_y_val = map_model["apply"](float(pixel_x), float(pixel_y))
        lng, lat = mercator_reverse.transform(world_x_val, world_y_val)
        overlay_corners.append([round(float(lng), 8), round(float(lat), 8)])

    return {
        "transform_type": transform_kind,
        "target_coordinate_system": coordinate_system_key,
        "resolved_coordinate_system": resolved_coordinate_system,
        "target_epsg": target_epsg,
        "coefficients": {
            "x": [round(float(value), 10) for value in target_model["coeff_x"].tolist()],
            "y": [round(float(value), 10) for value in target_model["coeff_y"].tolist()],
        },
        "homography": [
            round(float(value), 12) for value in target_model["projective_matrix"].reshape(-1).tolist()
        ]
        if target_model["projective_matrix"] is not None
        else None,
        "map_transform_type": str(map_model["transform_type"]),
        "map_coefficients": {
            "x": [round(float(value), 10) for value in map_model["coeff_x"].tolist()],
            "y": [round(float(value), 10) for value in map_model["coeff_y"].tolist()],
        },
        "map_homography": [
            round(float(value), 12) for value in map_model["projective_matrix"].reshape(-1).tolist()
        ]
        if map_model["projective_matrix"] is not None
        else None,
        "rms_error_m": round(float(rms_error), 3),
        "condition_number": round(float(target_model["condition_number"]), 4),
        "exact_fit": len(gcps) < 4,
        "quality": quality,
        "points_used": len(gcps),
        "residuals": residuals,
        "overlay_corners": overlay_corners,
    }


def _apply_transform_to_pixel(
    transform: dict[str, Any],
    pixel_x: float,
    pixel_y: float,
    *,
    space: str = "target",
) -> tuple[float, float]:
    homography_key = "map_homography" if str(space).strip().lower() == "map" else "homography"
    coefficients_key = "map_coefficients" if str(space).strip().lower() == "map" else "coefficients"
    homography = list((transform or {}).get(homography_key) or [])
    if len(homography) == 9:
        denominator = float(homography[6] * float(pixel_x) + homography[7] * float(pixel_y) + homography[8])
        if abs(denominator) >= 1e-9:
            x_world = float((homography[0] * float(pixel_x) + homography[1] * float(pixel_y) + homography[2]) / denominator)
            y_world = float((homography[3] * float(pixel_x) + homography[4] * float(pixel_y) + homography[5]) / denominator)
            return x_world, y_world
    coeff_x = list(((transform or {}).get(coefficients_key) or {}).get("x") or [])
    coeff_y = list(((transform or {}).get(coefficients_key) or {}).get("y") or [])
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
    mercator_reverse = Transformer.from_crs(3857, 4326, always_xy=True)

    target_coordinates = []
    wgs84_coordinates = []
    for point in pixels:
        world_x, world_y = _apply_transform_to_pixel(transform, point["x"], point["y"], space="target")
        target_coordinates.append([round(world_x, 6), round(world_y, 6)])
        try:
            map_x, map_y = _apply_transform_to_pixel(transform, point["x"], point["y"], space="map")
            lng, lat = mercator_reverse.transform(map_x, map_y)
        except HTTPException:
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
                    "point_type": "Digitized",
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
                "point_type": "Digitized",
            }
        )
        point_index += 1
    if not rows:
        raise HTTPException(status_code=400, detail="Add a primary polygon or point features before exporting CSV.")
    return rows


def _build_gcp_rows(gcps_raw: list[dict[str, Any]], target_coordinate_system: str) -> list[dict[str, Any]]:
    """The ground control points the surveyor input to establish the georeferencing transform -
    exported alongside the digitized/staked points (same row shape as _build_staking_rows) so a
    DGPS crew has the tie points too, not just the derived output. Reuses the same
    projected-vs-lng/lat detection _solve_affine already applies to these same raw points.
    """
    if not gcps_raw:
        return []
    target_epsg = _coordinate_system_to_epsg(target_coordinate_system)
    forward_transformer, reverse_transformer = _make_transformers(target_epsg)

    rows: list[dict[str, Any]] = []
    for idx, gcp in enumerate(gcps_raw):
        try:
            raw_x = float(gcp.get("ground_x"))
            raw_y = float(gcp.get("ground_y"))
        except (TypeError, ValueError):
            continue
        if forward_transformer is None:
            lng, lat = raw_x, raw_y
            easting, northing = raw_x, raw_y
        elif _looks_like_projected_coordinates(raw_x, raw_y):
            easting, northing = raw_x, raw_y
            lng, lat = reverse_transformer.transform(raw_x, raw_y)
        else:
            lng, lat = raw_x, raw_y
            easting, northing = forward_transformer.transform(raw_x, raw_y)
        rows.append(
            {
                "station": f"GCP{idx + 1}",
                "feature": str(gcp.get("label") or f"GCP {idx + 1}"),
                "coordinate_system": target_coordinate_system,
                "easting": round(float(easting), 4),
                "northing": round(float(northing), 4),
                "longitude": round(float(lng), 8),
                "latitude": round(float(lat), 8),
                "point_type": "GCP",
            }
        )
    return rows


def _raster_object_key(session_id: str, filename: str) -> str:
    stamp = _now_utc().strftime("%Y%m%dT%H%M%SZ")
    safe_name = _safe_slug(filename, "raster")
    return "/".join([R2_PREFIX, _now_utc().strftime("%Y"), _now_utc().strftime("%m"), f"{session_id}_{stamp}_{safe_name}"])


def _overlay_preview_object_key(session_id: str) -> str:
    stamp = _now_utc().strftime("%Y%m%dT%H%M%SZ")
    return "/".join([R2_PREFIX, _now_utc().strftime("%Y"), _now_utc().strftime("%m"), f"{session_id}_{stamp}_overlay_preview.png"])


def _download_r2_bytes(settings: dict[str, Any], object_key: str) -> bytes:
    client = create_r2_client(settings)
    obj = client.get_object(Bucket=settings["bucket"], Key=object_key)
    return bytes(obj["Body"].read())


def _signed_triangle_area(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return ((b[0] - a[0]) * (c[1] - a[1])) - ((b[1] - a[1]) * (c[0] - a[0]))


def _ensure_triangle_ccw(
    triangle: tuple[int, int, int],
    points: list[tuple[float, float]],
) -> tuple[int, int, int]:
    a, b, c = triangle
    if _signed_triangle_area(points[a], points[b], points[c]) < 0:
        return a, c, b
    return triangle


def _circumcircle_contains(
    point: tuple[float, float],
    triangle: tuple[int, int, int],
    points: list[tuple[float, float]],
) -> bool:
    ax = points[triangle[0]][0] - point[0]
    ay = points[triangle[0]][1] - point[1]
    bx = points[triangle[1]][0] - point[0]
    by = points[triangle[1]][1] - point[1]
    cx = points[triangle[2]][0] - point[0]
    cy = points[triangle[2]][1] - point[1]

    determinant = (
        (ax * ax + ay * ay) * (bx * cy - by * cx)
        - (bx * bx + by * by) * (ax * cy - ay * cx)
        + (cx * cx + cy * cy) * (ax * by - ay * bx)
    )
    orientation = _signed_triangle_area(points[triangle[0]], points[triangle[1]], points[triangle[2]])
    epsilon = 1e-12
    return determinant > epsilon if orientation >= 0 else determinant < -epsilon


def _build_delaunay_triangles(points: list[tuple[float, float]]) -> list[tuple[int, int, int]]:
    if len(points) < 3:
        return []
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    delta = max(max_x - min_x, max_y - min_y, 1.0)
    mid_x = (min_x + max_x) / 2.0
    mid_y = (min_y + max_y) / 2.0

    super_triangle = [
        (mid_x - (24.0 * delta), mid_y - (8.0 * delta)),
        (mid_x, mid_y + (24.0 * delta)),
        (mid_x + (24.0 * delta), mid_y - (8.0 * delta)),
    ]
    work_points = list(points) + super_triangle
    point_count = len(points)
    triangles: list[tuple[int, int, int]] = [
        _ensure_triangle_ccw((point_count, point_count + 1, point_count + 2), work_points)
    ]

    for point_index, point in enumerate(points):
        bad_triangles = [triangle for triangle in triangles if _circumcircle_contains(point, triangle, work_points)]
        boundary_counts: dict[tuple[int, int], int] = {}
        for triangle in bad_triangles:
            for edge in ((triangle[0], triangle[1]), (triangle[1], triangle[2]), (triangle[2], triangle[0])):
                key = tuple(sorted(edge))
                boundary_counts[key] = boundary_counts.get(key, 0) + 1
        triangles = [triangle for triangle in triangles if triangle not in bad_triangles]
        for edge, count in boundary_counts.items():
            if count != 1:
                continue
            next_triangle = _ensure_triangle_ccw((edge[0], edge[1], point_index), work_points)
            if abs(_signed_triangle_area(work_points[next_triangle[0]], work_points[next_triangle[1]], work_points[next_triangle[2]])) <= 1e-10:
                continue
            triangles.append(next_triangle)

    finalized: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    for triangle in triangles:
        if any(vertex >= point_count for vertex in triangle):
            continue
        normalized = _ensure_triangle_ccw(triangle, work_points)
        if normalized in seen:
            continue
        seen.add(normalized)
        finalized.append(normalized)
    return finalized


def _sample_rgba_bilinear(source_rgba: np.ndarray, sample_x: np.ndarray, sample_y: np.ndarray) -> np.ndarray:
    height, width = source_rgba.shape[:2]
    clipped_x = np.clip(sample_x.astype(np.float64), 0.0, max(0.0, float(width - 1)))
    clipped_y = np.clip(sample_y.astype(np.float64), 0.0, max(0.0, float(height - 1)))
    x0 = np.floor(clipped_x).astype(np.int32)
    y0 = np.floor(clipped_y).astype(np.int32)
    x1 = np.clip(x0 + 1, 0, max(0, width - 1))
    y1 = np.clip(y0 + 1, 0, max(0, height - 1))
    weight_x = (clipped_x - x0.astype(np.float64))[:, None]
    weight_y = (clipped_y - y0.astype(np.float64))[:, None]
    top = source_rgba[y0, x0] * (1.0 - weight_x) + source_rgba[y0, x1] * weight_x
    bottom = source_rgba[y1, x0] * (1.0 - weight_x) + source_rgba[y1, x1] * weight_x
    sampled = top * (1.0 - weight_y) + bottom * weight_y
    return np.clip(sampled, 0.0, 255.0).astype(np.uint8)


def _build_warped_overlay_preview(
    source_payload: bytes,
    session_transform: dict[str, Any],
    gcps: list[GroundControlPointInput],
    *,
    width: int,
    height: int,
) -> tuple[bytes, list[list[float]]]:
    if len(gcps) < 4:
        raise ValueError("A warped overlay preview requires at least four control points.")

    mercator_reverse = Transformer.from_crs(3857, 4326, always_xy=True)
    source_image = Image.open(io.BytesIO(source_payload)).convert("RGBA")
    source_width, source_height = source_image.size
    source_rgba = np.asarray(source_image, dtype=np.float32)
    raster_max_x = max(1.0, float(max(int(width or 0), source_width) - 1))
    raster_max_y = max(1.0, float(max(int(height or 0), source_height) - 1))
    raster_corners = [
        (0.0, 0.0),
        (raster_max_x, 0.0),
        (raster_max_x, raster_max_y),
        (0.0, raster_max_y),
    ]

    map_corners = [
        _apply_transform_to_pixel(session_transform, pixel_x, pixel_y, space="map")
        for pixel_x, pixel_y in raster_corners
    ]
    map_points = list(map_corners)
    for gcp in gcps:
        try:
            map_points.append(
                _apply_transform_to_pixel(
                    session_transform,
                    float(gcp.image_x),
                    float(gcp.image_y),
                    space="map",
                )
            )
        except HTTPException:
            continue

    min_x = min(point[0] for point in map_points)
    max_x = max(point[0] for point in map_points)
    min_y = min(point[1] for point in map_points)
    max_y = max(point[1] for point in map_points)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)

    aspect_ratio = span_x / span_y if span_y > 0 else (float(source_width) / float(max(1, source_height)))
    long_edge = min(PREVIEW_MAX_DIMENSION, max(PREVIEW_MIN_DIMENSION, max(source_width, source_height)))
    if aspect_ratio >= 1.0:
        output_width = int(long_edge)
        output_height = max(PREVIEW_MIN_DIMENSION // 2, int(round(output_width / max(aspect_ratio, 1e-6))))
    else:
        output_height = int(long_edge)
        output_width = max(PREVIEW_MIN_DIMENSION // 2, int(round(output_height * max(aspect_ratio, 1e-6))))
    output_width = max(512, min(PREVIEW_MAX_DIMENSION, output_width))
    output_height = max(512, min(PREVIEW_MAX_DIMENSION, output_height))

    grid_x, grid_y = np.meshgrid(
        np.arange(output_width, dtype=np.float64) + 0.5,
        np.arange(output_height, dtype=np.float64) + 0.5,
    )
    world_x = min_x + (grid_x / float(output_width)) * span_x
    world_y = max_y - (grid_y / float(output_height)) * span_y

    map_homography = list(session_transform.get("map_homography") or [])
    sample_x = np.zeros_like(world_x, dtype=np.float64)
    sample_y = np.zeros_like(world_y, dtype=np.float64)
    valid_mask = np.zeros_like(world_x, dtype=bool)

    if len(map_homography) == 9:
        homography_matrix = np.asarray(map_homography, dtype=float).reshape((3, 3))
        if abs(float(np.linalg.det(homography_matrix))) <= 1e-12:
            raise ValueError("The solved map homography is singular.")
        inverse_homography = np.linalg.inv(homography_matrix)
        denominator = (
            inverse_homography[2, 0] * world_x
            + inverse_homography[2, 1] * world_y
            + inverse_homography[2, 2]
        )
        valid_mask = np.abs(denominator) > 1e-9
        safe_denominator = np.where(valid_mask, denominator, 1.0)
        sample_x = (
            inverse_homography[0, 0] * world_x
            + inverse_homography[0, 1] * world_y
            + inverse_homography[0, 2]
        ) / safe_denominator
        sample_y = (
            inverse_homography[1, 0] * world_x
            + inverse_homography[1, 1] * world_y
            + inverse_homography[1, 2]
        ) / safe_denominator
    else:
        coeff_x = list(((session_transform or {}).get("map_coefficients") or {}).get("x") or [])
        coeff_y = list(((session_transform or {}).get("map_coefficients") or {}).get("y") or [])
        if len(coeff_x) != 3 or len(coeff_y) != 3:
            raise ValueError("The solved map transform is incomplete.")
        affine_matrix = np.asarray(
            [
                [float(coeff_x[1]), float(coeff_x[2])],
                [float(coeff_y[1]), float(coeff_y[2])],
            ],
            dtype=float,
        )
        if abs(float(np.linalg.det(affine_matrix))) <= 1e-12:
            raise ValueError("The solved affine transform is singular.")
        inverse_affine = np.linalg.inv(affine_matrix)
        translated_x = world_x - float(coeff_x[0])
        translated_y = world_y - float(coeff_y[0])
        sample_x = (inverse_affine[0, 0] * translated_x) + (inverse_affine[0, 1] * translated_y)
        sample_y = (inverse_affine[1, 0] * translated_x) + (inverse_affine[1, 1] * translated_y)
        valid_mask = np.isfinite(sample_x) & np.isfinite(sample_y)

    pixel_limit_x = float(max(source_width - 1, 0))
    pixel_limit_y = float(max(source_height - 1, 0))
    inside_mask = (
        valid_mask
        & np.isfinite(sample_x)
        & np.isfinite(sample_y)
        & (sample_x >= 0.0)
        & (sample_x <= pixel_limit_x)
        & (sample_y >= 0.0)
        & (sample_y <= pixel_limit_y)
    )

    preview_rgba = np.zeros((output_height, output_width, 4), dtype=np.uint8)
    if bool(np.any(inside_mask)):
        sampled = _sample_rgba_bilinear(source_rgba, sample_x[inside_mask], sample_y[inside_mask])
        preview_rgba[inside_mask] = sampled

    preview_image = Image.fromarray(preview_rgba, mode="RGBA")
    output_buffer = io.BytesIO()
    preview_image.save(output_buffer, format="PNG", optimize=True)

    overlay_corners: list[list[float]] = []
    for corner_x, corner_y in ((min_x, max_y), (max_x, max_y), (max_x, min_y), (min_x, min_y)):
        lng, lat = mercator_reverse.transform(corner_x, corner_y)
        overlay_corners.append([round(float(lng), 8), round(float(lat), 8)])

    return output_buffer.getvalue(), overlay_corners


def _build_overlay_payload(
    row: dict[str, Any],
    session_transform: dict[str, Any],
    gcps: list[GroundControlPointInput],
) -> dict[str, Any]:
    overlay_payload: dict[str, Any] = {
        "corners": list(session_transform.get("overlay_corners") or []),
    }
    previous_overlay = _safe_json_load(row.get("overlay_json"), {}) or {}
    previous_preview_key = str(previous_overlay.get("preview_object_key") or "").strip()
    settings = _build_r2()

    if len(gcps) >= 4 and str(row.get("source_object_key") or "").strip():
        try:
            source_payload = _download_r2_bytes(settings, str(row.get("source_object_key") or ""))
            preview_bytes, preview_corners = _build_warped_overlay_preview(
                source_payload,
                session_transform,
                gcps,
                width=int(row.get("source_width") or 0),
                height=int(row.get("source_height") or 0),
            )
            preview_key = _overlay_preview_object_key(str(row.get("id") or uuid.uuid4().hex))
            upload_bytes(settings, preview_key, preview_bytes, content_type="image/png")
            overlay_payload = {
                "corners": preview_corners,
                "preview_object_key": preview_key,
                "preview_content_type": "image/png",
            }
            if previous_preview_key and previous_preview_key != preview_key:
                delete_object_best_effort(settings, previous_preview_key)
        except Exception as exc:
            logger.warning("Unable to build warped georeference overlay preview for session %s: %s", row.get("id"), exc)
            if previous_preview_key:
                delete_object_best_effort(settings, previous_preview_key)
    elif previous_preview_key:
        delete_object_best_effort(settings, previous_preview_key)

    return overlay_payload


def _delete_overlay_preview_object(settings: dict[str, Any] | None, overlay_payload: Any) -> bool:
    if not settings or not isinstance(overlay_payload, dict):
        return False
    object_key = str(overlay_payload.get("preview_object_key") or "").strip()
    if not object_key:
        return False
    return delete_object_best_effort(settings, object_key)


def cleanup_expired_georeference_sessions(db: Session) -> dict[str, int]:
    _ensure_schema(db)
    rows = (
        db.execute(
            text(
                """
                SELECT id, source_object_key, overlay_json
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
        if _delete_overlay_preview_object(settings, _safe_json_load(row.get("overlay_json"), None)):
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
    request: Request,
    file: UploadFile = File(...),
    title_text: str | None = Form(None),
    target_coordinate_system: str = Form("wgs84"),
    db: Session = Depends(get_db),
):
    _ensure_schema(db)

    # Anonymous creation stays fully supported (same "value first" pattern as regular plots -
    # see create_plot in plots.py) - this only stamps ownership when the request happens to
    # already carry a signed-in Survey session, so the dashboard can list it under "mine"
    # immediately instead of relying on the claim-after-login step below.
    owner_user_id = None
    try:
        survey_session = resolve_survey_session(db, request)
        owner_user_id = survey_session.user_id if survey_session else None
    except Exception:
        owner_user_id = None
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
                delete_after_at,
                owner_user_id
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
                :delete_after_at,
                :owner_user_id
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
            "owner_user_id": owner_user_id,
        },
    )
    db.commit()
    row = _load_session_row(db, session_id)
    log_survey_activity(
        db,
        event_type="raster_uploaded",
        workflow="georeference",
        request=request,
        georeference_session_id=session_id,
        details={"file_name": str(file.filename or ""), "coordinate_system": target_coordinate_system},
    )
    return {"ok": True, "session": _session_to_payload(row)}


@router.post("/sessions/claim")
def claim_georeference_sessions(request: Request, session_ids: list[str] = Body(..., embed=True), db: Session = Depends(get_db)):
    """Mirrors POST /plots/claim - a session started before the surveyor logged in has no
    owner_user_id yet (only tracked by id in this browser's local draft list, see
    saveGeorefSessionToStorage in SurveyPlan.tsx), so this attaches it to their account right
    after sign-in the same way regular plots get claimed."""
    _ensure_schema(db)
    session = require_survey_session(db, request)
    ensure_survey_activity_table(db)
    if not session_ids:
        return {"claimed": []}
    rows = db.execute(
        text(
            """
            UPDATE survey_georeference_sessions
            SET owner_user_id = :user_id
            WHERE id = ANY(:session_ids) AND owner_user_id IS NULL
            RETURNING id
            """
        ),
        {"user_id": session.user_id, "session_ids": [str(s) for s in session_ids]},
    ).mappings().all()
    claimed_ids = [str(row["id"]) for row in rows]
    if claimed_ids:
        db.execute(
            text("""
                UPDATE survey_activity_events
                SET actor_user_id = :user_id
                WHERE actor_user_id IS NULL
                  AND georeference_session_id = ANY(:session_ids)
            """),
            {"user_id": session.user_id, "session_ids": claimed_ids},
        )
    db.commit()
    return {"claimed": claimed_ids}


@router.get("/sessions/mine")
def list_my_georeference_sessions(request: Request, db: Session = Depends(get_db)):
    _ensure_schema(db)
    session = require_survey_session(db, request)
    rows = db.execute(
        text(
            """
            SELECT id, title_text, status, target_coordinate_system, source_file_name,
                   created_at, updated_at, finalized_at
            FROM survey_georeference_sessions
            WHERE owner_user_id = :user_id
            ORDER BY updated_at DESC
            """
        ),
        {"user_id": session.user_id},
    ).mappings().all()
    return {
        "sessions": [
            {
                "session_id": str(row["id"]),
                "title": row["title_text"],
                "status": row["status"],
                "coordinate_system": row["target_coordinate_system"],
                "source_file_name": row["source_file_name"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                "finalized_at": row["finalized_at"].isoformat() if row["finalized_at"] else None,
            }
            for row in rows
        ]
    }


@router.get("/sessions/{session_id}")
def get_georeference_session(session_id: str, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    row = _ensure_overlay_preview_for_row(db, row)
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


@router.get("/sessions/{session_id}/overlay-raster")
def stream_georeference_overlay_raster(session_id: str, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    settings = _build_r2()

    def _stream_object(object_key: str, media_type: str) -> StreamingResponse:
        client = create_r2_client(settings)
        obj = client.get_object(Bucket=settings["bucket"], Key=object_key)
        _touch_session(db, session_id)
        return StreamingResponse(obj["Body"].iter_chunks(chunk_size=1024 * 512), media_type=media_type)

    overlay_payload = _safe_json_load(row.get("overlay_json"), {}) or {}
    preview_object_key = str(overlay_payload.get("preview_object_key") or "").strip()
    preview_content_type = str(overlay_payload.get("preview_content_type") or "image/png").strip() or "image/png"

    if preview_object_key:
        try:
            return _stream_object(preview_object_key, preview_content_type)
        except Exception as exc:
            # The recorded preview object is missing/unreachable in storage even though
            # overlay_json's corners describe a footprint sized and rotated specifically for THAT
            # warped preview - silently falling back to the raw, unwarped source raster here would
            # still get drawn into that same corner box, stretching/squashing a rotated plan into
            # something that can look like a narrow strip. Rebuild the preview (and its matching
            # corners together) instead of streaming raster bytes that no longer match them.
            logger.warning("Overlay preview object missing for session %s, rebuilding: %s", session_id, exc)
            healed_row = _rebuild_overlay_preview(db, row)
            healed_overlay = _safe_json_load(healed_row.get("overlay_json"), {}) or {}
            healed_key = str(healed_overlay.get("preview_object_key") or "").strip()
            if healed_key and healed_key != preview_object_key:
                try:
                    return _stream_object(
                        healed_key,
                        str(healed_overlay.get("preview_content_type") or "image/png").strip() or "image/png",
                    )
                except Exception as retry_exc:
                    logger.warning("Rebuilt overlay preview still unreachable for session %s: %s", session_id, retry_exc)

    source_object_key = str(row.get("source_object_key") or "").strip()
    if source_object_key:
        try:
            return _stream_object(source_object_key, str(row.get("source_content_type") or "application/octet-stream"))
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Unable to stream overlay raster from storage: {exc}") from exc
    raise HTTPException(status_code=404, detail="Raster overlay preview is no longer available.")


@router.post("/sessions/{session_id}/solve")
def solve_georeference_session(session_id: str, payload: SolveGeoreferenceRequest, request: Request, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    width = int(row.get("source_width") or 0)
    height = int(row.get("source_height") or 0)
    transform = _solve_affine(width, height, payload.target_coordinate_system, payload.ground_control_points)
    overlay_payload = _build_overlay_payload(row, transform, payload.ground_control_points)
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
            "overlay_json": json.dumps(overlay_payload),
            "delete_after_at": delete_after_at,
        },
    )
    db.commit()
    updated = _load_session_row(db, session_id)
    log_survey_activity(
        db,
        event_type="georeference_solved",
        workflow="georeference",
        request=request,
        georeference_session_id=session_id,
        details={"gcp_count": len(payload.ground_control_points), "coordinate_system": payload.target_coordinate_system},
    )
    return {"ok": True, "session": _session_to_payload(updated)}


@router.post("/sessions/{session_id}/features")
def save_georeference_features(session_id: str, payload: SaveDigitizedFeaturesRequest, request: Request, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    transform = _normalize_session_transform(row, _safe_json_load(row.get("transform_json"), None))
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
    log_survey_activity(
        db,
        event_type="digitizing_saved",
        workflow="georeference",
        request=request,
        georeference_session_id=session_id,
        details={"feature_count": len(features)},
    )
    return {"ok": True, "session": _session_to_payload(updated)}


def _ai_digitize_rate_limit_identity(request: Request, db: Session) -> str:
    """Same identity resolution as plan_reader.py's/field_to_finish.py's routers - signed-in
    surveyor's user id when available, otherwise the caller's IP as a soft per-machine limit. The
    "georefdigitize:" prefix keeps this feature's daily-usage bucket separate from every other AI
    feature's, even though they all share the same plan_reader_usage table/consume_daily_reading
    helper - without it, an "AI Digitize" run and a "Plan Reader" run from the same user would
    silently draw from and deplete the same counter instead of their own independent 2/day caps.
    """
    try:
        survey_session = resolve_survey_session(db, request)
    except Exception:
        survey_session = None
    if survey_session:
        return f"georefdigitize:user:{survey_session.user_id}"
    client_host = request.client.host if request.client else "unknown"
    return f"georefdigitize:ip:{client_host}"


@router.post("/sessions/{session_id}/ai-digitize")
async def ai_digitize_georeference_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    """Sends the session's anchored raster to Gemini vision to visually locate boundary-corner and
    stake markers, then runs each detected point through the exact same pixel->coordinate
    conversion (_feature_to_saved_payload) the manual digitizing tool already uses. Returns draft
    features only - nothing is written to the session here; the surveyor reviews/edits them using
    the existing digitizing tools and saves via the existing /features endpoint, same "AI proposes,
    human confirms" discipline as every other AI feature in this app.
    """
    row = _load_session_row(db, session_id)
    transform = _normalize_session_transform(row, _safe_json_load(row.get("transform_json"), None))
    if not isinstance(transform, dict):
        raise HTTPException(status_code=400, detail="Anchor the raster (solve a transform) before running AI Digitize.")

    identity_key = _ai_digitize_rate_limit_identity(request, db)
    new_count = consume_daily_reading(db, identity_key, AI_DIGITIZE_DAILY_LIMIT)
    if new_count is None:
        raise HTTPException(
            status_code=429,
            detail=f"You've used all {AI_DIGITIZE_DAILY_LIMIT} AI digitize runs for today. Please try again tomorrow.",
        )

    settings = _build_r2()
    object_key = str(row.get("source_object_key") or "")
    if not object_key:
        raise HTTPException(status_code=404, detail="Raster source is no longer available.")

    try:
        raster_bytes = _download_r2_bytes(settings, object_key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unable to load the raster from storage: {exc}") from exc

    try:
        # Blocking requests.post call - same event-loop-blocking risk plan_reader.py's router
        # comment documents, so it's offloaded to a thread the same way.
        result = await run_in_threadpool(
            detect_digitized_points, raster_bytes, str(row.get("source_content_type") or "image/jpeg")
        )
    except GeoreferenceAiDigitizeError as exc:
        logger.warning("AI digitize failed for session %s: %s", session_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    width = int(row.get("source_width") or 0)
    height = int(row.get("source_height") or 0)
    raw_points = result.get("points") or []

    boundary_pixels: list[PixelPointInput] = []
    other_features: list[DigitizedFeatureInput] = []
    stake_counter = 0
    for point in raw_points:
        try:
            pixel = PixelPointInput(
                x=float(point.get("x_frac") or 0.0) * width,
                y=float(point.get("y_frac") or 0.0) * height,
            )
        except (TypeError, ValueError):
            continue
        feature_type = str(point.get("feature_type") or "").strip().lower()
        if feature_type == "boundary_corner":
            boundary_pixels.append(pixel)
        else:
            stake_counter += 1
            other_features.append(
                DigitizedFeatureInput(
                    label=str(point.get("label") or f"AI Stake {stake_counter}").strip(),
                    feature_type="point",
                    pixels=[pixel],
                    is_primary=False,
                )
            )

    draft_inputs: list[DigitizedFeatureInput] = []
    if len(boundary_pixels) >= 3:
        draft_inputs.append(
            DigitizedFeatureInput(
                label="AI Detected Boundary",
                feature_type="polygon",
                pixels=boundary_pixels,
                is_primary=True,
            )
        )
    draft_inputs.extend(other_features)

    draft_features = [_feature_to_saved_payload(feature, transform) for feature in draft_inputs]

    log_survey_activity(
        db,
        event_type="georeference_ai_digitize",
        workflow="georeference",
        request=request,
        georeference_session_id=session_id,
        details={"detected_point_count": len(raw_points), "draft_feature_count": len(draft_features)},
    )

    return {
        "draft_features": draft_features,
        "notes": result.get("notes") or [],
        "digitize_runs_remaining_today": max(0, AI_DIGITIZE_DAILY_LIMIT - new_count),
    }


def _format_coordinate_number(value: float, decimals: int) -> str:
    # Plain "." decimal formatting, no thousands separator and no Excel formula-forcing wrapper -
    # this file's whole purpose is DGPS/GIS ingestion (QGIS, AutoCAD Civil 3D, DGPS receivers,
    # etc.), and every one of those expects a bare numeric token like "211213.1260". A
    # comma-grouped "211,213.1260" or an ="..." formula string is not a valid number to any of
    # them - it would only ever have helped Excel specifically, at the cost of breaking the export
    # for the software it's actually meant for.
    return f"{float(value):.{decimals}f}"


@router.get("/sessions/{session_id}/exports/staking.csv")
def export_georeference_staking_csv(session_id: str, request: Request, raw: bool = False, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    target_coordinate_system = str(row.get("target_coordinate_system") or "wgs84")
    transform_json = _safe_json_load(row.get("transform_json"), {}) or {}
    resolved_coordinate_system = str(
        transform_json.get("resolved_coordinate_system")
        or transform_json.get("target_coordinate_system")
        or target_coordinate_system
    )
    features = _safe_json_load(row.get("features_json"), [])
    gcps_raw = _safe_json_load(row.get("gcps_json"), [])

    gcp_rows = _build_gcp_rows(gcps_raw, resolved_coordinate_system)
    try:
        staking_rows = _build_staking_rows(features, resolved_coordinate_system)
    except HTTPException:
        # No digitized features yet isn't fatal here - the GCPs entered during georeferencing are
        # still worth exporting on their own; only error out if there's genuinely nothing at all.
        staking_rows = []
    rows = gcp_rows + staking_rows
    if not rows:
        raise HTTPException(status_code=400, detail="Add ground control points or digitized features before exporting CSV.")

    csv_buffer = io.StringIO(newline="")
    # "sep=," as the literal first line forces Excel to parse this as comma-delimited on
    # double-click regardless of the machine's regional list separator - the fix for "everything
    # lands in column A" that most LandCheck Survey users hit, since most open this in Excel, not
    # GIS software. A GIS/DGPS import tool that needs the strict form (no hint line, since some
    # CSV readers treat it as a malformed data row) can pass ?raw=1.
    if not raw:
        csv_buffer.write("sep=,\r\n")
    writer = csv.writer(csv_buffer, lineterminator="\r\n")
    writer.writerow(["Type", "Station", "Feature", "Coordinate System", "Easting (m)", "Northing (m)", "Longitude", "Latitude"])
    for item in rows:
        writer.writerow(
            [
                item.get("point_type", ""),
                item["station"],
                item["feature"],
                str(item["coordinate_system"]).upper(),
                _format_coordinate_number(item["easting"], 4),
                _format_coordinate_number(item["northing"], 4),
                _format_coordinate_number(item["longitude"], 8),
                _format_coordinate_number(item["latitude"], 8),
            ]
        )
    _touch_session(db, session_id)
    # Mirrors the frontend's own georeference identity fallback chain (SurveyPlan.tsx): session
    # title, then the originally-uploaded raster's filename (extension stripped), then a short
    # session-id tag - instead of the full opaque session id used here before.
    identity_source = str(row.get("title_text") or "").strip()
    if not identity_source:
        source_name = str(row.get("source_file_name") or "").strip()
        identity_source = os.path.splitext(source_name)[0] if source_name else ""
    identity = _safe_filename_fragment(identity_source, f"Session_{str(session_id)[:8]}")
    log_survey_activity(
        db,
        event_type="export_downloaded",
        workflow="georeference",
        request=request,
        georeference_session_id=session_id,
        details={"export_type": "staking.csv", "row_count": len(rows)},
    )
    return Response(
        content="\ufeff" + csv_buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{identity}_DGPS_Staking.csv"',
            # This export reflects live, editable session data - a browser or edge cache (e.g.
            # Cloudflare, in front of this API) serving a stale copy by URL would look exactly
            # like "the fix didn't deploy" even after it has. Never cache it.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@router.delete("/sessions/{session_id}")
def delete_georeference_session(session_id: str, db: Session = Depends(get_db)):
    row = _load_session_row(db, session_id)
    settings = build_r2_settings(prefix=R2_SETTINGS_PREFIX)
    if settings and row.get("source_object_key"):
        delete_object_best_effort(settings, str(row.get("source_object_key") or ""))
    _delete_overlay_preview_object(settings, _safe_json_load(row.get("overlay_json"), None))
    db.execute(text("DELETE FROM survey_georeference_sessions WHERE id = :session_id"), {"session_id": session_id})
    db.commit()
    return {"ok": True}
