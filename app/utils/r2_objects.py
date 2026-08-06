import logging
import os
from urllib.parse import quote, urlparse

import boto3


logger = logging.getLogger("r2_objects")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def build_r2_settings(*, prefix: str = "R2") -> dict | None:
    if not _env_bool(f"{prefix}_ENABLED", True):
        return None

    endpoint_raw = str(os.getenv(f"{prefix}_ENDPOINT_URL") or "").strip()
    public_base = str(os.getenv(f"{prefix}_PUBLIC_BASE_URL") or "").strip()
    bucket = str(os.getenv(f"{prefix}_BUCKET") or "").strip()
    access_key = str(os.getenv(f"{prefix}_ACCESS_KEY_ID") or "").strip()
    secret_key = str(os.getenv(f"{prefix}_SECRET_ACCESS_KEY") or "").strip()
    region = str(os.getenv(f"{prefix}_REGION") or "auto").strip() or "auto"

    raw_for_parse = endpoint_raw or public_base
    if not raw_for_parse or not access_key or not secret_key:
        return None

    parsed = urlparse(raw_for_parse)
    if not parsed.scheme or not parsed.netloc:
        logger.warning("R2 object storage skipped: invalid endpoint/public base URL.")
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and not bucket:
        bucket = path_parts[0]
    if not bucket:
        logger.warning("R2 object storage skipped: missing bucket configuration.")
        return None

    if endpoint_raw:
        endpoint_parsed = urlparse(endpoint_raw)
        if not endpoint_parsed.scheme or not endpoint_parsed.netloc:
            logger.warning("R2 object storage skipped: invalid endpoint URL.")
            return None
        endpoint_url = f"{endpoint_parsed.scheme}://{endpoint_parsed.netloc}"
    else:
        endpoint_url = f"{parsed.scheme}://{parsed.netloc}"

    if not public_base:
        public_base = f"{endpoint_url.rstrip('/')}/{bucket}"

    return {
        "endpoint_url": endpoint_url.rstrip("/"),
        "public_base": public_base.rstrip("/"),
        "bucket": bucket,
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region,
        "prefix": prefix,
    }


def create_r2_client(settings: dict):
    return boto3.client(
        "s3",
        endpoint_url=settings["endpoint_url"],
        aws_access_key_id=settings["access_key"],
        aws_secret_access_key=settings["secret_key"],
        region_name=settings["region"],
    )


def build_public_url(settings: dict, object_key: str) -> str:
    return f"{settings['public_base']}/{quote(str(object_key).lstrip('/'), safe='/')}"


def normalize_object_key(raw_key: str, bucket: str) -> str:
    value = str(raw_key or "").strip()
    if not value:
        return ""

    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        value = parsed.path or ""

    value = value.lstrip("/")
    bucket_prefix = f"{bucket}/"
    if value.startswith(bucket_prefix):
        value = value[len(bucket_prefix):]
    return value


def upload_bytes(settings: dict, object_key: str, payload: bytes, *, content_type: str, cache_control: str = "private, max-age=0, no-store") -> dict:
    client = create_r2_client(settings)
    client.put_object(
        Bucket=settings["bucket"],
        Key=object_key,
        Body=payload,
        ContentType=content_type,
        CacheControl=cache_control,
    )
    return {
        "bucket": settings["bucket"],
        "object_key": object_key,
        "public_url": build_public_url(settings, object_key),
    }


def delete_object_best_effort(settings: dict, object_key: str) -> bool:
    clean_key = normalize_object_key(object_key, str(settings.get("bucket") or ""))
    if not clean_key:
        return False
    try:
        client = create_r2_client(settings)
        client.delete_object(Bucket=settings["bucket"], Key=clean_key)
        return True
    except Exception as exc:
        logger.warning("R2 delete failed for '%s': %s", clean_key, exc)
        return False
