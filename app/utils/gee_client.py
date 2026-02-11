import json
import os
import threading
from typing import Optional

import ee

_init_lock = threading.Lock()
_initialized = False


def init_gee(project_id: Optional[str] = None) -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        key_json = os.environ.get("GEE_SERVICE_ACCOUNT_JSON")
        key_path = os.environ.get("GEE_SERVICE_ACCOUNT_JSON_PATH")
        if not key_json and key_path:
            try:
                with open(key_path, "r", encoding="utf-8") as handle:
                    key_json = handle.read()
            except OSError as exc:
                raise RuntimeError(f"Failed to read GEE_SERVICE_ACCOUNT_JSON_PATH: {exc}") from exc
        if not key_json:
            raise RuntimeError("GEE_SERVICE_ACCOUNT_JSON not set (or GEE_SERVICE_ACCOUNT_JSON_PATH missing)")
        try:
            key_data = json.loads(key_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GEE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc

        client_email = key_data.get("client_email")
        if not client_email:
            raise RuntimeError("GEE service account JSON missing client_email")

        proj = project_id or os.environ.get("GEE_PROJECT_ID") or key_data.get("project_id")
        if not proj:
            raise RuntimeError("GEE project ID not set (GEE_PROJECT_ID)")

        credentials = ee.ServiceAccountCredentials(client_email, key_data=key_json)
        ee.Initialize(credentials, project=proj)
        _initialized = True
