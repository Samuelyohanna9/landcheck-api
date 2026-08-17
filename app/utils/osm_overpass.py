# app/utils/osm_overpass.py
"""Live OSM feature fetching for countries not bulk-imported into local PostGIS.

Today only Nigeria has been imported wholesale into `multipolygons`/`lines` (see
_run_plot_feature_detection in plots.py). Rather than importing every other African country's
OSM extract up front - real storage cost for countries that might turn out to have one plot ever
- this module fetches buildings/roads/rivers from the public Overpass API on demand, the first
time a plot shows up in a given area, and caches the raw results locally so every later plot
nearby is served from Postgres at the same speed as the Nigeria path.

Caching granularity: a plot's own buffer is tiny (~150m across - see plot_buffers, a 50m ring
around the plot). Fetching from Overpass per-plot would be wasteful and slow for the next surveyor
working two streets over. Instead this fetches a whole coarse grid cell ("bucket",
REGION_BUCKET_DEG wide, ~5.5km) around the plot's centroid once, and reuses it for CACHE_MAX_AGE_DAYS.

Failure handling: every Overpass call is best-effort. A slow/unreachable Overpass endpoint must
never block plot creation - it should just mean this plot gets no auto-detected features, same as
a legitimately empty area.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

REGION_BUCKET_DEG = 0.02  # ~2.2km grid cells - smaller than the first cut (0.05deg/~5.5km), which
                          # took 60-80s to fetch for a single dense-city bucket (e.g. central Accra,
                          # ~5000 buildings), since Overpass server-side processing time and payload
                          # size scale with area. Still much larger than a plot's ~150m buffer, so
                          # most plots in the same neighborhood still share one already-cached bucket.
CACHE_MAX_AGE_DAYS = 90
OVERPASS_TIMEOUT_S = 12  # passed to osmnx as both the HTTP client timeout AND the embedded
                         # Overpass QL `[timeout:N]` server-side hint - neither is a hard wall-
                         # clock deadline on its own (requests' timeout is inter-chunk, not total;
                         # Overpass's is a soft "try to abort around here"), which is why
                         # _fetch_bucket_from_overpass also wraps the call in a hard thread timeout.
OVERPASS_HARD_TIMEOUT_S = 25  # absolute ceiling regardless of what the HTTP/Overpass layers do -
                              # see _fetch_bucket_from_overpass. Only matters when this ends up
                              # running inline (no BackgroundTasks available) - see
                              # _run_plot_feature_detection in plots.py, which is the normal path.

_SCHEMA_READY = False

# Rough bounding boxes, only used to label usage-log rows for the admin "should we bulk-import
# this country yet" report - never used for anything that affects detection correctness itself.
_COUNTRY_HINT_BOXES = [
    ("Nigeria", (2.5, 4.0, 14.7, 14.1)),
    ("Ghana", (-3.3, 4.5, 1.3, 11.2)),
    ("Uganda", (29.5, -1.5, 35.1, 4.3)),
]


def _rough_country_hint(lat: float, lon: float) -> str:
    for name, (min_lon, min_lat, max_lon, max_lat) in _COUNTRY_HINT_BOXES:
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
            return name
    return "Other"


def ensure_osm_overpass_schema(db: Session) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS osm_overpass_cache_buckets (
            bucket_key TEXT PRIMARY KEY,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            feature_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ok'
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS osm_overpass_cache_features (
            id SERIAL PRIMARY KEY,
            bucket_key TEXT NOT NULL REFERENCES osm_overpass_cache_buckets(bucket_key) ON DELETE CASCADE,
            feature_type TEXT NOT NULL,
            name TEXT,
            subtype TEXT,
            geom GEOMETRY(GEOMETRY, 4326) NOT NULL
        )
    """))
    db.execute(text("ALTER TABLE osm_overpass_cache_features ADD COLUMN IF NOT EXISTS name TEXT"))
    db.execute(text("ALTER TABLE osm_overpass_cache_features ADD COLUMN IF NOT EXISTS subtype TEXT"))
    db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_osm_overpass_cache_features_geom "
        "ON osm_overpass_cache_features USING GIST (geom)"
    ))
    db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_osm_overpass_cache_features_bucket "
        "ON osm_overpass_cache_features(bucket_key, feature_type)"
    ))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS osm_overpass_usage_log (
            id SERIAL PRIMARY KEY,
            plot_id INTEGER,
            bucket_key TEXT NOT NULL,
            country_hint TEXT,
            cache_hit BOOLEAN NOT NULL,
            feature_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_osm_overpass_usage_created ON osm_overpass_usage_log(created_at DESC)"
    ))
    db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_osm_overpass_usage_country ON osm_overpass_usage_log(country_hint)"
    ))
    db.commit()
    _SCHEMA_READY = True


