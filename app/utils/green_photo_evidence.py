from __future__ import annotations

import io
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import imagehash
from PIL import ExifTags, Image
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Two independent, deliberately cheap (no AI call, no external API) fraud/mistake signals for
# field-evidence photos, run on every upload linked to a tree or task:
#   1. Perceptual-hash duplicate detection - is this the same photo (or a lightly re-compressed/
#      re-cropped copy of it) as one already submitted for this project?
#   2. EXIF GPS + capture-time sanity check - does the photo's own embedded location/timestamp
#      (when the phone/camera recorded one) match where and roughly when it's being claimed for?
# Neither ever blocks the upload - both are surfaced as flags for the agent and/or a reviewer to
# see, same "flag for review, don't silently certify or silently reject" discipline used
# throughout this codebase (see hazard_pluvial.py's Phase 4 note, plan_reader.py's checker).

_DUPLICATE_HAMMING_THRESHOLD = 6  # out of 64 bits - phash is robust to recompression/resizing, so
                                  # a genuinely different photo of a different tree is almost always
                                  # >20 bits apart; this catches "same photo, re-saved/cropped/lightly
                                  # filtered" without false-flagging two honestly different photos of
                                  # similar-looking trees.
_MAX_CANDIDATES_PER_PROJECT = 500  # bounds the comparison cost regardless of project photo volume
_LOCATION_MISMATCH_METERS = 150.0  # generous margin over normal phone-GPS error + a tree not being
                                    # planted exactly on its recorded point
_PHOTO_AGE_WARNING_DAYS = 3  # a "fresh evidence" photo older than this is worth a reviewer's glance,
                             # not proof of anything on its own (phones/apps do legitimately preserve
                             # original EXIF for re-uploaded/re-shared photos)


def ensure_photo_evidence_schema(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_photo_evidence (
            id SERIAL PRIMARY KEY,
            tree_id INTEGER,
            task_id INTEGER,
            project_id INTEGER,
            object_key TEXT NOT NULL,
            phash TEXT NOT NULL,
            exif_lat DOUBLE PRECISION,
            exif_lng DOUBLE PRECISION,
            exif_captured_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))
    # Derived flags (computed once at upload time by check_photo_location_and_time /
    # find_similar_photo) persisted alongside the raw hash/EXIF - without this, the flags only ever
    # existed in that one upload request's HTTP response, invisible to a supervisor reviewing the
    # evidence later. Nullable/booleans default to unknown-false so old rows (written before these
    # columns existed) read as "nothing flagged" rather than erroring.
    for column_sql in (
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS duplicate_distance INTEGER",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS matched_photo_id INTEGER",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS matched_tree_id INTEGER",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS matched_task_id INTEGER",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS gps_available BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS timestamp_available BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS location_mismatch BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS distance_from_tree_m DOUBLE PRECISION",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS photo_not_recent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE green_photo_evidence ADD COLUMN IF NOT EXISTS photo_age_days INTEGER",
    ):
        db.execute(text(column_sql))
    db.execute(text("CREATE INDEX IF NOT EXISTS ix_green_photo_evidence_project ON green_photo_evidence (project_id, created_at DESC)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS ix_green_photo_evidence_task ON green_photo_evidence (task_id)"))
    db.commit()


def compute_perceptual_hash(image_bytes: bytes) -> Optional[str]:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            return str(imagehash.phash(image))
    except Exception:
        logger.warning("Could not compute perceptual hash for uploaded photo", exc_info=True)
        return None


