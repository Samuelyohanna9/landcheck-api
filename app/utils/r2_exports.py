import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import boto3


logger = logging.getLogger("r2_exports")


def _is_enabled(value: str | None) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _safe_file_name(filename: str | None, default_name: str = "export.pdf") -> str:
    base = Path(str(filename or default_name)).name
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", base).strip("-.")
    return cleaned or default_name


def _safe_segment(value: str | None, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-.")
    return cleaned or fallback


def _build_export_r2_settings() -> dict | None:
    if not _is_enabled(os.getenv("R2_EXPORTS_ENABLED", "true")):
        return None

    endpoint_raw = (os.getenv("R2_EXPORTS_ENDPOINT_URL") or os.getenv("R2_ENDPOINT_URL") or "").strip()
    public_base = (os.getenv("R2_EXPORTS_PUBLIC_BASE_URL") or os.getenv("R2_PUBLIC_BASE_URL") or "").strip()
    bucket = (os.getenv("R2_EXPORTS_BUCKET") or os.getenv("R2_BUCKET") or "").strip()
    access_key = (os.getenv("R2_EXPORTS_ACCESS_KEY_ID") or os.getenv("R2_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.getenv("R2_EXPORTS_SECRET_ACCESS_KEY") or os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
    region = (os.getenv("R2_EXPORTS_REGION") or os.getenv("R2_REGION") or "auto").strip()

    raw_for_parse = endpoint_raw or public_base
    if not raw_for_parse or not access_key or not secret_key:
        return None

    parsed = urlparse(raw_for_parse)
    if not parsed.scheme or not parsed.netloc:
        logger.warning("R2 export skipped: invalid endpoint/public base URL.")
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and not bucket:
        bucket = path_parts[0]
    if not bucket:
        logger.warning("R2 export skipped: missing bucket configuration.")
        return None

    if endpoint_raw:
        endpoint_parsed = urlparse(endpoint_raw)
        if not endpoint_parsed.scheme or not endpoint_parsed.netloc:
            logger.warning("R2 export skipped: invalid endpoint URL.")
            return None
        endpoint_url = f"{endpoint_parsed.scheme}://{endpoint_parsed.netloc}"
    else:
        endpoint_url = f"{parsed.scheme}://{parsed.netloc}"

    if not public_base:
        public_base = f"{endpoint_url.rstrip('/')}/{bucket}"

    return {
        "endpoint_url": endpoint_url,
        "public_base": public_base.rstrip("/"),
        "bucket": bucket,
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region or "auto",
    }


def upload_export_file_best_effort(
    local_path: str,
    filename: str,
    *,
    category: str = "general",
    project_id: int | None = None,
    organization_id: int | None = None,
    content_type: str = "application/pdf",
) -> dict | None:
    settings = _build_export_r2_settings()
    if not settings:
        return None

    try:
        payload = Path(local_path).read_bytes()
    except Exception:
        logger.warning("R2 export skipped: unable to read local file '%s'.", local_path)
        return None

    if not payload:
        logger.warning("R2 export skipped: file '%s' is empty.", local_path)
        return None

    prefix_raw = (os.getenv("R2_EXPORTS_PREFIX") or "exports/pdf").strip().strip("/")
    prefix_parts = [p for p in prefix_raw.split("/") if p]
    category_part = _safe_segment(category, "general")
    safe_name = _safe_file_name(filename, "export.pdf")
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    key_parts = [
        *prefix_parts,
        category_part,
        *( [f"org_{int(organization_id)}"] if organization_id is not None else [] ),
        *( [f"project_{int(project_id)}"] if project_id is not None else [] ),
        now.strftime("%Y"),
        now.strftime("%m"),
        f"{stamp}_{uuid.uuid4().hex[:12]}_{safe_name}",
    ]
    object_key = "/".join(key_parts)

    try:
        client = boto3.client(
            "s3",
            endpoint_url=settings["endpoint_url"],
            aws_access_key_id=settings["access_key"],
            aws_secret_access_key=settings["secret_key"],
            region_name=settings["region"],
        )
        client.put_object(
            Bucket=settings["bucket"],
            Key=object_key,
            Body=payload,
            ContentType=content_type,
            CacheControl="private, max-age=0, no-store",
        )
    except Exception as exc:
        logger.warning("R2 export upload failed for '%s': %s", object_key, exc)
        return None

    public_url = f"{settings['public_base']}/{quote(object_key, safe='/')}"
    return {
        "bucket": settings["bucket"],
        "object_key": object_key,
        "public_url": public_url,
    }
