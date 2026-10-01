from __future__ import annotations

import os
from pathlib import PurePath

from fastapi import HTTPException, UploadFile


MAX_IMPORT_UPLOAD_BYTES = max(
    1024 * 1024,
    int(os.getenv("ESTATE_IMPORT_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))),
)


def upload_extension(file: UploadFile) -> str:
    name = os.path.basename(str(file.filename or "").strip())
    if not name or name in {".", ".."} or name != PurePath(name).name:
        raise HTTPException(status_code=422, detail="Invalid upload filename")
    return os.path.splitext(name)[1].lower()


async def read_limited_upload(file: UploadFile, *, max_bytes: int = MAX_IMPORT_UPLOAD_BYTES) -> bytes:
    payload = await file.read(max(1, int(max_bytes)) + 1)
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail="Uploaded file exceeds the configured size limit")
    if not payload:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    return payload