def find_similar_photo(
    db: Session, project_id: Optional[int], phash_hex: str, exclude_tree_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Scans this project's recent photo hashes for a near-duplicate. Scoped to project_id (not
    global) - keeps the comparison set relevant and bounded, and avoids flagging two unrelated
    projects' genuinely different trees that happen to photograph similarly. exclude_tree_id skips
    a tree's own prior photos (a surveyor legitimately re-photographing the same tree for a later
    maintenance task shouldn't itself be flagged as "duplicate").
    """
    if project_id is None:
        return None
    try:
        new_hash = imagehash.hex_to_hash(phash_hex)
    except Exception:
        return None

    rows = db.execute(
        text("""
            SELECT id, tree_id, task_id, object_key, phash, created_at
            FROM green_photo_evidence
            WHERE project_id = :project_id
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"project_id": project_id, "limit": _MAX_CANDIDATES_PER_PROJECT},
    ).mappings().all()

    best_match: Optional[Dict[str, Any]] = None
    best_distance: Optional[int] = None
    for row in rows:
        if exclude_tree_id is not None and row.get("tree_id") == exclude_tree_id:
            continue
        try:
            candidate_hash = imagehash.hex_to_hash(str(row["phash"]))
        except Exception:
            continue
        distance = new_hash - candidate_hash
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_match = dict(row)

    if best_match is not None and best_distance is not None and best_distance <= _DUPLICATE_HAMMING_THRESHOLD:
        return {
            "distance": int(best_distance),
            "matched_photo_id": best_match["id"],
            "matched_tree_id": best_match["tree_id"],
            "matched_task_id": best_match["task_id"],
            "matched_created_at": best_match["created_at"].isoformat() if best_match["created_at"] else None,
        }
    return None


def record_photo_evidence(
    db: Session,
    *,
    tree_id: Optional[int],
    task_id: Optional[int],
    project_id: Optional[int],
    object_key: str,
    phash_hex: Optional[str],
    exif_lat: Optional[float],
    exif_lng: Optional[float],
    exif_captured_at: Optional[datetime],
    checks: Optional[Dict[str, Any]] = None,
) -> None:
    if not phash_hex:
        return
    checks = checks or {}
    duplicate = checks.get("duplicate") or {}
    db.execute(
        text("""
            INSERT INTO green_photo_evidence
                (tree_id, task_id, project_id, object_key, phash, exif_lat, exif_lng, exif_captured_at,
                 is_duplicate, duplicate_distance, matched_photo_id, matched_tree_id, matched_task_id,
                 gps_available, timestamp_available, location_mismatch, distance_from_tree_m,
                 photo_not_recent, photo_age_days)
            VALUES (:tree_id, :task_id, :project_id, :object_key, :phash, :exif_lat, :exif_lng, :exif_captured_at,
                    :is_duplicate, :duplicate_distance, :matched_photo_id, :matched_tree_id, :matched_task_id,
                    :gps_available, :timestamp_available, :location_mismatch, :distance_from_tree_m,
                    :photo_not_recent, :photo_age_days)
        """),
        {
            "tree_id": tree_id, "task_id": task_id, "project_id": project_id, "object_key": object_key,
            "phash": phash_hex, "exif_lat": exif_lat, "exif_lng": exif_lng, "exif_captured_at": exif_captured_at,
            "is_duplicate": bool(duplicate),
            "duplicate_distance": duplicate.get("distance"),
            "matched_photo_id": duplicate.get("matched_photo_id"),
            "matched_tree_id": duplicate.get("matched_tree_id"),
            "matched_task_id": duplicate.get("matched_task_id"),
            "gps_available": bool(checks.get("gps_available")),
            "timestamp_available": bool(checks.get("timestamp_available")),
            "location_mismatch": bool(checks.get("location_mismatch")),
            "distance_from_tree_m": checks.get("distance_from_tree_m"),
            "photo_not_recent": bool(checks.get("photo_not_recent")),
            "photo_age_days": checks.get("photo_age_days"),
        },
    )
    db.commit()


def get_task_photo_flags(db: Session, task_ids: list[int]) -> Dict[int, Dict[str, Any]]:
    """Bulk lookup for a supervisor review queue - one row per task_id with the flags summarized
    across every photo submitted for that task (a duplicate/mismatch on ANY of a task's photos is
    worth a reviewer's attention, so these are OR'd/MAX'd rather than only looking at the latest).
    """
    if not task_ids:
        return {}
    rows = db.execute(
        text("""
            SELECT task_id,
                   bool_or(is_duplicate) AS any_duplicate,
                   min(duplicate_distance) FILTER (WHERE is_duplicate) AS best_duplicate_distance,
                   bool_or(location_mismatch) AS any_location_mismatch,
                   max(distance_from_tree_m) FILTER (WHERE location_mismatch) AS max_mismatch_distance_m,
                   bool_or(photo_not_recent) AS any_not_recent,
                   max(photo_age_days) FILTER (WHERE photo_not_recent) AS max_photo_age_days
            FROM green_photo_evidence
            WHERE task_id = ANY(:task_ids)
            GROUP BY task_id
        """),
        {"task_ids": list(task_ids)},
    ).mappings().all()
    return {
        int(row["task_id"]): {
            "any_duplicate": bool(row["any_duplicate"]),
            "best_duplicate_distance": row["best_duplicate_distance"],
            "any_location_mismatch": bool(row["any_location_mismatch"]),
            "max_mismatch_distance_m": row["max_mismatch_distance_m"],
            "any_not_recent": bool(row["any_not_recent"]),
            "max_photo_age_days": row["max_photo_age_days"],
        }
        for row in rows
    }


