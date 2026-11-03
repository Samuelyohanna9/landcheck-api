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
# Native CAD files architects actually work in (DWG/DXF). DWG's own magic bytes vary by AutoCAD
# version (AC1032, AC1027, AC1021...) with no single reliable signature, and browsers report
# inconsistent or absent MIME types for both - so these are accepted by extension only, not
# content-sniffed like the types above. They're stored and versioned like any other document but
# aren't renderable for the live measurement tool (that needs a raster image or PDF page).
CAD_EXTENSIONS = (".dwg", ".dxf")

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
    if rule and extension in rule[0] and data.startswith(rule[1]):
        resolved_mime = mime
    elif extension in CAD_EXTENSIONS:
        resolved_mime = "application/octet-stream"
    else:
        raise HTTPException(422, "Upload a valid PDF, JPG, JPEG, PNG, DWG, or DXF")
    settings = build_r2_settings(prefix="R2")
    if not settings: raise HTTPException(503, "Private document storage is not configured")
    clean_name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(filename)).strip(".-") or "document"
    key = f"estates/org_{organization_uid}/{category}/{entity_uid}/{uuid.uuid4().hex}_{clean_name}"
    create_r2_client(settings).put_object(Bucket=settings["bucket"], Key=key, Body=data, ContentType=resolved_mime, CacheControl="private, max-age=0, no-store")
    return StoredPrivateObject(key, resolved_mime, len(data), hashlib.sha256(data).hexdigest(), clean_name)

def read_private_estate_file(object_key: str) -> tuple[bytes, str]:
    settings = build_r2_settings(prefix="R2")
    if not settings: raise HTTPException(503, "Private document storage is not configured")
    response = create_r2_client(settings).get_object(Bucket=settings["bucket"], Key=object_key)
    return response["Body"].read(), str(response.get("ContentType") or "application/octet-stream")