def _bucket_key(lat: float, lon: float) -> str:
    bucket_lat = round(lat / REGION_BUCKET_DEG) * REGION_BUCKET_DEG
    bucket_lon = round(lon / REGION_BUCKET_DEG) * REGION_BUCKET_DEG
    return f"{bucket_lat:.4f}_{bucket_lon:.4f}"


def _bucket_bounds(bucket_key: str) -> tuple[float, float, float, float]:
    lat_str, lon_str = bucket_key.split("_")
    lat, lon = float(lat_str), float(lon_str)
    half = REGION_BUCKET_DEG / 2
    return (lon - half, lat - half, lon + half, lat + half)  # west, south, east, north


def _fetch_bucket_from_overpass_uncapped(bucket_key: str):
    """The actual osmnx/Overpass call, with no time limit of its own - always call this through
    _fetch_bucket_from_overpass, never directly, so the hard timeout below applies."""
    import osmnx as ox

    ox.settings.requests_timeout = OVERPASS_TIMEOUT_S
    west, south, east, north = _bucket_bounds(bucket_key)
    return ox.features.features_from_bbox(
        bbox=(west, south, east, north),
        tags={"building": True, "highway": True, "waterway": True},
    )


def _fetch_bucket_from_overpass(bucket_key: str) -> dict[str, list[tuple[BaseGeometry, str | None, str | None]]]:
    """Returns {"building"|"road"|"river": [(geom, name, subtype), ...]}. `name` is the OSM
    `name` tag (used for road labels on the general template, matching what Nigeria's `lines`
    table already provides); `subtype` is the matched tag's value (e.g. "primary", "residential",
    "river", "stream") - captured for completeness/future use, not currently read by any renderer."""
    result: dict[str, list[tuple[BaseGeometry, str | None, str | None]]] = {"building": [], "road": [], "river": []}
    try:
        import osmnx  # noqa: F401 - just checking it's installed before spinning up a thread
    except ImportError:
        logger.warning("osmnx is not installed - skipping Overpass fetch for bucket %s", bucket_key)
        return result

    # requests' own `timeout` only measures gaps between chunks, not total request duration, and
    # Overpass's embedded `[timeout:N]` is a soft server-side hint it can run past - so a slow-
    # but-still-streaming response can take far longer than OVERPASS_TIMEOUT_S even with both set
    # (observed: a single dense-city bucket took ~77s despite a 15s setting on both). This thread-
    # based wrapper is what actually enforces a ceiling. The thread is started as a daemon so an
    # abandoned (timed-out) fetch never keeps the process alive waiting for it - it just finishes
    # (or errors) on its own in the background and its result is discarded via `outcome`.
    outcome: dict = {}

    def _worker():
        try:
            outcome["gdf"] = _fetch_bucket_from_overpass_uncapped(bucket_key)
        except Exception as exc:  # noqa: BLE001 - captured for the main thread to log/handle
            outcome["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=OVERPASS_HARD_TIMEOUT_S)

    if thread.is_alive():
        logger.warning(
            "Overpass fetch for bucket %s exceeded the %ss hard timeout - treating as empty "
            "(the abandoned request may still complete server-side; a later plot in this area "
            "will retry the fetch since nothing gets cached on a timeout)",
            bucket_key, OVERPASS_HARD_TIMEOUT_S,
        )
        return result
    if "error" in outcome:
        # Covers osmnx's own "nothing found" exception as well as real network/5xx failures -
        # either way this bucket just gets treated as empty, never a hard error that could block
        # whatever triggered it.
        logger.warning("Overpass fetch failed for bucket %s: %r", bucket_key, outcome["error"])
        return result
    gdf = outcome.get("gdf")

    if gdf is None or gdf.empty:
        return result

    has_building = "building" in gdf.columns
    has_waterway = "waterway" in gdf.columns
    has_highway = "highway" in gdf.columns
    has_name = "name" in gdf.columns

    # osmnx GeoDataFrame columns are pandas object columns holding a mix of real values and
    # missing entries as float NaN (not Python None) wherever a row lacks that tag - naively
    # str()-ing a NaN produces the literal text "nan", which very nearly shipped as a building
    # name. pd.isna() is the only reliable way to catch both None and NaN in one check.
    def _tag_present(value) -> bool:
        # True/"yes" both mean "this boolean-style tag applies" (building=yes, waterway=yes);
        # None/NaN/False/"no" all mean it doesn't.
        if value is None or value is False:
            return False
        if not isinstance(value, bool) and pd.isna(value):
            return False
        return str(value).strip().lower() != "no"

    def _tag_text(value) -> str | None:
        # A real classification/name string if one exists, else None for a bare boolean tag
        # (building=yes carries presence but no extra info) or a missing value.
        if value is None or isinstance(value, bool):
            return None
        if pd.isna(value):
            return None
        text_value = str(value).strip()
        return text_value or None

    for _, row in gdf.iterrows():
        geom = row.get("geometry")
        if geom is None or geom.is_empty:
            continue
        name = _tag_text(row.get("name")) if has_name else None
        # A building whose true OSM way straddles this bucket's bbox edge gets clipped by
        # Overpass/osmnx at the boundary - if the clip cuts through a closed ring, what comes
        # back is an open LineString fragment (sometimes spanning most of the bucket) instead of
        # a Polygon. A LineString can't be meaningfully hatch-filled as a building, and rendering
        # it anyway is exactly what produced stray diagonal lines across an otherwise-clean plan -
        # so buildings are restricted to real area geometry, discarding the rest. Roads/rivers get
        # the opposite treatment: they're supposed to be lines, not areas.
        geom_type = geom.geom_type
        # A feature can only be one of the three here - checked in this order because a way
        # tagged both building=* and something else (rare, usually data-entry noise) is still
        # most usefully drawn as a building on the plan.
        if has_building and _tag_present(row.get("building")):
            if geom_type not in ("Polygon", "MultiPolygon"):
                continue
            result["building"].append((geom, name, _tag_text(row.get("building"))))
        elif has_waterway and _tag_present(row.get("waterway")):
            if geom_type not in ("LineString", "MultiLineString"):
                continue
            result["river"].append((geom, name, _tag_text(row.get("waterway"))))
        elif has_highway and _tag_present(row.get("highway")):
            if geom_type not in ("LineString", "MultiLineString"):
                continue
            result["road"].append((geom, name, _tag_text(row.get("highway"))))

    return result


def _bucket_is_fresh(db: Session, bucket_key: str) -> bool:
    row = db.execute(
        text("SELECT fetched_at FROM osm_overpass_cache_buckets WHERE bucket_key = :bk"),
        {"bk": bucket_key},
    ).mappings().first()
    if not row or row["fetched_at"] is None:
        return False
    fetched_at = row["fetched_at"]
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fetched_at) < timedelta(days=CACHE_MAX_AGE_DAYS)


