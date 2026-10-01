# app/main.py
import os
from time import perf_counter

from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware  # Added for speed
from fastapi.responses import JSONResponse
from sqlalchemy import text

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import contextily as ctx

from app.db import SessionLocal
from app.routers import (
    health,
    plots,
    analytics,
    feedback,
    hazards,
    green,
    green_work,
    green_sponsor,
    green_reports,
    green_remote_monitoring,
    green_payouts,
    survey_georeference,
    survey_auth,
    plan_reader,
    field_to_finish,
    estates,
    estate_auth,
    estate_billing,
    estate_marketing,
    estate_social,
    estate_legal,
)
from app.db_init import init_db
from app.utils.activity_logger import ensure_activity_log_table, log_request_activity, should_skip_request_logging
from app.utils.auth_security import resolve_request_session
from app.utils.auth_security import require_authenticated_session
from app.utils.rate_limit import allow_request
from app.utils.row_security import set_survey_user_context
from app.services.estates.identity import resolve_session as resolve_estate_session
from app.utils.survey_auth_security import require_survey_session
from app.utils.survey_auth_security import resolve_survey_session

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


_GREEN_PUBLIC_PREFIXES = (
    "/green/public/",
    "/green/public-projects",
    "/green/sponsor/public/",
    "/green/sponsor-auth/",
    "/green/work-auth/",
    "/green/green-auth/",
    "/green/merchant/",
    "/green/merchant-auth/",
    "/green/track-order",
    "/green/sponsor-engagement/unsubscribe",
    "/green/sponsor/public/reset-password",
)


def _requires_green_session(request: Request) -> bool:
    if _is_cors_preflight_request(request):
        return False
    path = str(request.url.path or "").strip().lower()
    method = str(request.method or "").strip().upper()
    if not path.startswith("/green/"):
        return False
    if path in {"/green/privacy/policy", "/green/privacy/consents", "/green/app/version-check", "/green/public/logs"}:
        return False
    if path == "/green/auth/logout":
        return False
    if path.startswith("/green/admin/sponsor-agent-payouts/flutterwave/callback"):
        return False
    if any(path.startswith(prefix) for prefix in _GREEN_PUBLIC_PREFIXES):
        return False
    if path.startswith("/green/sponsor/payments/flutterwave/"):
        return False
    if path == "/green/sponsor/orders" and method == "POST":
        return False
    if path == "/green/sponsor/public/order-lookup":
        return False
    if path == "/green/organizations/logo-proxy":
        return False
    # Every remaining Green endpoint is private. Route handlers still enforce the precise actor,
    # organization, role, and sponsor/project scope; this closes forgotten-route gaps centrally.
    return True


def _requires_estate_session(request: Request) -> bool:
    if _is_cors_preflight_request(request):
        return False
    path = str(request.url.path or "").strip().lower()
    if not path.startswith("/estates/"):
        return False
    public_prefixes = (
        "/estates/auth/",
        "/estates/public/",
        "/estates/buyer/",
        "/estates/agent-portal/",
    )
    if any(path.startswith(prefix) for prefix in public_prefixes):
        return False
    if path in {
        "/estates/billing/plans",
        "/estates/billing/checkout/return",
        "/estates/billing/webhook",
    }:
        return False
    return True