def _dms_to_decimal(dms, ref: str) -> Optional[float]:
    try:
        degrees, minutes, seconds = (float(part) for part in dms)
    except Exception:
        return None
    value = degrees + minutes / 60.0 + seconds / 3600.0
    if str(ref).upper() in ("S", "W"):
        value = -value
    return value


def extract_exif_gps_and_time(image_bytes: bytes) -> Dict[str, Any]:
    """Reads whatever GPS coordinates and capture timestamp the camera/phone itself embedded -
    None for anything not present (many screenshots, downloads, and some messaging-app-compressed
    photos strip EXIF entirely; that absence is reported to the caller, not treated as suspicious
    on its own, since it's extremely common for innocent reasons).
    """
    result: Dict[str, Any] = {"lat": None, "lng": None, "captured_at": None}
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            exif = image.getexif()
            if not exif:
                return result

            # DateTimeOriginal (36867) lives in the Exif SUB-IFD, not the main/0th IFD that a
            # plain exif.get() reads - confirmed live, this was silently returning None for every
            # real photo before adding the get_ifd(ExifTags.IFD.Exif) lookup. Plain DateTime (306)
            # IS in the main IFD directly, and is checked second - it can reflect a file-modified
            # time rather than true capture time, so DateTimeOriginal wins when both exist.
            exif_sub_ifd: Dict[int, Any] = {}
            try:
                exif_sub_ifd = dict(exif.get_ifd(ExifTags.IFD.Exif) or {})
            except Exception:
                exif_sub_ifd = {}
            raw_candidates = [exif_sub_ifd.get(36867), exif.get(306)]
            for raw in raw_candidates:
                if not raw:
                    continue
                try:
                    result["captured_at"] = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                    break
                except ValueError:
                    continue

            gps_ifd = None
            try:
                gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
            except Exception:
                gps_ifd = exif.get(34853)
            if gps_ifd:
                lat = _dms_to_decimal(gps_ifd.get(2), gps_ifd.get(1) or "N") if gps_ifd.get(2) else None
                lng = _dms_to_decimal(gps_ifd.get(4), gps_ifd.get(3) or "E") if gps_ifd.get(4) else None
                if lat is not None and lng is not None:
                    result["lat"] = lat
                    result["lng"] = lng
    except Exception:
        logger.warning("Could not read EXIF data from uploaded photo", exc_info=True)
    return result


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def check_photo_location_and_time(
    exif_data: Dict[str, Any], tree_lat: Optional[float], tree_lng: Optional[float],
) -> Dict[str, Any]:
    """Turns raw EXIF readings into the flags a caller actually cares about - a location mismatch
    against the tree's own recorded planting point, and a "this doesn't look freshly taken" age
    check. Both are advisory (see module docstring) - a mismatch might mean the tree's own recorded
    point was itself imprecise, not that the photo is fraudulent.
    """
    flags: Dict[str, Any] = {
        "gps_available": exif_data.get("lat") is not None and exif_data.get("lng") is not None,
        "timestamp_available": exif_data.get("captured_at") is not None,
        "location_mismatch": False,
        "distance_from_tree_m": None,
        "photo_not_recent": False,
        "photo_age_days": None,
    }

    if flags["gps_available"] and tree_lat is not None and tree_lng is not None:
        distance = _haversine_meters(tree_lat, tree_lng, exif_data["lat"], exif_data["lng"])
        flags["distance_from_tree_m"] = round(distance, 1)
        flags["location_mismatch"] = distance > _LOCATION_MISMATCH_METERS

    if flags["timestamp_available"]:
        captured_at = exif_data["captured_at"]
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - captured_at).days
        flags["photo_age_days"] = age_days
        flags["photo_not_recent"] = age_days > _PHOTO_AGE_WARNING_DAYS

    return flags
