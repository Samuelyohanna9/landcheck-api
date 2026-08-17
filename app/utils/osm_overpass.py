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
CACHE_MAX_AGE_DAYS = 90  # how long a *successful* fetch (even a genuinely empty one) is trusted.
FAILED_FETCH_RETRY_MINUTES = 15  # how long a *failed* fetch (timeout/network/5xx) blocks retries -
                                 # short on purpose. A failure used to get cached with the same
                                 # 90-day lifetime as a real result, which meant one transient
                                 # Overpass hiccup could permanently starve an entire ~2.2km area
                                 # of features for months - every plot created there afterwards
                                 # silently detected nothing, with no way to tell a "confirmed
                                 # empty" bucket from a "we never actually managed to check" one.
                                 # This is what "sometimes detects, sometimes doesn't" was.
OVERPASS_TIMEOUT_S = 8  # passed to osmnx as both the per-ENDPOINT HTTP client timeout AND the
                        # embedded Overpass QL `[timeout:N]` server-side hint - neither is a hard
                        # wall-clock deadline on its own (requests' timeout is inter-chunk, not
                        # total; Overpass's is a soft "try to abort around here"). Kept fairly
                        # tight since _fetch_bucket_from_overpass_uncapped now tries up to 3
                        # mirror endpoints in sequence on failure - a slow/dead one should get cut
                        # loose quickly so there's still time left to try the next.
OVERPASS_HARD_TIMEOUT_S = 45  # absolute ceiling regardless of what the HTTP/Overpass layers do -
                              # see _fetch_bucket_from_overpass. Only matters when this ends up
                              # running inline (no BackgroundTasks available) - see
                              # _run_plot_feature_detection in plots.py, which is the normal path.
                              # Sized for up to 3 sequential endpoint attempts (OVERPASS_TIMEOUT_S
                              # each) plus response-parsing overhead, not just one - a timeout here
                              # retries itself shortly after anyway (FAILED_FETCH_RETRY_MINUTES)
                              # instead of being cached for 90 days, so there's little downside to
                              # giving the full mirror list a real chance to answer first.
                              # on the first try rather than needing a second attempt at all.

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


# Base URLs as osmnx expects them (it appends "/interpreter" itself - see osmnx._overpass, `url =
# settings.overpass_url.rstrip("/") + "/interpreter"`). overpass-api.de first since it's the
# best-known/most complete instance; the OSM wiki itself documents it as slow/overloaded under
# load, so the other two are real fallbacks, not decorative - all three mirror the same underlying
# OSM database (global coverage, not limited to their host country despite the domains), so
# failing over between them changes nothing about data freshness/completeness, only which server
# actually answers. Each was verified live (not just taken from documentation) immediately before
# being listed here: overpass.kumi.systems and overpass.private.coffee - both commonly recommended
# historically - didn't respond at all when tested; osm.ch and maps.mail.ru both did.
_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api",
    "https://overpass.osm.ch/api",
    "https://maps.mail.ru/osm/tools/overpass/api",
]


def _fetch_bucket_from_overpass_uncapped(bucket_key: str):
    """The actual osmnx/Overpass call, with no time limit of its own - always call this through
    _fetch_bucket_from_overpass, never directly, so the hard timeout below applies.

    Tries each endpoint in _OVERPASS_ENDPOINTS in turn, since the default public instance is
    documented (OSM wiki, and this module's own observed behavior) as frequently slow/overloaded.
    """
    import osmnx as ox
    from osmnx._errors import InsufficientResponseError

    # osmnx's built-in rate limiter queries the server's `/status` endpoint before every request
    # to decide how long to pause - a documented, occasionally-broken mechanism upstream
    # (gboeing/osmnx#832, #697: a status-endpoint format change or load-balanced inconsistency can
    # make this hang or misreport, stalling even a trivial small-bbox query for over a minute,
    # independent of whether Overpass itself could have answered quickly). This module's own
    # request volume to Overpass is already naturally throttled by the bucket cache (one real
    # fetch per ~2.2km area per 90 days, see ensure_bucket_cached), so osmnx's extra layer buys
    # nothing here and is a documented source of exactly the failures this module exists to route
    # around.
    ox.settings.overpass_rate_limit = False
    ox.settings.requests_timeout = OVERPASS_TIMEOUT_S

    west, south, east, north = _bucket_bounds(bucket_key)
    last_error: Exception | None = None
    for endpoint in _OVERPASS_ENDPOINTS:
        ox.settings.overpass_url = endpoint
        try:
            return ox.features.features_from_bbox(
                bbox=(west, south, east, north),
                tags={"building": True, "highway": True, "waterway": True},
            )
        except InsufficientResponseError:
            # A real, authoritative "nothing here" answer - every mirror draws from the same OSM
            # database, so there's no point asking a different one the same question.
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Overpass endpoint %s failed for bucket %s: %r - trying next endpoint",
                endpoint, bucket_key, exc,
            )
            continue
    raise last_error or RuntimeError("All Overpass endpoints failed")