def _enforce_survey_resource_scope(db, request: Request) -> None:
    """Protect legacy Survey resources that predate router-level auth dependencies."""
    path = str(request.url.path or "").strip().lower()

    def table_exists(table_name: str) -> bool:
        return db.execute(text("SELECT to_regclass(:table_name)"), {"table_name": table_name}).scalar() is not None

    if path.startswith("/plots/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[1].isdigit():
            if not table_exists("plots"):
                return
            session = require_survey_session(db, request)
            owner_id = db.execute(
                text("SELECT owner_user_id FROM plots WHERE id = :plot_id"),
                {"plot_id": int(parts[1])},
            ).scalar()
            if owner_id is not None and int(owner_id) != int(session.user_id):
                raise HTTPException(status_code=404, detail="Plot not found")
    if path.startswith("/hazards/jobs/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[2] != "mine":
            if not table_exists("hazard_analysis_jobs"):
                return
            session = require_survey_session(db, request)
            owner_id = db.execute(
                text("SELECT owner_user_id FROM hazard_analysis_jobs WHERE id = :job_id"),
                {"job_id": parts[2]},
            ).scalar()
            if owner_id is not None and int(owner_id) != int(session.user_id):
                raise HTTPException(status_code=404, detail="Hazard job not found")
    if path.startswith("/survey-georeference/sessions/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[2] not in {"mine", "claim"}:
            if not table_exists("survey_georeference_sessions"):
                return
            session = require_survey_session(db, request)
            owner_id = db.execute(
                text("SELECT owner_user_id FROM survey_georeference_sessions WHERE id = :session_id"),
                {"session_id": parts[2]},
            ).scalar()
            if owner_id is not None and int(owner_id) != int(session.user_id):
                raise HTTPException(status_code=404, detail="Georeference session not found")

    if path.startswith("/plots/subdivision/batches/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 4 and parts[3].isdigit():
            if not table_exists("plot_subdivision_batches") or not table_exists("plots"):
                return
            session = require_survey_session(db, request)
            owner_id = db.execute(
                text(
                    """
                    SELECT p.owner_user_id
                    FROM plot_subdivision_batches b
                    JOIN plots p ON p.id = b.parent_plot_id
                    WHERE b.id = :batch_id
                    """
                ),
                {"batch_id": int(parts[3])},
            ).scalar()
            if owner_id is not None and int(owner_id) != int(session.user_id):
                raise HTTPException(status_code=404, detail="Subdivision batch not found")

    if path.startswith("/plots/export-jobs/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[2].isdigit():
            if not table_exists("plot_export_jobs"):
                return
            session = require_survey_session(db, request)
            owner_id = db.execute(
                text(
                    """
                    SELECT COALESCE(p.owner_user_id, parent_plot.owner_user_id)
                    FROM plot_export_jobs j
                    LEFT JOIN plots p ON p.id = j.plot_id
                    LEFT JOIN plot_subdivision_batches b ON b.id = j.subdivision_batch_id
                    LEFT JOIN plots parent_plot ON parent_plot.id = b.parent_plot_id
                    WHERE j.id = :job_id
                    """
                ),
                {"job_id": int(parts[2])},
            ).scalar()
            if owner_id is not None and int(owner_id) != int(session.user_id):
                raise HTTPException(status_code=404, detail="Export job not found")


def _enforce_private_request_sync(request: Request) -> None:
    session_db = SessionLocal()
    try:
        if _requires_green_session(request):
            require_authenticated_session(session_db, request, auth_modes={"partner_user", "env_admin", "sponsor_user"})
        if _requires_estate_session(request):
            if resolve_estate_session(session_db, request) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
        if str(request.url.path or "").lower().startswith(("/plots/", "/hazards/jobs/", "/survey-georeference/sessions/")):
            survey_session = resolve_survey_session(session_db, request)
            set_survey_user_context(session_db, survey_session.user_id if survey_session else None)
        _enforce_survey_resource_scope(session_db, request)
    finally:
        session_db.close()


def _validate_production_configuration() -> None:
    if not _env_bool("LANDCHECK_ENV_PRODUCTION_CHECKS", True):
        return
    environment = str(os.getenv("LANDCHECK_ENV") or os.getenv("APP_ENV") or "").strip().lower()
    if environment not in {"prod", "production", "live"}:
        return
    required = {"DATABASE_URL": os.getenv("DATABASE_URL"), "CORS_ALLOW_ORIGINS": os.getenv("CORS_ALLOW_ORIGINS"), "LANDCHECK_API_PUBLIC_URL": os.getenv("LANDCHECK_API_PUBLIC_URL")}
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(f"Missing production security configuration: {', '.join(missing)}")
    if not str(required["LANDCHECK_API_PUBLIC_URL"]).strip().lower().startswith("https://"):
        raise RuntimeError("LANDCHECK_API_PUBLIC_URL must use HTTPS in production")
    if "*" in _parse_csv_env("CORS_ALLOW_ORIGINS"):
        raise RuntimeError("CORS_ALLOW_ORIGINS must not contain '*' in production")
    if str(os.getenv("CORS_ALLOW_ORIGIN_REGEX") or "").strip():
        raise RuntimeError("CORS_ALLOW_ORIGIN_REGEX must be empty in production")
    if str(os.getenv("DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}:
        raise RuntimeError("DEBUG must be disabled in production")
    if str(os.getenv("LANDCHECK_ENV_ADMIN_MFA_ENABLED") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("LANDCHECK_ENV_ADMIN_MFA_ENABLED must be true in production")
    for name in ("WORK_USERNAME", "WORK_PASSWORD"):
        if str(os.getenv(name) or "").strip().lower() in {"", "change_me", "changeme", "password", "admin"}:
            raise RuntimeError(f"{name} must be replaced with a strong server-side secret")

scheduler = BackgroundScheduler(timezone="Africa/Lagos")


def _run_birthday_gift_celebration_check_job():
    session_db = SessionLocal()
    try:
        green._run_birthday_gift_celebration_check(session_db)
        session_db.commit()
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


def _run_sponsor_engagement_email_check_job():
    session_db = SessionLocal()
    try:
        green._run_sponsor_engagement_email_check(session_db)
        session_db.commit()
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


def _run_georeference_retention_cleanup_job():
    session_db = SessionLocal()
    try:
        survey_georeference.cleanup_expired_georeference_sessions(session_db)
        session_db.commit()
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


def _run_stale_plot_export_job_sweep():
    session_db = SessionLocal()
    try:
        plots.sweep_stale_plot_export_jobs(session_db)
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


# Arbitrary fixed key for the estate-billing Postgres advisory lock below - any bigint works as
# long as it's unique to this job and stable across deploys.
_ESTATE_SUBSCRIPTION_BILLING_LOCK_KEY = 872341001


def _run_estate_subscription_billing_job():
    from sqlalchemy import text

    from app.services.estates import subscriptions as estate_subscriptions

    session_db = SessionLocal()
    try:
        # uvicorn runs multiple worker processes (UVICORN_WORKERS), each with its own
        # APScheduler instance, so this cron can fire concurrently in more than one process.
        # A session-level advisory lock makes sure only one of them actually runs the sweep;
        # the rest no-op instead of racing to charge the same subscriptions.
        got_lock = session_db.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _ESTATE_SUBSCRIPTION_BILLING_LOCK_KEY}
        ).scalar()
        if not got_lock:
            return
        try:
            estate_subscriptions.process_due_billing(session_db)
            # Send trial/renewal reminders in the same locked billing sweep so multiple
            # application workers cannot send the same subscription email twice.
            estate_subscriptions.send_due_subscription_reminders(session_db)
            session_db.commit()
        except Exception:
            session_db.rollback()
            raise
        finally:
            session_db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _ESTATE_SUBSCRIPTION_BILLING_LOCK_KEY})
    finally:
        session_db.close()


_ESTATE_RESERVATION_EXPIRY_LOCK_KEY = 872341002


def _run_estate_reservation_expiry_job():
    from sqlalchemy import text

    from app.services.estates.operations import expire_due_reservations

    session_db = SessionLocal()
    try:
        got_lock = session_db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _ESTATE_RESERVATION_EXPIRY_LOCK_KEY}).scalar()
        if not got_lock:
            return
        try:
            expire_due_reservations(session_db)
            session_db.commit()
        except Exception:
            session_db.rollback()
            raise
        finally:
            session_db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _ESTATE_RESERVATION_EXPIRY_LOCK_KEY})
    finally:
        session_db.close()


