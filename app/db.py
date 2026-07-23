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


engine = create_engine(
    DATABASE_URL,
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
