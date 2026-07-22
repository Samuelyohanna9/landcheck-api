# app/main.py
import os
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware  # Added for speed

from app.routers import health, plots, analytics, feedback, hazards, green
from app.db_init import init_db
from app.utils.activity_logger import ensure_activity_log_table, log_request_activity, should_skip_request_logging

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

# ✅ Create tables on startup
@app.on_event("startup")
def startup_event():
    init_db()
    ensure_activity_log_table()

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
    if should_skip_request_logging(request):
        return await call_next(request)
    started_at = perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = (perf_counter() - started_at) * 1000
        log_request_activity(request, status_code=500, duration_ms=duration_ms, error_message=str(exc))
        raise
    duration_ms = (perf_counter() - started_at) * 1000
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