_ESTATE_PAYMENT_REMINDER_LOCK_KEY = 872341003


def _run_estate_payment_reminder_job():
    from sqlalchemy import text

    from app.services.estates.operations import send_due_payment_reminders

    session_db = SessionLocal()
    try:
        got_lock = session_db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _ESTATE_PAYMENT_REMINDER_LOCK_KEY}).scalar()
        if not got_lock:
            return
        try:
            send_due_payment_reminders(session_db)
            session_db.commit()
        except Exception:
            session_db.rollback()
            raise
        finally:
            session_db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _ESTATE_PAYMENT_REMINDER_LOCK_KEY})
    finally:
        session_db.close()


_ESTATE_LEAD_FOLLOWUP_LOCK_KEY = 872341004
_ESTATE_INSPECTION_REMINDER_LOCK_KEY = 872341005


def _run_locked_estate_marketing_job(lock_key: int, work) -> None:
    """Runs one marketing sweep under a Postgres advisory lock so only one worker process sends it."""
    from sqlalchemy import text

    session_db = SessionLocal()
    try:
        got_lock = session_db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}).scalar()
        if not got_lock:
            return
        try:
            work(session_db)
            session_db.commit()
        except Exception:
            session_db.rollback()
            raise
        finally:
            session_db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
    finally:
        session_db.close()


def _run_estate_lead_followup_job():
    from app.services.estates.marketing_alerts import send_lead_followup_reminders

    _run_locked_estate_marketing_job(_ESTATE_LEAD_FOLLOWUP_LOCK_KEY, send_lead_followup_reminders)


