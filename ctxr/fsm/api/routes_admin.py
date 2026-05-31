"""``ctxr.fsm.api.routes_admin`` — administrative / observability surface.

This router exposes the *operator-facing* read endpoints plus the
single side-effecting maintenance call (``/db/doctor``). Everything
here is auth-guarded — admin endpoints leak schema-level information
(table row counts, alembic revision, journal status counts) that a
production deployment should not expose unauthenticated.

Endpoint inventory
------------------

* ``GET /journal_txns?status=&limit=`` — paginate the journal-txn
  ledger across every run for forensic inspection.
* ``GET /locks`` — list every currently-held lock row.
* ``GET /tool_calls?run_id=&limit=`` — audit log of tool invocations
  (W12 enforcement substrate). ``run_id`` is required; the per-run
  read is the only one the underlying repo provides.
* ``GET /drift_signals?run_id=`` — list typed drift signals for a run
  plus the aggregate score the W12 aggregator would compute.
* ``GET /commit_signatures?run_id=`` — list every commit-signature
  envelope captured for a run.
* ``POST /db/doctor`` — produce the same diagnostic report the
  ``ctxr-fsm doctor`` CLI prints, packaged as JSON.

Design notes
------------

* Every endpoint is ``async def`` to satisfy the project convention
  for write/IO routes. Synchronous SQLite work is offloaded to a
  thread via :func:`starlette.concurrency.run_in_threadpool` so the
  ASGI loop stays unblocked under load.
* Pydantic response models are declared in this file (or re-exported
  from :mod:`ctxr.fsm.sqlite`) so the OpenAPI schema at ``/docs`` is
  precise. The doctor report shape mirrors the CLI's output exactly so
  a future "what does the CLI know that the API doesn't?" audit is a
  diff of two dictionaries.
* No MCP / Typer imports — this layer is HTTP-only. The doctor
  helpers in :mod:`ctxr.fsm.cli.doctor_cmd` are *not* imported because
  pulling them in would drag the CLI's Typer wiring into the API
  package; instead we re-implement the same three small SQL queries
  here, kept byte-equivalent in shape to the CLI.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from ctxr.fsm.api._deps import ProjectDep, require_auth
from ctxr.fsm.core.models import JournalStatus
from ctxr.fsm.sqlite import (
    CommitSignatureRecord,
    DriftSignal,
    JournalTxn,
    Lock,
    ToolCall,
)
from ctxr.fsm.sqlite.connection import detect_journal_state
from ctxr.fsm.sqlite.models_core import LockTable
from ctxr.fsm.sqlite.models_enforcement import (
    CommitSignatureTable,
    JournalTxnTable,
)

__all__ = [
    "DoctorReport",
    "DriftSignalsResponse",
    "router",
]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
# ``dependencies=[Depends(require_auth)]`` on the router applies the
# auth gate to every endpoint here so we never accidentally publish an
# admin route without it. Individual routes still list the dependency
# explicitly in their signatures where they need access to the request
# (none currently do), but the router-level guard is the safety net.
router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DriftSignalsResponse(BaseModel):
    """Drift signals for a run plus the aggregate weight score.

    The aggregate is the same value
    :meth:`ctxr.fsm.sqlite.DriftSignalsRepo.score_for_run` computes;
    we surface it in the same payload so a UI dashboard can render the
    list and the gauge without a second round-trip.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    run_id: str = Field(..., description="The run whose signals are reported.")
    score: float = Field(
        ...,
        description=(
            "Sum of every signal's ``weight`` field — the value the W12 "
            "drift aggregator compares against its pause threshold."
        ),
    )
    signals: list[DriftSignal] = Field(
        default_factory=list,
        description="Signals in oldest-first order (matches repo semantics).",
    )


