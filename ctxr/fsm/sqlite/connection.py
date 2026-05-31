"""SQLite connection management for ctxr.fsm.

Centralises engine creation, PRAGMA configuration, session helpers, and
diagnostic utilities used by the SQLite-backed repositories.

Conventions enforced here:
  * WAL journal mode for concurrent read while writing.
  * busy_timeout=5000 to ride out short lock contention rather than fail.
  * foreign_keys=ON because SQLite leaves them OFF by default.
  * synchronous=NORMAL — the WAL-recommended setting (safe + fast).
  * encoding='UTF-8' for text storage determinism.

STRICT-table caveat
-------------------
SQLite's STRICT table modifier MUST be declared at CREATE TABLE time; there is
no ALTER TABLE ... SET STRICT. We therefore cannot retrofit STRICT post-hoc.
``ensure_strict_tables`` only inspects ``sqlite_master`` and returns a report
(plus emits ``warnings.warn(...)``) for tables that lack STRICT. The actual
STRICT clause must be appended to the CREATE TABLE DDL — either via an
Alembic migration's ``op.execute(...)`` or by customising SQLAlchemy's DDL
compilation. This module deliberately stays in the diagnostics lane.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session

__all__ = [
    "detect_journal_state",
    "ensure_strict_tables",
    "open_engine",
    "open_session",
]


# PRAGMAs applied on every new DB-API connection. Order matters slightly:
# journal_mode first so subsequent reads of PRAGMA journal_mode observe WAL,
# then the cheaper toggles. Each statement is executed via the raw DB-API
# cursor — SQLAlchemy's connect listener fires before the Session layer is
# involved, so we cannot use ORM helpers here.
_CONNECT_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("busy_timeout", "5000"),
    ("foreign_keys", "ON"),
    ("synchronous", "NORMAL"),
    ("encoding", "'UTF-8'"),
)


def open_engine(db_path: Path | str, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy 2.0 Engine bound to ``db_path``.

    Ensures the parent directory exists, registers a ``connect`` listener that
    installs the project's standard PRAGMAs on every fresh connection, and
    returns the engine ready for use with ``open_session`` or raw ``connect``.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file. The parent directory is
        created on demand with ``parents=True, exist_ok=True``.
    echo:
        Forwarded to ``create_engine``; when True, SQLAlchemy logs every
        statement to stdout (useful for ad-hoc debugging).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # ``future=True`` is a no-op in SQLAlchemy 2.0 (it is the default) but we
    # pass it explicitly to make our intent obvious in code review and to
    # remain forward-compatible should the kwarg ever resurface.
    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=echo,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        # Pure DB-API here — SQLAlchemy ORM machinery is not yet wired up
        # when this fires. The cursor is closed eagerly to release resources;
        # SQLite ignores the result of a PRAGMA-set statement, but we still
        # need to drain it to avoid "unfinalized statement" warnings on
        # connection close in some sqlite3 builds.
        cursor = dbapi_connection.cursor()
        try:
            for pragma, value in _CONNECT_PRAGMAS:
                cursor.execute(f"PRAGMA {pragma}={value}")
                # PRAGMA assignments that read back a value (e.g. journal_mode)
                # need their result row consumed.
                cursor.fetchall()
        finally:
            cursor.close()

    return engine


def open_session(engine: Engine) -> Session:
    """Return a fresh ``Session`` bound to ``engine``.

    A thin convenience wrapper: callers that need transactional semantics
    should pair this with the ``@atomic`` decorator (W2) or use
    ``with session.begin():`` directly.
    """
    return Session(bind=engine, future=True, expire_on_commit=False)


def ensure_strict_tables(engine: Engine) -> dict[str, Any]:
    """Audit existing tables for SQLite STRICT mode.

    SQLite STRICT tables can only be declared at CREATE TABLE time, so this
    function does NOT attempt to add STRICT to existing tables. Instead it
    inspects ``sqlite_master`` and returns a diagnostic report identifying
    which tables already declare STRICT and which do not.

    Returns a dict of the form::

        {
            "strict": ["table_a", "table_b"],
            "non_strict": ["table_c"],
            "sqlite_version": "3.45.1",
            "supports_strict": True,
        }

    Emits a ``UserWarning`` for each non-STRICT project table so the issue is
    surfaced loudly during ``fsm doctor`` runs without raising. SQLite gained
    STRICT support in 3.37 (2021-11); on older builds we report
    ``supports_strict=False`` and skip the warning.
    """
    strict_tables: list[str] = []
    non_strict_tables: list[str] = []

    with engine.connect() as conn:
        sqlite_version: str = conn.execute(text("SELECT sqlite_version()")).scalar_one()
        supports_strict = _version_at_least(sqlite_version, (3, 37, 0))

        # Limit to user tables; skip sqlite internal bookkeeping and any view.
        rows = conn.execute(
            text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ).all()

    for name, sql in rows:
        # sqlite_master.sql preserves the original DDL verbatim, so a simple
        # case-insensitive substring check is sufficient and avoids dragging
        # in a SQL parser just for this diagnostic.
        if sql and "STRICT" in sql.upper():
            strict_tables.append(name)
        else:
            non_strict_tables.append(name)
            if supports_strict:
                warnings.warn(
                    f"Table {name!r} is not declared STRICT; "
                    "ctxr.fsm conventions require STRICT mode. "
                    "STRICT cannot be added post-hoc — recreate via migration.",
                    UserWarning,
                    stacklevel=2,
                )

    return {
        "strict": strict_tables,
        "non_strict": non_strict_tables,
        "sqlite_version": sqlite_version,
        "supports_strict": supports_strict,
    }


def detect_journal_state(engine: Engine) -> dict[str, Any]:
    """Return the current values of the PRAGMAs we care about.

    Used by ``fsm doctor`` and by tests to confirm that the connect-time
    listener actually ran. Always opens a fresh connection so we observe the
    live state rather than any cached value.
    """
    pragmas_to_read = (
        "journal_mode",
        "busy_timeout",
        "foreign_keys",
        "synchronous",
        "encoding",
        "page_size",
        "cache_size",
    )

    state: dict[str, Any] = {}
    with engine.connect() as conn:
        for pragma in pragmas_to_read:
            state[pragma] = conn.execute(text(f"PRAGMA {pragma}")).scalar()
        state["sqlite_version"] = conn.execute(text("SELECT sqlite_version()")).scalar_one()
    return state


def _version_at_least(version_str: str, minimum: tuple[int, int, int]) -> bool:
    """Compare a dotted SQLite version string against ``minimum``.

    Tolerates trailing labels (e.g. ``3.45.1-rc1``) by truncating non-digit
    fragments before comparison.
    """
    parts: list[int] = []
    for raw in version_str.split(".")[:3]:
        digits = "".join(ch for ch in raw if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts) >= minimum