def _store_bucket_cache(
    db: Session,
    bucket_key: str,
    features: dict[str, list[tuple[BaseGeometry, str | None, str | None]]],
) -> int:
    db.execute(
        text("""
            INSERT INTO osm_overpass_cache_buckets (bucket_key, fetched_at, feature_count, status)
            VALUES (:bucket_key, NOW(), 0, 'ok')
            ON CONFLICT (bucket_key) DO UPDATE SET fetched_at = NOW(), status = 'ok'
        """),
        {"bucket_key": bucket_key},
    )
    db.execute(text("DELETE FROM osm_overpass_cache_features WHERE bucket_key = :bk"), {"bk": bucket_key})

    total = 0
    for feature_type, entries in features.items():
        for geom, name, subtype in entries:
            try:
                db.execute(
                    text("""
                        INSERT INTO osm_overpass_cache_features (bucket_key, feature_type, name, subtype, geom)
                        VALUES (:bucket_key, :feature_type, :name, :subtype, ST_SetSRID(ST_GeomFromText(:wkt), 4326))
                    """),
                    {"bucket_key": bucket_key, "feature_type": feature_type, "name": name, "subtype": subtype, "wkt": geom.wkt},
                )
                total += 1
            except Exception:
                # A single malformed geometry (self-intersecting way, etc.) shouldn't drop the
                # whole bucket - skip it and keep the rest.
                continue

    db.execute(
        text("UPDATE osm_overpass_cache_buckets SET feature_count = :count WHERE bucket_key = :bk"),
        {"count": total, "bk": bucket_key},
    )
    db.commit()
    return total