class DoctorReport(BaseModel):
    """HTTP shape of the ``ctxr-fsm doctor`` report.

    Mirrors the dict produced by :func:`ctxr.fsm.cli.doctor_cmd.doctor`
    so an operator can move freely between the CLI and the API without
    relearning field names. Field-by-field commentary lives on each
    attribute below.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    db_path: str = Field(..., description="Filesystem path of the open DB file.")
    pragmas: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Live PRAGMA values (journal_mode, foreign_keys, sqlite_version, "
            "etc.) — the values the connect-time listener actually applied."
        ),
    )
    tables_with_row_counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Map of user-table name to ``COUNT(*)``. ``alembic_version`` is "
            "included; SQLite internal tables (``sqlite_%``) are skipped."
        ),
    )
    alembic_revision: str | None = Field(
        default=None,
        description="Current ``alembic_version.version_num`` (or ``None`` if absent).",
    )
    journal_txn_breakdown: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-status counts for the ``journal_txns`` table — keys are the "
            "three canonical statuses ``pending``, ``ready_to_finalise``, "
            "``finalised`` plus any unexpected values found in the wild."
        ),
    )
    lock_count: int = Field(
        ..., description="Number of rows currently in the ``locks`` table."
    )


# ---------------------------------------------------------------------------
# Internal helpers — synchronous SQLite work, called via run_in_threadpool
# ---------------------------------------------------------------------------
# The helpers below are intentionally plain ``def`` functions (not
# methods on a class) so each one is trivially individually wrappable
# in ``run_in_threadpool``. They operate on a session_factory or
# engine the caller hands in, which keeps them testable without a
# running FastAPI app.


_VALID_JOURNAL_STATUSES: tuple[str, ...] = (
    "pending",
    "ready_to_finalise",
    "finalised",
)


def _select_journal_txns(
    session_factory: sessionmaker[Session],
    *,
    status: JournalStatus | None,
    limit: int,
) -> list[JournalTxn]:
    """Return journal-txn rows across all runs, optionally filtered by status.

    Sorted ``started_at DESC`` so the freshest activity surfaces
    first. The ORM rows are projected through
    :meth:`JournalRepo._row_to_txn` so the API speaks the same
    Pydantic shape the repo speaks — we re-implement the projection
    inline (rather than calling the private method) to keep this
    module's dependency on the repo at the public-symbol boundary.
    """
    from ctxr.fsm.sqlite.repos_locks_journal import JournalRepo

    with session_factory() as session:
        stmt = select(JournalTxnTable).order_by(
            JournalTxnTable.started_at.desc(),
            JournalTxnTable.id.desc(),
        )
        if status is not None:
            stmt = stmt.where(JournalTxnTable.status == status.value)
        stmt = stmt.limit(limit)
        rows = session.execute(stmt).scalars().all()
        # ``_row_to_txn`` is the canonical projection — using it
        # guarantees byte-equivalence with the rest of the substrate
        # without re-implementing the JSON decode + datetime parse.
        return [JournalRepo._row_to_txn(row) for row in rows]


def _select_locks(session_factory: sessionmaker[Session]) -> list[Lock]:
    """Return every lock row currently in the table.

    The locks table is intentionally tiny (one row per held run); we
    don't paginate. Sorted by ``acquired_at DESC`` so the most-recently
    grabbed lock appears first.
    """
    from ctxr.fsm.sqlite.repos_locks_journal import LocksRepo

    with session_factory() as session:
        stmt = select(LockTable).order_by(LockTable.acquired_at.desc())
        rows = session.execute(stmt).scalars().all()
        return [LocksRepo._row_to_lock(row) for row in rows]


def _select_tool_calls(
    session_factory: sessionmaker[Session],
    *,
    run_id: str,
    limit: int,
) -> list[ToolCall]:
    """Return the most-recent ``limit`` tool calls for ``run_id``.

    Thin wrapper around :meth:`ToolCallsRepo.by_run` — kept here so the
    sync/threadpool boundary is one obvious place rather than scattered
    through the route handlers.
    """
    from ctxr.fsm.sqlite.repos_enforcement import ToolCallsRepo

    repo = ToolCallsRepo()
    with session_factory() as session:
        return repo.by_run(session, run_id, limit=limit)


def _select_drift_signals_with_score(
    session_factory: sessionmaker[Session],
    *,
    run_id: str,
) -> DriftSignalsResponse:
    """Return the signals + aggregate score for ``run_id`` in one round-trip."""
    from ctxr.fsm.sqlite.repos_enforcement import DriftSignalsRepo

    repo = DriftSignalsRepo()
    with session_factory() as session:
        signals = repo.by_run(session, run_id)
        score = repo.score_for_run(session, run_id)
    return DriftSignalsResponse(run_id=run_id, score=score, signals=signals)


def _select_commit_signatures(
    session_factory: sessionmaker[Session],
    *,
    run_id: str,
) -> list[CommitSignatureRecord]:
    """Return every commit-signature row for ``run_id``, newest first.

    The :class:`CommitSignaturesRepo` only exposes ``last_for_run`` and
    ``record``; the admin view wants the full timeline, so we issue
    the query directly here and project through the same private
    adapter the repo uses.
    """
    from ctxr.fsm.sqlite.repos_enforcement import _commit_signature_from_row

    with session_factory() as session:
        stmt = (
            select(CommitSignatureTable)
            .where(CommitSignatureTable.run_id == run_id)
            .order_by(CommitSignatureTable.created_at.desc())
        )
        rows = session.execute(stmt).scalars().all()
        return [_commit_signature_from_row(row) for row in rows]


def _list_user_tables(engine: Engine) -> list[str]:
    """Return user table names sorted alphabetically.

    Skips ``sqlite_%`` internal tables but includes ``alembic_version``
    (user-visible bookkeeping). Same contract as
    :func:`ctxr.fsm.cli.doctor_cmd._list_tables`.
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


