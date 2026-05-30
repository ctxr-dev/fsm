"""``ctxr-fsm migrate`` — run ``alembic upgrade head`` against the project DB.

This is the standalone migration command for callers that want to
upgrade a database without opening a project (deployment scripts,
CI pipelines, post-pull "make sure your DB is current" muscle memory).
We always invoke :func:`run_migrations` directly — never via
``subprocess(alembic)`` — so the command works regardless of whether
the user has an ``alembic`` binary on PATH or even has the
``[sqlite]`` extra installed (the dependency comes in transitively
via ``ctxr-fsm``).

The command brackets the upgrade with two ``SELECT version_num`` reads
so the operator can immediately see whether the migration actually
moved the schema forward. When the DB does not yet exist (first run on
a blank repo), the ``before`` revision is reported as ``None`` and the
``after`` revision is whatever ``alembic upgrade head`` lands on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from ctxr.fsm.cli._common import (
    DB_OPTION,
    JSON_OPTION,
    json_or_pretty,
    resolve_db_path,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.project import run_migrations

__all__ = ["migrate"]


def _read_revision(db_path: Path) -> str | None:
    """Return the current alembic revision for ``db_path`` or ``None``.

    ``None`` covers two cases: the file does not exist yet (first run)
    or the file exists but the ``alembic_version`` table was never
    created (someone hand-rolled a blank SQLite DB). Both warrant the
    same "no recorded revision" answer.
    """
    if not db_path.exists():
        return None
    # We deliberately bypass ``Project.open`` here because that helper
    # runs migrations as part of opening; calling it before our own
    # ``run_migrations`` would muddle the before / after report.
    from ctxr.fsm.sqlite.connection import open_engine

    engine = open_engine(db_path, echo=False)
    try:
        with engine.connect() as conn:
            try:
                row = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).first()
            except Exception:
                # The table doesn't exist yet on an uninitialised DB
                # — that is the "None" answer this function exists
                # to provide. Catching ``Exception`` (not just
                # ``OperationalError``) keeps us robust against the
                # DB-API quirks of older sqlite3 builds.
                return None
    finally:
        engine.dispose()
    if row is None:
        return None
    return str(row[0])


def migrate(
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Run ``alembic upgrade head`` against the project DB.

    Prints the alembic revision before and after the upgrade so the
    operator can tell whether the call actually moved the schema
    forward.
    """
    db_path = resolve_db_path(db)

    revision_before = _read_revision(db_path)
    run_migrations(db_path)
    revision_after = _read_revision(db_path)

    # Briefly open the project just to confirm the engine binds; this
    # gives us a fast smoke check that the migrated schema is
    # consistent with the code's expectations (a half-applied
    # migration would surface as a missing-table error here, not at
    # the next CLI call).
    with Project.open(db_path, migrate=False, echo=False):
        pass

    payload: dict[str, Any] = {
        "db_path": str(db_path),
        "revision_before": revision_before,
        "revision_after": revision_after,
        "upgraded": revision_before != revision_after,
    }
    json_or_pretty(payload, json_mode)
