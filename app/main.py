# app/main.py
import os
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware  # Added for speed
from fastapi.responses import JSONResponse

from app.db import SessionLocal
from app.routers import health, plots, analytics, feedback, hazards, green
from app.db_init import init_db
from app.utils.activity_logger import ensure_activity_log_table, log_request_activity, should_skip_request_logging
from app.utils.auth_security import resolve_request_session

app = FastAPI(title="LandCheck API")


def _parse_csv_env(name: str) -> list[str]:
    raw_value = str(os.getenv(name, "") or "").strip()
    if not raw_value:
        return []
    values: list[str] = []
    for item in raw_value.split(","):
        clean = item.strip().rstrip("/")
        if clean and clean not in values:
            values.append(clean)
    return values


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


REQUEST_ACTIVITY_LOG_ALL = _env_bool("REQUEST_ACTIVITY_LOG_ALL", False)
REQUEST_ACTIVITY_LOG_SLOW_MS = max(_env_int("REQUEST_ACTIVITY_LOG_SLOW_MS", 1500), 0)


def _should_log_request_activity(request: Request, *, status_code: int, duration_ms: float) -> bool:
    if should_skip_request_logging(request):
        return False
    if REQUEST_ACTIVITY_LOG_ALL:
        return True
    method = str(request.method or "").upper()
    if int(status_code) >= 400:
        return True
    if method != "GET":
        return True
    return float(duration_ms) >= float(REQUEST_ACTIVITY_LOG_SLOW_MS)


_GREEN_ADMIN_PUBLIC_CALLBACKS = {
    "/green/admin/sponsor-agent-payouts/flutterwave/callback",
}


def _is_cors_preflight_request(request: Request) -> bool:
    method = str(request.method or "").strip().upper()
    if method != "OPTIONS":
        return False
    origin = str(request.headers.get("origin") or "").strip()
    requested_method = str(request.headers.get("access-control-request-method") or "").strip()
    return bool(origin and requested_method)


def _requires_super_admin_session(request: Request) -> bool:
    if _is_cors_preflight_request(request):
        return False
    clean_path = str(request.url.path or "").strip().lower()
    if clean_path in _GREEN_ADMIN_PUBLIC_CALLBACKS:
        return False
    return clean_path.startswith("/green/admin/")

# ✅ Create tables on startup
@app.on_event("startup")
def startup_event():
    init_db()
    ensure_activity_log_table()
    green.bootstrap_green_schema()

# ✅ SPEED OPTIMIZATION: Gzip Compression
# This shrinks large JSON/Report data (like your 210MB I/O) before sending it
# through the Cloudflare Tunnel, making it up to 10x faster.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ✅ SECURITY OPTIMIZATION: Custom Domain CORS
# Replacing "*" with specific origins allows you to set allow_credentials=True
# which is required if you ever add logins or cookies.
default_origins = [
    "https://landcheck.online",
    "https://www.landcheck.online",
    "https://landcheck-web.pages.dev",  # Keep for testing
    "http://localhost:3000",             # Keep for local dev if needed
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]

origins: list[str] = []
for origin in [*default_origins, *_parse_csv_env("CORS_ALLOW_ORIGINS")]:
    clean = origin.strip().rstrip("/")
    if clean and clean not in origins:
        origins.append(clean)

default_local_origin_regex = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
configured_origin_regex = str(os.getenv("CORS_ALLOW_ORIGIN_REGEX", "") or "").strip()
local_origin_regex = configured_origin_regex or default_local_origin_regex

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=local_origin_regex or None,
    allow_credentials=True,  # Now allowed because we specified origins
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.middleware("http")
async def capture_system_activity(request: Request, call_next):
    started_at = perf_counter()
    session_db = SessionLocal()
    try:
        try:
            resolve_request_session(session_db, request)
        except Exception:
            session_db.rollback()
        if _requires_super_admin_session(request):
            session = getattr(request.state, "landcheck_session", None)
            if session is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            if not bool(getattr(session, "is_super_admin", False)):
                raise HTTPException(status_code=403, detail="Super Admin access is required for this action.")
        response = await call_next(request)
    except HTTPException as exc:
        duration_ms = (perf_counter() - started_at) * 1000
        if _should_log_request_activity(request, status_code=exc.status_code, duration_ms=duration_ms):
            log_request_activity(request, status_code=exc.status_code, duration_ms=duration_ms, error_message=str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except Exception as exc:
        duration_ms = (perf_counter() - started_at) * 1000
        if _should_log_request_activity(request, status_code=500, duration_ms=duration_ms):
            log_request_activity(request, status_code=500, duration_ms=duration_ms, error_message=str(exc))
        raise
    finally:
        try:
            session_db.close()
        except Exception:
            pass
    duration_ms = (perf_counter() - started_at) * 1000
    if _should_log_request_activity(request, status_code=response.status_code, duration_ms=duration_ms):
        log_request_activity(request, status_code=response.status_code, duration_ms=duration_ms)
    return response


# Routers
app.include_router(health.router)
app.include_router(plots.router)
app.include_router(analytics.router)
app.include_router(feedback.router)
app.include_router(hazards.router)
app.include_router(green.router)

@app.get("/")
def root():
    return {"status": "ok"}