def ensure_bucket_cached(db: Session, lat: float, lon: float) -> tuple[str, bool]:
    """Returns (bucket_key, was_cache_hit). Fetches from Overpass and populates the cache tables
    only on a miss/stale bucket."""
    ensure_osm_overpass_schema(db)
    bucket_key = _bucket_key(lat, lon)
    if _bucket_is_fresh(db, bucket_key):
        return bucket_key, True
    features = _fetch_bucket_from_overpass(bucket_key)
    _store_bucket_cache(db, bucket_key, features)
    return bucket_key, False


def run_overpass_feature_detection(db: Session, plot_id: int, centroid_lat: float, centroid_lon: float) -> dict:
    """Populates detected_features (plot_id, feature_type, location, geom) for a plot outside
    Nigeria, via the Overpass-backed regional cache. Deliberately matches the exact table shape
    _run_plot_feature_detection already writes for Nigeria (plots.py), so every downstream
    consumer - rendering, CSV export, hazard-analysis reuse, the admin panel - works unchanged
    regardless of which path populated a given plot's rows.

    Never raises - a fetch failure just means fewer/no detected features, not a broken plot
    creation. Callers should still wrap this in a try/except as defense in depth (schema-ensure
    calls above it, or the usage-log insert, could theoretically fail independently).
    """
    t0 = time.monotonic()
    try:
        bucket_key, cache_hit = ensure_bucket_cached(db, centroid_lat, centroid_lon)

        for feature_type in ("building", "road", "river"):
            db.execute(
                text("""
                    INSERT INTO detected_features (plot_id, feature_type, location, name, subtype, geom)
                    SELECT :plot_id, :feature_type, 'inside', c.name, c.subtype, c.geom
                    FROM osm_overpass_cache_features c
                    JOIN plots p ON p.id = :plot_id
                    WHERE c.bucket_key = :bucket_key AND c.feature_type = :feature_type
                      AND ST_Intersects(c.geom, p.geom)
                """),
                {"plot_id": plot_id, "feature_type": feature_type, "bucket_key": bucket_key},
            )
            db.execute(
                text("""
                    INSERT INTO detected_features (plot_id, feature_type, location, name, subtype, geom)
                    SELECT :plot_id, :feature_type, 'buffer', c.name, c.subtype, c.geom
                    FROM osm_overpass_cache_features c
                    JOIN plot_buffers b ON b.plot_id = :plot_id
                    JOIN plots p ON p.id = :plot_id
                    WHERE c.bucket_key = :bucket_key AND c.feature_type = :feature_type
                      AND ST_Intersects(c.geom, b.geom)
                      AND NOT ST_Intersects(c.geom, p.geom)
                """),
                {"plot_id": plot_id, "feature_type": feature_type, "bucket_key": bucket_key},
            )

        feature_count = int(
            db.execute(
                text("SELECT COUNT(*) FROM detected_features WHERE plot_id = :plot_id"),
                {"plot_id": plot_id},
            ).scalar()
            or 0
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        try:
            db.execute(
                text("""
                    INSERT INTO osm_overpass_usage_log
                        (plot_id, bucket_key, country_hint, cache_hit, feature_count, duration_ms)
                    VALUES (:plot_id, :bucket_key, :country_hint, :cache_hit, :feature_count, :duration_ms)
                """),
                {
                    "plot_id": plot_id,
                    "bucket_key": bucket_key,
                    "country_hint": _rough_country_hint(centroid_lat, centroid_lon),
                    "cache_hit": cache_hit,
                    "feature_count": feature_count,
                    "duration_ms": duration_ms,
                },
            )
        except Exception:
            pass

        db.commit()
        return {"bucket_key": bucket_key, "cache_hit": cache_hit, "feature_count": feature_count, "duration_ms": duration_ms}
    except Exception as exc:
        logger.warning("Overpass feature detection failed for plot %s: %r", plot_id, exc)
        db.rollback()
        return {"bucket_key": None, "cache_hit": False, "feature_count": 0, "duration_ms": int((time.monotonic() - t0) * 1000), "error": str(exc)}