def _run_estate_inspection_reminder_job():
    from app.services.estates.marketing_alerts import send_inspection_reminders

    _run_locked_estate_marketing_job(_ESTATE_INSPECTION_REMINDER_LOCK_KEY, send_inspection_reminders)


_ESTATE_SOCIAL_LOCK_KEY = 872341006


def _run_estate_social_job():
    from app.services.estates.social_posts import run_social_sweeps

    _run_locked_estate_marketing_job(_ESTATE_SOCIAL_LOCK_KEY, run_social_sweeps)


# Preserve the legacy Survey/Green bootstrap; Estate schema is managed by Alembic.
@app.on_event("startup")
def startup_event():
    _validate_production_configuration()
    init_db()
    ensure_activity_log_table()
    green.bootstrap_green_schema()
    # Persistent on-disk cache for basemap tiles (orthophoto/topo rendering) so a plot rendered
    # more than once reuses already-fetched tiles instead of re-hitting the external imagery
    # provider every time - see app/utils/orthophoto_renderer.py.
    try:
        tile_cache_dir = os.path.join(os.path.dirname(__file__), "reports", "tile_cache")
        os.makedirs(tile_cache_dir, exist_ok=True)
        ctx.set_cache_dir(tile_cache_dir)
    except Exception:
        pass
    # Fires the birthday-gift celebration email once daily — see _run_birthday_gift_celebration_check
    # in green.py. Safe even if this ever runs across multiple instances, since each order is
    # atomically claimed (UPDATE ... WHERE birthday_email_sent_at IS NULL) before its email sends.
    scheduler.add_job(
        _run_birthday_gift_celebration_check_job,
        trigger=CronTrigger(hour=8, minute=0),
        id="birthday_gift_celebration_check",
        replace_existing=True,
    )
    # Monthly-cadence marketing emails (prospect nudge / thank-you) — see
    # _run_sponsor_engagement_email_check in green.py. Runs at a different hour than the birthday
    # check purely to spread load; same atomic per-account claim makes it safe to run more than once.
    scheduler.add_job(
        _run_sponsor_engagement_email_check_job,
        trigger=CronTrigger(hour=9, minute=30),
        id="sponsor_engagement_email_check",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_georeference_retention_cleanup_job,
        trigger=CronTrigger(hour="*/6"),
        id="georeference_retention_cleanup",
        replace_existing=True,
    )
    # Frees up any export job stuck in "running" for too long (e.g. a hung tile fetch) so it
    # stops permanently blocking retries of the same export - see sweep_stale_plot_export_jobs.
    scheduler.add_job(
        _run_stale_plot_export_job_sweep,
        trigger=CronTrigger(minute="*/5"),
        id="stale_plot_export_job_sweep",
        replace_existing=True,
    )
    # Converts due trials, charges due renewals, retries due dunning attempts, and finalizes
    # cancellations whose paid period has ended - see subscriptions.process_due_billing.
    scheduler.add_job(
        _run_estate_subscription_billing_job,
        trigger=CronTrigger(hour=6, minute=0),
        id="estate_subscription_billing",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_estate_reservation_expiry_job,
        trigger=CronTrigger(minute="*/5"),
        id="estate_reservation_expiry",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_estate_payment_reminder_job,
        trigger=CronTrigger(hour="*/2"),
        id="estate_payment_reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_estate_lead_followup_job,
        trigger=CronTrigger(minute="*/20"),
        id="estate_lead_followup",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_estate_inspection_reminder_job,
        trigger=CronTrigger(minute="*/30"),
        id="estate_inspection_reminders",
        replace_existing=True,
    )
    # Scheduled social posts, "time to post" reminders and queued WhatsApp updates.
    scheduler.add_job(
        _run_estate_social_job,
        trigger=CronTrigger(minute="*"),
        id="estate_social_sweeps",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown(wait=False)

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

_production_environment = str(os.getenv("LANDCHECK_ENV") or os.getenv("APP_ENV") or "").strip().lower() in {"prod", "production", "live"}
if _production_environment:
    default_origins = []

origins: list[str] = []
for origin in [*default_origins, *_parse_csv_env("CORS_ALLOW_ORIGINS")]:
    clean = origin.strip().rstrip("/")
    if clean and clean not in origins:
        origins.append(clean)

default_local_origin_regex = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
configured_origin_regex = str(os.getenv("CORS_ALLOW_ORIGIN_REGEX", "") or "").strip()
local_origin_regex = None if _production_environment else (configured_origin_regex or default_local_origin_regex)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=local_origin_regex or None,
    allow_credentials=True,  # Now allowed because we specified origins
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type", "X-LC-Auth-Mode", "X-LC-App-Route", "X-LC-Client", "X-LC-Organization-Id", "X-LC-Role-Key", "X-LC-Session-App-Mode", "X-LC-User-Id", "X-LC-User-Name", "X-Idempotency-Key", "X-Requested-With"],
    expose_headers=["Content-Disposition", "X-Request-Id"],
)


