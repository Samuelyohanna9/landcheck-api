from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


# Server-side safety nets so one stuck request can never hold the whole pool hostage:
# - lock_timeout: a statement waiting on another transaction's lock gives up (and the request
#   fails fast) instead of queueing while holding a pooled connection;
# - idle_in_transaction_session_timeout: a connection whose owner went quiet inside an open
#   transaction is closed by Postgres, releasing its locks.
_connect_args = {}
if str(DATABASE_URL or "").startswith(("postgresql", "postgres")):
    _lock_ms = max(_env_int("DB_LOCK_TIMEOUT_MS", 20000), 0)
    _idle_tx_ms = max(_env_int("DB_IDLE_TX_TIMEOUT_MS", 300000), 0)
    _connect_args["options"] = f"-c lock_timeout={_lock_ms} -c idle_in_transaction_session_timeout={_idle_tx_ms}"

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    echo=_env_bool("SQLALCHEMY_ECHO", False),
    pool_pre_ping=_env_bool("SQLALCHEMY_POOL_PRE_PING", True),
    pool_recycle=max(_env_int("SQLALCHEMY_POOL_RECYCLE", 1800), 60),
    pool_size=max(_env_int("SQLALCHEMY_POOL_SIZE", 10), 1),
    max_overflow=max(_env_int("SQLALCHEMY_MAX_OVERFLOW", 20), 0),
    pool_timeout=max(_env_int("SQLALCHEMY_POOL_TIMEOUT", 30), 5),
    pool_use_lifo=_env_bool("SQLALCHEMY_POOL_USE_LIFO", True),
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)
