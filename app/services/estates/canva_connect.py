from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlencode

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.models.estate_canva import EstateCanvaConnection

logger = logging.getLogger(__name__)

CANVA_API = "https://api.canva.com/rest/v1"
CANVA_AUTHORIZE = "https://www.canva.com/api/oauth/authorize"
CANVA_KEYS = f"{CANVA_API}/connect/keys"
DEFAULT_SCOPES = "asset:read asset:write design:content:read design:content:write design:meta:read"


class CanvaNotConfigured(RuntimeError):
    pass


class CanvaApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else str(raw).strip().lower() in {"1", "true", "yes", "on"}


def configured() -> bool:
    return _env_bool("CANVA_ENABLED") and all(str(os.getenv(name) or "").strip() for name in ("CANVA_CLIENT_ID", "CANVA_CLIENT_SECRET", "CANVA_SECRET_KEY"))


def require_configured() -> None:
    if not configured():
        raise CanvaNotConfigured("Canva editing is not configured on this server yet.")


def client_id() -> str:
    require_configured()
    return str(os.getenv("CANVA_CLIENT_ID") or "").strip()


def redirect_uri() -> str:
    value = str(os.getenv("CANVA_REDIRECT_URI") or "").strip()
    return value or f"{api_public_url()}/estates/marketing/canva/oauth/callback"


def return_url() -> str:
    value = str(os.getenv("CANVA_RETURN_URL") or "").strip()
    return value or f"{api_public_url()}/estates/marketing/canva/return"


def return_navigation_enabled() -> bool:
    """Use Canva correlation state only after Return navigation is enabled in Canva."""
    return _env_bool("CANVA_RETURN_NAVIGATION_ENABLED")


def prepare_edit_url(edit_url: str, correlation_state: str) -> str:
    """Add Canva return state only when the app is configured for Return navigation."""
    if not return_navigation_enabled():
        return edit_url
    separator = "&" if "?" in edit_url else "?"
    return f"{edit_url}{separator}{urlencode({'correlation_state': correlation_state})}"


def web_url() -> str:
    return str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").strip().rstrip("/")


def api_public_url() -> str:
    return str(os.getenv("LANDCHECK_API_PUBLIC_URL") or "https://api.landcheck.online").strip().rstrip("/")


def scopes() -> str:
    return " ".join((str(os.getenv("CANVA_SCOPES") or DEFAULT_SCOPES).split()))