def _rate_limit_for_path(path: str) -> tuple[int, int] | None:
    path = str(path or "").lower()
    if path in {
        "/estates/auth/login",
        "/green/work-auth/login",
        "/green/green-auth/login",
        "/green/sponsor-auth/login",
        "/green/auth/mfa/verify",
    }:
        return 10, 300
    if path in {
        "/estates/auth/register",
        "/green/work-auth/register",
        "/green/sponsor-auth/signup",
    }:
        return 5, 900
    if path in {
        "/estates/auth/forgot-password",
        "/estates/auth/reset-password",
        "/green/green-auth/forgot-password",
        "/green/green-auth/reset-password",
        "/survey/auth/magic-link/request",
        "/survey/auth/otp/verify",
        "/green/sponsor/guest/claim",
    }:
        return 5, 900
    return None


@app.middleware("http")
async def rate_limit_sensitive_requests(request: Request, call_next):
    policy = _rate_limit_for_path(request.url.path)
    if policy:
        limit, window_seconds = policy
        client_host = str(request.client.host if request.client else "unknown")
        allowed, retry_after = allow_request(
            f"{client_host}:{str(request.url.path).lower()}",
            limit=limit,
            window_seconds=window_seconds,
        )
        if not allowed:
            return JSONResponse(
                {"detail": "Too many attempts. Please try again later."},
                status_code=429,
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )
    return await call_next(request)


def _resolve_request_session_sync(request: Request) -> None:
    """Look the bearer token up on a short-lived session and give the connection straight back."""
    session_db = SessionLocal()
    try:
        try:
            resolve_request_session(session_db, request)
        except Exception:
            session_db.rollback()
    finally:
        try:
            session_db.close()
        except Exception:
            pass


@app.middleware("http")
async def capture_system_activity(request: Request, call_next):
    started_at = perf_counter()
    try:
        # Sync database work must never run on the event loop: when the pool is busy it blocks
        # until a connection frees up, and with the loop blocked nothing can finish and release one.
        await run_in_threadpool(_resolve_request_session_sync, request)
        await run_in_threadpool(_enforce_private_request_sync, request)
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
            await run_in_threadpool(log_request_activity, request, status_code=exc.status_code, duration_ms=duration_ms, error_message=str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except Exception as exc:
        duration_ms = (perf_counter() - started_at) * 1000
        if _should_log_request_activity(request, status_code=500, duration_ms=duration_ms):
            await run_in_threadpool(log_request_activity, request, status_code=500, duration_ms=duration_ms, error_message=str(exc))
        raise
    duration_ms = (perf_counter() - started_at) * 1000
    if _should_log_request_activity(request, status_code=response.status_code, duration_ms=duration_ms):
        await run_in_threadpool(log_request_activity, request, status_code=response.status_code, duration_ms=duration_ms)
    return response


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(self)")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
    response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Embedder-Policy", "unsafe-none")
    if str(response.headers.get("content-type") or "").lower().startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
    if _production_environment:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path.startswith(("/estates/auth/", "/survey/auth/", "/green/auth/", "/green/work-auth/", "/green/green-auth/", "/green/sponsor-auth/")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# Routers
app.include_router(health.router)
app.include_router(plots.router)
app.include_router(analytics.router)
app.include_router(feedback.router)
app.include_router(hazards.router)
app.include_router(green_work.router)
app.include_router(green_sponsor.router)
app.include_router(green_reports.router)
app.include_router(green_remote_monitoring.router)
app.include_router(green_payouts.router)
app.include_router(survey_auth.router)
app.include_router(survey_georeference.router)
app.include_router(plan_reader.router)
app.include_router(field_to_finish.router)
app.include_router(estates.router)
app.include_router(estate_auth.router)
app.include_router(estate_billing.router)
app.include_router(estate_marketing.router)
app.include_router(estate_social.router)
app.include_router(estate_legal.router)

@app.get("/")
def root():
    return {"status": "ok"}
