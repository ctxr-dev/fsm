"""``ctxr-fsm doctor`` — diagnostic dump for the project DB.

The doctor command is the operator's first stop when "something looks
off". It opens the project (running migrations, so the schema is
guaranteed current), then surfaces the runtime facts that determine
correctness:

* The resolved DB path and the on-disk file size.
* The SQLite version actually loaded by the Python build (the
  ``sqlite3`` module ships its own copy, which can drift from the
  system library).
* The PRAGMAs we care about — journal_mode, foreign_keys, etc. —
  read live via :func:`detect_journal_state` so we observe the values
  the connect-time listener actually applied.
* The current alembic revision so the operator can correlate against
  the migrations directory.
* The list of user tables in the database plus a per-table row count
  (useful to spot drift between the schema and what the engine has
  populated).
* The :class:`JournalTxnTable` breakdown by status
  (``pending`` / ``ready_to_finalise`` / ``finalised``) so a
  half-finalised commit is visible at a glance.
* The :class:`LockTable` row count so an operator can see whether any
  runs hold the single-writer lock.

Output is either rich-formatted (default) or pure JSON (``--json``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from ctxr.fsm.cli._common import (
    DB_OPTION,
    JSON_OPTION,
    json_or_pretty,
    open_project_for_cli,
    resolve_db_path,
)
from ctxr.fsm.sqlite.connection import detect_journal_state
from ctxr.fsm.sqlite.models_core import LockTable

__all__ = ["doctor"]


def _list_tables(engine: Any) -> list[str]:
    """Return user table names sorted alphabetically.

    We skip ``sqlite_%`` internal tables but include
    ``alembic_version`` (it is user-visible bookkeeping and operators
    expect to see it in the doctor output).
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ).all()
    return [row[0] for row in rows]


def _count_rows(engine: Any, table_name: str) -> int:
    """Return ``COUNT(*)`` for ``table_name`` using raw SQL.

    We use raw SQL (with a parameter-free, validated identifier from
    :func:`_list_tables`) because the ORM mapping for some bookkeeping
    tables (eg. ``alembic_version``) is not declared in our SQLModel
    modules. The table name is supplied by ``sqlite_master`` so it is
    not user-controlled — there is no SQL-injection vector here.
    """
    with engine.connect() as conn:
        return int(conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar() or 0)


def _alembic_revision(engine: Any) -> str | None:
    """Return ``alembic_version.version_num`` for ``engine`` or ``None``."""
    with engine.connect() as conn:
        try:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        except Exception:
            return None
    return None if row is None else str(row[0])


def _journal_breakdown(session_factory: Any) -> dict[str, int]:
    """Return per-status counts for the journal_txns table.

    The three statuses match the ``Literal`` declared on
    :class:`ctxr.fsm.sqlite.repos_locks_journal.JournalTxn` so the
    output is stable even if a future migration adds a new status
    we have not accounted for here.
    """
    breakdown = {"pending": 0, "ready_to_finalise": 0, "finalised": 0}
    with session_factory() as session:
        # ``JournalTxnTable.status`` is typed as a bare ``str`` on the
        # SQLModel side (rather than ``Mapped[str]``), so the typed
        # ``select()`` overload does not accept it directly; we use
        # raw SQL here for the same reason the project's repo modules
        # disable the SQLAlchemy mypy overloads — see pyproject.toml.
        rows = session.execute(
            text(
                "SELECT status, COUNT(*) FROM journal_txns GROUP BY status"
            )
        ).all()
    for status, count in rows:
        # Tolerate unknown statuses by stashing them under their own
        # key — better than silently dropping data the operator needs
        # to know about.
        breakdown[str(status)] = int(count)
    return breakdown


def _locks_count(session_factory: Any) -> int:
    """Return the number of rows currently in the locks table."""
    with session_factory() as session:
        return int(
            session.execute(select(func.count()).select_from(LockTable)).scalar() or 0
        )


def doctor(
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Print a diagnostic report for the project DB.

    Opens the project (running migrations so the report describes a
    fully-current schema), then assembles the report and prints it via
    :func:`json_or_pretty`.
    """
    db_path = resolve_db_path(db)
    file_size = db_path.stat().st_size if db_path.exists() else 0

    with open_project_for_cli(db_path) as project:
        pragmas = detect_journal_state(project.engine)
        tables = _list_tables(project.engine)
        row_counts = {name: _count_rows(project.engine, name) for name in tables}
        revision = _alembic_revision(project.engine)
        journal = _journal_breakdown(project.session_factory)
        locks = _locks_count(project.session_factory)

    report: dict[str, Any] = {
        "db_path": str(db_path),
        "file_size_bytes": file_size,
        "sqlite_version": pragmas.get("sqlite_version"),
        "pragmas": {k: v for k, v in pragmas.items() if k != "sqlite_version"},
        "alembic_revision": revision,
        "tables": row_counts,
        "journal_txns": journal,
        "locks": {"count": locks},
    }
    json_or_pretty(report, json_mode)