def _count_table_rows(engine: Engine, table_name: str) -> int:
    """Return ``COUNT(*)`` for ``table_name``.

    The table name is sourced from ``sqlite_master`` (see
    :func:`_list_user_tables`) so it is not user-controlled — no
    SQL-injection vector. We still quote the identifier defensively.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar() or 0
        )


def _alembic_revision(engine: Engine) -> str | None:
    """Return ``alembic_version.version_num`` for ``engine`` or ``None``.

    A missing ``alembic_version`` table (only possible on a brand-new
    DB that has not yet been migrated) returns ``None`` rather than
    raising so the doctor report stays useful in that edge case.
    """
    with engine.connect() as conn:
        try:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        except Exception:
            return None
    return None if row is None else str(row[0])


def _journal_status_breakdown(session_factory: sessionmaker[Session]) -> dict[str, int]:
    """Return per-status counts for the ``journal_txns`` table.

    Initialised with zeros for every canonical status so a quiet DB
    returns ``{"pending": 0, "ready_to_finalise": 0, "finalised": 0}``
    rather than an empty dict (which would force every consumer to
    null-check before reading). Any unexpected statuses encountered are
    surfaced under their own key.
    """
    breakdown: dict[str, int] = dict.fromkeys(_VALID_JOURNAL_STATUSES, 0)
    with session_factory() as session:
        rows = session.execute(
            text("SELECT status, COUNT(*) FROM journal_txns GROUP BY status")
        ).all()
    for status, count in rows:
        breakdown[str(status)] = int(count)
    return breakdown


def _lock_table_count(session_factory: sessionmaker[Session]) -> int:
    """Return the row count of the ``locks`` table.

    Used by the doctor report's ``lock_count`` field — answers
    "how many runs currently hold the single-writer lock?" in one int.
    """
    with session_factory() as session:
        return int(
            session.execute(select(func.count()).select_from(LockTable)).scalar() or 0
        )


def _build_doctor_report(
    *,
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> DoctorReport:
    """Assemble the :class:`DoctorReport` against a live project handle.

    Pulled out so the route handler stays a one-liner over
    :func:`run_in_threadpool` and the tests can call this directly
    without spinning up FastAPI.
    """
    db_path = engine.url.database or str(engine.url)
    pragmas = detect_journal_state(engine)
    tables = _list_user_tables(engine)
    row_counts = {name: _count_table_rows(engine, name) for name in tables}
    revision = _alembic_revision(engine)
    journal_breakdown = _journal_status_breakdown(session_factory)
    lock_count = _lock_table_count(session_factory)

    return DoctorReport(
        db_path=db_path,
        pragmas=pragmas,
        tables_with_row_counts=row_counts,
        alembic_revision=revision,
        journal_txn_breakdown=journal_breakdown,
        lock_count=lock_count,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/journal_txns",
    response_model=list[JournalTxn],
    summary="List journal transactions across every run (admin).",
)
async def list_journal_txns(
    project: ProjectDep,
    status: Annotated[
        JournalStatus | None,
        Query(
            description=(
                "Optional status filter. One of ``pending``, "
                "``ready_to_finalise``, ``finalised``."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=1000,
            description="Maximum rows to return. Defaults to 100.",
        ),
    ] = 100,
) -> list[JournalTxn]:
    """Return journal-txn rows for forensic inspection.

    The admin view is global (no ``run_id`` filter required) because
    the most common operator question is "which runs have stuck
    transactions right now?" — a query that needs to see every row.
    """
    return await run_in_threadpool(
        _select_journal_txns,
        project.session_factory,
        status=status,
        limit=limit,
    )


@router.get(
    "/locks",
    response_model=list[Lock],
    summary="List every currently-held lock row (admin).",
)
async def list_locks(project: ProjectDep) -> list[Lock]:
    """Return every row in the ``locks`` table, freshest acquisition first.

    The table is intentionally tiny (one row per actively-locked run),
    so we do not paginate. An empty list is the normal quiescent
    state.
    """
    return await run_in_threadpool(_select_locks, project.session_factory)


@router.get(
    "/tool_calls",
    response_model=list[ToolCall],
    summary="Tool-call audit log scoped to a run (W12 substrate).",
)
async def list_tool_calls(
    project: ProjectDep,
    run_id: Annotated[
        str,
        Query(
            min_length=1,
            description="Run id whose tool calls should be returned.",
        ),
    ],
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=1000,
            description="Maximum rows to return. Defaults to 100.",
        ),
    ] = 100,
) -> list[ToolCall]:
    """Return the most recent tool calls for ``run_id``.

    ``run_id`` is required because the underlying repo only exposes a
    per-run query (off-allowlist global scans would be expensive on
    busy installations and offer little operational value — the audit
    log is run-scoped by design).
    """
    return await run_in_threadpool(
        _select_tool_calls,
        project.session_factory,
        run_id=run_id,
        limit=limit,
    )


@router.get(
    "/drift_signals",
    response_model=DriftSignalsResponse,
    summary="Drift signals + aggregate score for a run (W12 substrate).",
)
async def list_drift_signals(
    project: ProjectDep,
    run_id: Annotated[
        str,
        Query(min_length=1, description="Run id whose drift signals are reported."),
    ],
) -> DriftSignalsResponse:
    """Return every drift signal for ``run_id`` plus the summed weight.

    The aggregate score is the same value
    :meth:`ctxr.fsm.sqlite.DriftSignalsRepo.score_for_run` returns —
    we compute it server-side so the UI does not have to re-sum a
    potentially-long list client-side.
    """
    return await run_in_threadpool(
        _select_drift_signals_with_score,
        project.session_factory,
        run_id=run_id,
    )


@router.get(
    "/commit_signatures",
    response_model=list[CommitSignatureRecord],
    summary="Commit-signature timeline for a run (W12 substrate).",
)
async def list_commit_signatures(
    project: ProjectDep,
    run_id: Annotated[
        str,
        Query(
            min_length=1,
            description="Run id whose commit signatures should be returned.",
        ),
    ],
) -> list[CommitSignatureRecord]:
    """Return every commit-signature envelope captured for ``run_id``.

    Sorted newest-first so the most recent commit is element zero —
    matches the shape :meth:`CommitSignaturesRepo.last_for_run`
    returns and the order operators expect when reading a timeline.
    """
    return await run_in_threadpool(
        _select_commit_signatures,
        project.session_factory,
        run_id=run_id,
    )


@router.post(
    "/db/doctor",
    response_model=DoctorReport,
    summary="Diagnostic dump of the project DB (mirrors `ctxr-fsm doctor`).",
)
async def db_doctor(project: ProjectDep) -> DoctorReport:
    """Build and return the doctor report against the open project.

    Modelled as ``POST`` (not ``GET``) because doctor walks every user
    table to compute row counts — a non-trivial amount of read work
    that we don't want browser pre-fetchers or naive monitoring
    pollers to trigger on their own.
    """
    try:
        return await run_in_threadpool(
            _build_doctor_report,
            engine=project.engine,
            session_factory=project.session_factory,
        )
    except Exception as exc:  # pragma: no cover — defensive only
        # The doctor walks every user table; a malformed table or a
        # mid-migration DB could surface here. Surface as a 500 with
        # the exception message rather than a bare stack trace so the
        # operator can pivot to the CLI doctor with a clue.
        raise HTTPException(
            status_code=500,
            detail=f"doctor report failed: {exc}",
        ) from exc