def _fernet() -> Fernet:
    raw = str(os.getenv("CANVA_SECRET_KEY") or "").strip()
    if len(raw) < 16:
        raise CanvaNotConfigured("CANVA_SECRET_KEY must be set to a random value of at least 16 characters.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"landcheck-canva-token|" + raw.encode()).digest()))


def encrypt_token(value: str) -> str:
    return _fernet().encrypt(str(value).encode()).decode("ascii")


def decrypt_token(value: str) -> str:
    try:
        return _fernet().decrypt(str(value).encode()).decode()
    except InvalidToken as exc:
        raise CanvaNotConfigured("Stored Canva token could not be decrypted.") from exc


def hash_value(value: str) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(*, state: str, challenge: str) -> str:
    params = {
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": scopes(),
        "response_type": "code",
        "client_id": client_id(),
        "state": state,
        "redirect_uri": redirect_uri(),
    }
    return f"{CANVA_AUTHORIZE}?{urlencode(params)}"


def _json_response(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        raise CanvaApiError(str(data.get("message") or "Canva request failed."), response.status_code, data.get("code"))
    return data if isinstance(data, dict) else {}


def exchange_code(code: str, verifier: str) -> dict:
    require_configured()
    response = requests.post(
        f"{CANVA_API}/oauth/token",
        auth=(str(os.getenv("CANVA_CLIENT_ID")), str(os.getenv("CANVA_CLIENT_SECRET"))),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "code_verifier": verifier},
        timeout=15,
    )
    return _json_response(response)


def revoke_token(token: str) -> None:
    """Revoke a Canva refresh/access token using the backend-only client credentials flow."""
    require_configured()
    response = requests.post(
        f"{CANVA_API}/oauth/revoke",
        auth=(str(os.getenv("CANVA_CLIENT_ID")), str(os.getenv("CANVA_CLIENT_SECRET"))),
        data={"token": str(token)},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    _json_response(response)


def _refresh(db: Session, connection: EstateCanvaConnection) -> str:
    refresh = decrypt_token(connection.refresh_token_enc)
    response = requests.post(
        f"{CANVA_API}/oauth/token",
        auth=(str(os.getenv("CANVA_CLIENT_ID")), str(os.getenv("CANVA_CLIENT_SECRET"))),
        data={"grant_type": "refresh_token", "refresh_token": refresh},
        timeout=15,
    )
    data = _json_response(response)
    access = str(data.get("access_token") or "").strip()
    new_refresh = str(data.get("refresh_token") or "").strip()
    if not access or not new_refresh:
        raise CanvaApiError("Canva did not return a complete refreshed token.")
    connection.access_token_enc = encrypt_token(access)
    connection.refresh_token_enc = encrypt_token(new_refresh)
    connection.access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in") or 3600))
    connection.status = "active"
    connection.last_error = None
    db.flush()
    return access


def access_token(db: Session, connection: EstateCanvaConnection) -> str:
    require_configured()
    if connection.status != "active":
        raise CanvaApiError("Reconnect the Canva account before using it.", 409, "connection_inactive")
    if connection.access_token_expires_at and connection.access_token_expires_at <= datetime.now(timezone.utc) + timedelta(seconds=60):
        return _refresh(db, connection)
    return decrypt_token(connection.access_token_enc)


def request_json(db: Session, connection: EstateCanvaConnection, method: str, path: str, *, json_body: dict | None = None, body: bytes | None = None, headers: dict | None = None) -> dict:
    token = access_token(db, connection)
    request_headers = {"Authorization": f"Bearer {token}"}
    request_headers.update(headers or {})
    response = requests.request(method, f"{CANVA_API}{path}", headers=request_headers, json=json_body, data=body, timeout=30)
    if response.status_code == 401:
        token = _refresh(db, connection)
        request_headers["Authorization"] = f"Bearer {token}"
        response = requests.request(method, f"{CANVA_API}{path}", headers=request_headers, json=json_body, data=body, timeout=30)
    try:
        return _json_response(response)
    except CanvaApiError as exc:
        if exc.status in {401, 403}:
            connection.status = "needs_reconnect" if exc.status == 401 else connection.status
            connection.last_error = str(exc)[:500]
            db.flush()
        raise


def current_user(db: Session, connection: EstateCanvaConnection) -> tuple[str, str]:
    data = request_json(db, connection, "GET", "/users/me")
    user = data.get("team_user") or {}
    user_id, team_id = str(user.get("user_id") or "").strip(), str(user.get("team_id") or "").strip()
    if not user_id or not team_id:
        raise CanvaApiError("Canva did not return the connected user and team.")
    return user_id, team_id


def upload_image(db: Session, connection: EstateCanvaConnection, data: bytes, name: str) -> str:
    safe_name = str(name or "LandCheck flyer").strip()[:50] or "LandCheck flyer"
    metadata = json.dumps(
        {"name_base64": base64.b64encode(safe_name.encode("utf-8")).decode("ascii")},
        separators=(",", ":"),
    )
    created = request_json(db, connection, "POST", "/asset-uploads", body=data, headers={"Content-Type": "application/octet-stream", "Asset-Upload-Metadata": metadata})
    job = created.get("job") or {}
    job_id = str(job.get("id") or "")
    if not job_id:
        raise CanvaApiError("Canva did not return an asset upload job.")
    result = poll_job(db, connection, f"/asset-uploads/{job_id}")
    asset_id = str((result.get("asset") or {}).get("id") or "")
    if not asset_id:
        raise CanvaApiError("Canva completed the upload without returning an asset.")
    return asset_id


def poll_job(db: Session, connection: EstateCanvaConnection, path: str, *, timeout_seconds: int = 45) -> dict:
    started = time.monotonic()
    delay = 0.7
    while time.monotonic() - started < timeout_seconds:
        data = request_json(db, connection, "GET", path)
        job = data.get("job") or {}
        status = str(job.get("status") or "")
        if status == "success":
            return job
        if status == "failed":
            error = job.get("error") or {}
            raise CanvaApiError(str(error.get("message") or "Canva could not complete the job."), code=error.get("code"))
        time.sleep(delay)
        delay = min(delay * 1.5, 3.0)
    raise CanvaApiError("Canva is still processing this request. Try importing again shortly.", 504, "job_timeout")


def create_design(db: Session, connection: EstateCanvaConnection, *, asset_id: str, title: str, width: int, height: int) -> dict:
    width, height = max(40, min(int(width), 8000)), max(40, min(int(height), 8000))
    if width * height > 25_000_000:
        scale = (25_000_000 / (width * height)) ** 0.5
        width, height = max(40, int(width * scale)), max(40, int(height * scale))
    data = request_json(db, connection, "POST", "/designs", json_body={"type": "type_and_asset", "design_type": {"type": "custom", "width": width, "height": height}, "asset_id": asset_id, "title": title[:255]})
    return data.get("design") or {}


def export_design(db: Session, connection: EstateCanvaConnection, design_id: str, export_format: str) -> bytes:
    export_format = "jpg" if export_format == "jpg" else "png"
    created = request_json(db, connection, "POST", "/exports", json_body={"design_id": design_id, "format": {"type": export_format}})
    job_id = str((created.get("job") or {}).get("id") or "")
    if not job_id:
        raise CanvaApiError("Canva did not return an export job.")
    job = poll_job(db, connection, f"/exports/{job_id}")
    urls = job.get("urls") or []
    if not urls:
        raise CanvaApiError("Canva completed the export without a download URL.")
    response = requests.get(str(urls[0]), timeout=30)
    if response.status_code >= 400 or not response.content:
        raise CanvaApiError("The Canva export could not be downloaded.", response.status_code)
    return bytes(response.content)


_JWKS_CACHE: tuple[float, dict] | None = None


def verify_correlation_jwt(token: str) -> dict:
    require_configured()
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise CanvaApiError("Invalid Canva return token.", 400, "invalid_return")
    decoder = lambda value: json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    try:
        header, payload = decoder(parts[0]), decoder(parts[1])
        signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    except (ValueError, json.JSONDecodeError) as exc:
        raise CanvaApiError("Invalid Canva return token.", 400, "invalid_return") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise CanvaApiError("Unsupported Canva return token.", 400, "invalid_return")
    global _JWKS_CACHE
    if _JWKS_CACHE is None or _JWKS_CACHE[0] <= time.time():
        response = requests.get(CANVA_KEYS, timeout=10)
        if response.status_code >= 400:
            raise CanvaApiError("Canva signing keys could not be fetched.", 502, "keys_unavailable")
        _JWKS_CACHE = (time.time() + 3600, response.json())
    jwk = next((key for key in (_JWKS_CACHE[1].get("keys") or []) if key.get("kid") == header.get("kid")), None)
    if not jwk:
        raise CanvaApiError("Unknown Canva return token key.", 400, "invalid_return")
    try:
        key = rsa.RSAPublicNumbers(
            int.from_bytes(base64.urlsafe_b64decode(str(jwk["e"]) + "=" * (-len(str(jwk["e"])) % 4)), "big"),
            int.from_bytes(base64.urlsafe_b64decode(str(jwk["n"]) + "=" * (-len(str(jwk["n"])) % 4)), "big"),
        ).public_key()
        key.verify(signature, f"{parts[0]}.{parts[1]}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except (KeyError, ValueError, InvalidSignature) as exc:
        raise CanvaApiError("Invalid Canva return token signature.", 400, "invalid_return") from exc
    if payload.get("aud") != client_id() or payload.get("type") != "rti" or int(payload.get("exp") or 0) <= int(time.time()):
        raise CanvaApiError("Expired or invalid Canva return token.", 400, "invalid_return")
    return payload
