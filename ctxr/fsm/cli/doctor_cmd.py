"""``ctxr-fsm doctor`` — diagnostic dump for the project DB and supervisor.

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

W7 service-lifecycle extension
------------------------------

The W7 supervisor persists port assignments and singleton PID files
under ``<project_root>/.ctxr-fsm/`` (``ports.json`` and
``pids/<name>.pid``). The doctor report now surfaces, for each
subsystem (``mcp``, ``api``, ``ui``):

* The remembered port (from ``ports.json``), or ``None`` if no
  ``ctxr-fsm serve`` has booted that subsystem yet.
* The pid recorded in the singleton file (plus the recorded probe URL
  and ``acquired_at`` timestamp), or ``None`` if no pid file exists.
* A liveness flag (``pid_alive``) computed via
  :func:`pid_is_alive` — distinguishes "stale lock" from "running".
* A health-probe outcome (``healthz``) — the body of ``GET
  <probe_url>/healthz`` on success, ``None`` on any failure (no probe
  URL, connection refused, non-200, timeout). Reusing the same
  primitive the supervisor uses means a green doctor report and a
  supervisor "reuse" decision are observing the same signal.

The supervisor section sits alongside the existing DB section in both
the pretty (``rich.print``) and JSON output paths so an operator
chaining ``ctxr-fsm doctor --json | jq`` can pull either domain.

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
from ctxr.fsm.cli.lifecycle.primitives import (
    _probe_healthz,
    pid_is_alive,
    read_pid_file,
    recall_port,
)
from ctxr.fsm.sqlite.connection import detect_journal_state
from ctxr.fsm.sqlite.models_core import LockTable

__all__ = ["doctor"]


# The subsystems the supervisor manages. Kept as a module constant so
# the doctor and the supervisor share one source of truth for "which
# names does ``.ctxr-fsm/`` know about" — adding a fourth subsystem
# only needs to touch one place.
_SUPERVISOR_SUBSYSTEMS: tuple[str, ...] = ("mcp", "api", "ui")


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


def _supervisor_subsystem_report(
    name: str, *, project_root: Path
) -> dict[str, Any]:
    """Return the doctor sub-report for one supervisor-managed subsystem.

    Fields:

    * ``name`` — the subsystem name (``mcp`` / ``api`` / ``ui``).
    * ``port`` — the port last remembered via :func:`recall_port`, or
      ``None`` if the supervisor has never booted this subsystem in
      this project.
    * ``pid`` / ``probe_url`` / ``acquired_at`` — fields read straight
      from the singleton pid file (``.ctxr-fsm/pids/<name>.pid``).
      ``None`` when the file is missing or malformed; ``read_pid_file``
      already collapses both cases for us.
    * ``pid_alive`` — :func:`pid_is_alive` result for the recorded pid;
      ``False`` when no pid is recorded so the field stays a strict
      bool (operator scripts can rely on the shape).
    * ``healthz`` — body of ``GET <probe_url>/healthz`` on success,
      otherwise ``None``. We reuse the supervisor's private probe
      helper so a green doctor report matches what the supervisor
      itself observes when deciding whether to reuse an existing
      instance.
    """
    port = recall_port(name, project_root=project_root)

    pid_path = project_root / ".ctxr-fsm" / "pids" / f"{name}.pid"
    pid_record = read_pid_file(pid_path)

    pid: int | None = None
    probe_url: str | None = None
    acquired_at: str | None = None
    if pid_record is not None:
        raw_pid = pid_record.get("pid")
        if isinstance(raw_pid, int):
            pid = raw_pid
        raw_probe = pid_record.get("probe_url")
        if isinstance(raw_probe, str) and raw_probe:
            probe_url = raw_probe
        raw_acquired = pid_record.get("acquired_at")
        if isinstance(raw_acquired, str):
            acquired_at = raw_acquired

    alive = pid_is_alive(pid) if pid is not None else False

    # Health probe is best-effort. We only attempt it when there's a
    # probe URL recorded *and* the pid looks alive — probing a stale
    # URL whose owner is gone would just burn the 1s HTTP timeout for
    # no diagnostic value. ``_probe_healthz`` already swallows every
    # transport error and returns ``None`` for the non-200 case.
    healthz: str | None = None
    if alive and probe_url is not None:
        body = _probe_healthz(probe_url)
        if body is not None:
            healthz = body.strip() or "ok"

    return {
        "name": name,
        "port": port,
        "pid": pid,
        "probe_url": probe_url,
        "acquired_at": acquired_at,
        "pid_alive": alive,
        "healthz": healthz,
    }


def _supervisor_report(project_root: Path) -> dict[str, Any]:
    """Aggregate per-subsystem reports into one stable map.

    Returns ``{"subsystems": {name: report, ...}}`` so the JSON output
    can grow additional supervisor-level keys (e.g. an ``active_run``
    section once W12 lands) without breaking the existing
    ``payload["supervisor"]["subsystems"]["mcp"]`` access path.
    """
    return {
        "subsystems": {
            name: _supervisor_subsystem_report(name, project_root=project_root)
            for name in _SUPERVISOR_SUBSYSTEMS
        }
    }


def doctor(
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Print a diagnostic report for the project DB and supervisor.

    Opens the project (running migrations so the report describes a
    fully-current schema), assembles the DB report, then layers on the
    supervisor section (port assignments, PID state, health probes for
    each managed subsystem) and prints the lot via
    :func:`json_or_pretty`.

    The supervisor section is computed against the *current working
    directory* (matching how the supervisor itself resolves its
    ``project_root`` default). An operator running ``ctxr-fsm doctor``
    from a different checkout will see that checkout's ``.ctxr-fsm/``
    state, which is the answer they almost certainly wanted.
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

    # The supervisor's lifecycle state lives under
    # ``<cwd>/.ctxr-fsm/`` — the same root the supervisor uses when no
    # explicit ``project_root`` is passed. Resolving here (rather than
    # relative to ``db_path``) means a developer who points ``--db`` at
    # a sibling checkout still sees the *local* supervisor state, which
    # is the answer they almost certainly wanted.
    supervisor_root = Path.cwd().resolve()
    supervisor = _supervisor_report(supervisor_root)

    report: dict[str, Any] = {
        "db_path": str(db_path),
        "file_size_bytes": file_size,
        "sqlite_version": pragmas.get("sqlite_version"),
        "pragmas": {k: v for k, v in pragmas.items() if k != "sqlite_version"},
        "alembic_revision": revision,
        "tables": row_counts,
        "journal_txns": journal,
        "locks": {"count": locks},
        "supervisor": supervisor,
    }
    json_or_pretty(report, json_mode)
