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


engine = create_engine(DATABASE_URL, echo=_env_bool("SQLALCHEMY_ECHO", False))

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)