def _fetch_bucket_from_overpass(
    bucket_key: str,
) -> tuple[dict[str, list[tuple[BaseGeometry, str | None, str | None]]], bool]:
    """Returns ({"building"|"road"|"river": [(geom, name, subtype), ...]}, success).

    `success` distinguishes "we asked Overpass and it genuinely has nothing here" (a real result,
    safe to cache for CACHE_MAX_AGE_DAYS) from "we don't actually know" - a timeout, network error,
    or 5xx (must NOT be cached the same way, or one transient hiccup silently starves an entire
    ~2.2km area of features for months; see FAILED_FETCH_RETRY_MINUTES). Callers must check this
    before treating an empty result dict as a confirmed-empty area.

    `name` is the OSM `name` tag (used for road labels on the general template, matching what
    Nigeria's `lines` table already provides); `subtype` is the matched tag's value (e.g.
    "primary", "residential", "river", "stream") - captured for completeness/future use, not
    currently read by any renderer.
    """
    result: dict[str, list[tuple[BaseGeometry, str | None, str | None]]] = {"building": [], "road": [], "river": []}
    try:
        import osmnx  # noqa: F401 - just checking it's installed before spinning up a thread
        from osmnx._errors import InsufficientResponseError
    except ImportError:
        logger.warning("osmnx is not installed - skipping Overpass fetch for bucket %s", bucket_key)
        return result, False

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
            "Overpass fetch for bucket %s exceeded the %ss hard timeout - will retry in %s "
            "minutes (the abandoned request may still complete server-side, but its result is "
            "discarded either way)",
            bucket_key, OVERPASS_HARD_TIMEOUT_S, FAILED_FETCH_RETRY_MINUTES,
        )
        return result, False
    if "error" in outcome:
        exc = outcome["error"]
        if isinstance(exc, InsufficientResponseError):
            # A real answer: Overpass was reachable and this bbox genuinely has none of the
            # tags we asked for. Safe to cache as a confirmed-empty bucket.
            return result, True
        # Network error, timeout inside requests itself, 5xx, etc. - we don't know what's really
        # out there, so this must NOT be cached as if it were a confirmed-empty result.
        logger.warning("Overpass fetch failed for bucket %s: %r", bucket_key, exc)
        return result, False
    gdf = outcome.get("gdf")

    if gdf is None or gdf.empty:
        return result, True

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

    return result, True


def _bucket_is_fresh(db: Session, bucket_key: str) -> bool:
    row = db.execute(
        text("SELECT fetched_at, status FROM osm_overpass_cache_buckets WHERE bucket_key = :bk"),
        {"bk": bucket_key},
    ).mappings().first()
    if not row or row["fetched_at"] is None:
        return False
    fetched_at = row["fetched_at"]
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - fetched_at
    # A 'failed' bucket (timeout/network error - see _fetch_bucket_from_overpass) only blocks
    # retries briefly, not for the full 90 days a genuine result (found-something or confirmed-
    # empty, both 'ok') is trusted for.
    max_age = timedelta(minutes=FAILED_FETCH_RETRY_MINUTES) if row["status"] == "failed" else timedelta(days=CACHE_MAX_AGE_DAYS)
    return age < max_age


def _store_bucket_cache(
    db: Session,
    bucket_key: str,
    features: dict[str, list[tuple[BaseGeometry, str | None, str | None]]],
    success: bool,
) -> int:
    if not success:
        # Record the attempt (so _bucket_is_fresh's short failure-cooldown applies) without
        # touching osm_overpass_cache_features at all - if an earlier successful fetch already
        # cached real features here, a later failed retry must never wipe them out.
        db.execute(
            text("""
                INSERT INTO osm_overpass_cache_buckets (bucket_key, fetched_at, feature_count, status)
                VALUES (:bucket_key, NOW(), 0, 'failed')
                ON CONFLICT (bucket_key) DO UPDATE SET fetched_at = NOW(), status = 'failed'
            """),
            {"bucket_key": bucket_key},
        )
        db.commit()
        return 0

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
    only on a miss/stale bucket (see _bucket_is_fresh for what "stale" means for a failed vs. a
    successful previous attempt)."""
    ensure_osm_overpass_schema(db)
    bucket_key = _bucket_key(lat, lon)
    if _bucket_is_fresh(db, bucket_key):
        return bucket_key, True
    features, success = _fetch_bucket_from_overpass(bucket_key)
    _store_bucket_cache(db, bucket_key, features, success)
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
