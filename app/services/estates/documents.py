from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from app.utils.r2_objects import build_r2_settings, create_r2_client

MAX_ESTATE_DOCUMENT_BYTES = max(int(os.getenv("ESTATE_DOCUMENT_MAX_BYTES", str(15 * 1024 * 1024))), 1)
ALLOWED_DOCUMENT_TYPES = {"application/pdf": ((".pdf",), b"%PDF-"), "image/jpeg": ((".jpg", ".jpeg"), b"\xff\xd8\xff"), "image/png": ((".png",), b"\x89PNG\r\n\x1a\n")}

@dataclass(frozen=True, slots=True)
class StoredPrivateObject:
    object_key: str
    mime_type: str
    size_bytes: int
    checksum: str
    filename: str

def store_private_estate_file(*, organization_uid: str, category: str, entity_uid: str, filename: str, content_type: str, data: bytes) -> StoredPrivateObject:
    mime = str(content_type or "").lower(); extension = os.path.splitext(filename)[1].lower()
    rule = ALLOWED_DOCUMENT_TYPES.get(mime)
    if not data: raise HTTPException(422, "File is empty")
    if len(data) > MAX_ESTATE_DOCUMENT_BYTES: raise HTTPException(413, "File exceeds the configured size limit")
    if not rule or extension not in rule[0] or not data.startswith(rule[1]): raise HTTPException(422, "Upload a valid PDF, JPG, JPEG, or PNG")
    settings = build_r2_settings(prefix="R2")
    if not settings: raise HTTPException(503, "Private document storage is not configured")
    clean_name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(filename)).strip(".-") or "document"
    key = f"estates/org_{organization_uid}/{category}/{entity_uid}/{uuid.uuid4().hex}_{clean_name}"
    create_r2_client(settings).put_object(Bucket=settings["bucket"], Key=key, Body=data, ContentType=mime, CacheControl="private, max-age=0, no-store")
    return StoredPrivateObject(key, mime, len(data), hashlib.sha256(data).hexdigest(), clean_name)

def read_private_estate_file(object_key: str) -> tuple[bytes, str]:
    settings = build_r2_settings(prefix="R2")
    if not settings: raise HTTPException(503, "Private document storage is not configured")
    response = create_r2_client(settings).get_object(Bucket=settings["bucket"], Key=object_key)
    return response["Body"].read(), str(response.get("ContentType") or "application/octet-stream")
