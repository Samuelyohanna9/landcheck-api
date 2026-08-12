from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from shapely import wkb
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.orm import Session

# Shared 4-tier risk scale used by every hazard module (flood, erosion, ...) so a client sees
# one consistent vocabulary and palette across the whole hazard report suite, not a different
# scale per hazard type.
RISK_TIERS = [
    (0.25, "Low", "#22c55e"),
    (0.50, "Moderate", "#f59e0b"),
    (0.75, "High", "#f97316"),
    (1.01, "Severe", "#ef4444"),
]
NO_DATA_COLOR = "#94a3b8"


def classify_risk(value: float, data_available: bool = True) -> Tuple[str, str]:
    """Maps a 0-1 risk score to (label, hex color). data_available=False always returns the
    "No Data" tier regardless of value, since a 0.0 score from missing data must never be
    displayed the same way as a genuine "Low" score.
    """
    if not data_available:
        return "No Data", NO_DATA_COLOR
    safe_value = max(0.0, min(1.0, float(value)))
    for ceiling, label, color in RISK_TIERS:
        if safe_value < ceiling:
            return label, color
    return "Severe", "#ef4444"


def risk_tier_legend() -> list[dict]:
    labels_seen = set()
    legend = []
    for _, label, color in RISK_TIERS:
        if label in labels_seen:
            continue
        labels_seen.add(label)
        legend.append({"label": label, "color": color})
    return legend


def fetch_buildings_near(db: Session, boundary_geojson: Dict[str, Any], buffer_m: float = 500, limit: int = 1500) -> List[BaseGeometry]:
    """Real OSM building footprint polygons (EPSG:4326) intersecting a metric buffer around a
    hazard boundary - the same `multipolygons` table Survey Plan's Auto Feature Detection already
    reads from (see plots.py's _run_plot_feature_detection), just queried directly against an
    arbitrary boundary instead of a saved plot row.

    The row limit exists so a plot dropped in the middle of a dense city center can't turn a
    single hazard request into a multi-thousand-polygon query-and-plot that blows past a client
    timeout - 1500 buildings is already far more than fit legibly on the rendered map anyway.
    """
    rows = db.execute(
        text(
            """
            SELECT m.geom
            FROM multipolygons m
            WHERE m.building IS NOT NULL
              AND ST_Intersects(
                  m.geom,
                  ST_Buffer(
                      ST_SetSRID(ST_GeomFromGeoJSON(:boundary_geojson), 4326)::geography,
                      :buffer_m
                  )::geometry
              )
            LIMIT :limit
            """
        ),
        {"boundary_geojson": json.dumps(boundary_geojson), "buffer_m": buffer_m, "limit": limit},
    ).fetchall()
    geometries = []
    for row in rows:
        try:
            geometries.append(wkb.loads(row[0]))
        except Exception:
            continue
    return geometries
