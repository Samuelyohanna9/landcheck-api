"""Runtime schema-bootstrap policy.

Schema creation and alteration belongs to the one-shot migration/bootstrap process.  API and
worker processes may still call the legacy ensure_* helpers for compatibility, but in production
those helpers must only verify that the migrated objects exist; they must never attempt DDL with a
least-privilege application role.
"""

from __future__ import annotations

import os

from sqlalchemy import text


def runtime_schema_bootstrap_enabled() -> bool:
    """Return whether a process is explicitly authorised to perform legacy bootstrap DDL."""

    raw = str(os.getenv("LANDCHECK_RUNTIME_SCHEMA_BOOTSTRAP", "false") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def require_table(connection, table_name: str) -> None:
    """Fail closed when a migrated legacy table is missing.

    ``table_name`` is always an internal constant at call sites; it is passed as a bind value so
    this helper cannot turn a user-controlled identifier into SQL.
    """

    exists = connection.execute(
        text("SELECT to_regclass(:table_name)"),
        {"table_name": table_name},
    ).scalar()
    if exists is None:
        raise RuntimeError(
            f"Required table {table_name!r} is missing. Run the one-shot migration/bootstrap "
            "process before starting API or worker processes."
        )

