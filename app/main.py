# app/main.py

from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware  # Added for speed

from app.routers import health, plots, analytics, feedback, hazards, green
from app.db_init import init_db
from app.utils.activity_logger import ensure_activity_log_table, log_request_activity, should_skip_request_logging

app = FastAPI(title="LandCheck API")

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
origins = [
    "https://landcheck.online",
    "https://www.landcheck.online",
    "https://landcheck-web.pages.dev",  # Keep for testing
    "http://localhost:3000",             # Keep for local dev if needed
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
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
