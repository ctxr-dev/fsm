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

from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from ctxr.fsm.api._deps import ProjectDep, require_auth
from ctxr.fsm.api._pagination import (
    Page,
    PageParams,
    make_page_params,
    paginate_sa_select,
)
from ctxr.fsm.api._paths import looks_like_filesystem_db_path, project_root_and_relative
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

# ── Per-route pagination factories ──────────────────────────────────
# Each list endpoint binds its own ``PageParams`` factory at module
# scope. The factory captures the default + allowed sort keys so the
# OpenAPI schema documents them and unknown keys 422 at the edge.
JournalTxnsPageParams = make_page_params(
    default_sort="started_at_desc",
    allowed_sorts=("started_at_desc", "started_at_asc"),
)

LocksPageParams = make_page_params(
    default_sort="acquired_at_desc",
    allowed_sorts=("acquired_at_desc", "acquired_at_asc"),
)

ToolCallsPageParams = make_page_params(
    default_sort="created_at_desc",
    allowed_sorts=("created_at_desc", "created_at_asc"),
)

CommitSignaturesPageParams = make_page_params(
    default_sort="created_at_desc",
    allowed_sorts=("created_at_desc", "created_at_asc"),
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

    Mostly mirrors the dict produced by
    :func:`ctxr.fsm.cli.doctor_cmd.doctor`, but the HTTP surface is
    a strict superset: it additionally carries ``project_root`` +
    ``db_path_relative`` (W22) so UI consumers can display portable,
    project-relative paths without re-deriving them. A follow-up
    will plumb the same fields into the CLI's ``--json`` output so
    the two surfaces converge; today, an operator reading the CLI
    JSON will see the absolute ``db_path`` only. Field-by-field
    commentary lives on each attribute below.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    db_path: str = Field(
        ...,
        description=(
            "Absolute filesystem path of the open DB file when"
            " filesystem-backed (normalised via :meth:`Path.resolve`)."
            " For non-file backends (``:memory:``, ``file:``-URI"
            " variants), this is the raw ``engine.url.database``"
            " segment — ``:memory:`` or ``file:test.db`` —"
            " surfacing what SQLAlchemy resolved from the URL. When"
            " the URL has no ``database`` component (``sqlite://``),"
            " falls back to the rendered ``str(engine.url)``. Clients"
            " distinguish a real path from a sentinel by checking"
            " ``project_root`` / ``db_path_relative`` for ``None``."
        ),
    )
    project_root: str | None = Field(
        None,
        description=(
            "Absolute path of the project root that hosts ``.ctxr-fsm/``."
            " Computed by walking up from the resolved DB path; falls"
            " back to the DB's parent directory when no ``.ctxr-fsm/``"
            " ancestor is found (operator passed a non-canonical"
            " ``--db``). UI surfaces use this as the anchor for"
            " portable, relative display of the DB path. ``None`` when"
            " the DB URL has no filesystem path (in-memory / non-file"
            " backends) — derivation is meaningless in that case."
        ),
    )
    db_path_relative: str | None = Field(
        None,
        description=(
            "DB path rendered relative to ``project_root``. Canonical"
            " layout: ``.ctxr-fsm/fsm.db``. UI prefers this over"
            " ``db_path`` so the displayed value stays portable across"
            " machines and committable to shared configs. ``None`` when"
            " ``project_root`` is also ``None``."
        ),
    )
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


def _select_journal_txns_page(
    session_factory: sessionmaker[Session],
    *,
    status: JournalStatus | None,
    params: PageParams,
) -> Page[JournalTxn]:
    """Return a page of journal-txn rows, optionally filtered by status.

    Default sort surfaces the freshest activity first (``started_at
    DESC, id DESC``); ``params.sort = "started_at_asc"`` inverts both
    keys so the timeline reads chronologically. Rows are projected
    through :meth:`JournalRepo._row_to_txn` — the canonical adapter
    that owns the JSON-decode + datetime-parse boundary — so the wire
    shape stays byte-equivalent with the rest of the substrate.

    The base ``select()`` is built with its ``order_by`` clause already
    applied; :func:`paginate_sa_select` then bolts on
    ``COUNT(*) OVER ()`` plus ``LIMIT/OFFSET`` in one round-trip so
    the envelope's ``total`` is consistent with the page slice even
    under concurrent writes.
    """
    from ctxr.fsm.sqlite.repos_locks_journal import JournalRepo

    if params.sort == "started_at_asc":
        order_clause = (
            JournalTxnTable.started_at.asc(),
            JournalTxnTable.id.asc(),
        )
    else:  # "started_at_desc" — the default
        order_clause = (
            JournalTxnTable.started_at.desc(),
            JournalTxnTable.id.desc(),
        )

    stmt = select(JournalTxnTable).order_by(*order_clause)
    if status is not None:
        stmt = stmt.where(JournalTxnTable.status == status.value)

    def _factory(mapping: Any) -> JournalTxn:
        # ``conn.execute()`` over an ORM-targeted ``select()`` yields
        # Core rows whose ``_mapping`` is column-name keyed. Wrapping
        # in :class:`SimpleNamespace` gives the existing private
        # ``_row_to_txn`` projector the attribute-style row it expects
        # without forcing us to duplicate its decode logic here.
        shim = SimpleNamespace(**{k: v for k, v in mapping.items() if k != "__page_total__"})
        return JournalRepo._row_to_txn(cast(JournalTxnTable, shim))

    with session_factory() as session:
        return paginate_sa_select(
            session.connection(),
            stmt,
            params=params,
            row_factory=_factory,
        )


def _select_locks_page(
    session_factory: sessionmaker[Session],
    *,
    params: PageParams,
) -> Page[Lock]:
    """Return a page of lock rows, freshest acquisition first by default.

    The locks table is intentionally tiny (one row per actively-locked
    run) so a single page typically holds every row; the envelope is
    still useful so the UI gets a consistent shape across list
    endpoints. ``params.sort = "acquired_at_asc"`` reverses the order
    for "oldest lock first" diagnostics.
    """
    from ctxr.fsm.sqlite.repos_locks_journal import LocksRepo

    if params.sort == "acquired_at_asc":
        order_col = LockTable.acquired_at.asc()
    else:  # "acquired_at_desc" — the default
        order_col = LockTable.acquired_at.desc()

    stmt = select(LockTable).order_by(order_col)

    def _factory(mapping: Any) -> Lock:
        shim = SimpleNamespace(**{k: v for k, v in mapping.items() if k != "__page_total__"})
        return LocksRepo._row_to_lock(cast(LockTable, shim))

    with session_factory() as session:
        return paginate_sa_select(
            session.connection(),
            stmt,
            params=params,
            row_factory=_factory,
        )


def _select_tool_calls_page(
    session_factory: sessionmaker[Session],
    *,
    run_id: str,
    params: PageParams,
) -> Page[ToolCall]:
    """Return a page of tool calls for ``run_id``.

    Delegates to :meth:`ToolCallsRepo.by_run_paged`, which runs a
    single SQL statement (filtered WHERE + ``COUNT(*) OVER ()``) so
    ``Page.total`` reflects the true population count even when a
    run has more tool calls than the page-size cap. The pre-W22b2-
    iter draft used the cap-bounded :meth:`by_run` then sliced in
    memory; that path silently truncated ``total`` to
    ``MAX_PAGE_SIZE`` for any run with more tool calls — making the
    envelope's count lie, and rendering pages past the cap
    unreachable.
    """
    from ctxr.fsm.sqlite.repos_enforcement import ToolCallsRepo

    repo = ToolCallsRepo()
    with session_factory() as session:
        items, total = repo.by_run_paged(
            session,
            run_id,
            sort_axis=params.sort,
            offset=params.offset,
            limit=params.page_size,
        )
    return Page[ToolCall].from_items_and_total(items, total, params=params)


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


def _select_commit_signatures_page(
    session_factory: sessionmaker[Session],
    *,
    run_id: str,
    params: PageParams,
) -> Page[CommitSignatureRecord]:
    """Return a page of commit-signature rows for ``run_id``, newest first by default.

    The :class:`CommitSignaturesRepo` only exposes ``last_for_run`` and
    ``record``; the admin view wants the full timeline, so we issue
    the query directly here and project through the same private
    adapter the repo uses. ``params.sort = "created_at_asc"`` flips
    the order for chronological reads.
    """
    from ctxr.fsm.sqlite.repos_enforcement import _commit_signature_from_row

    if params.sort == "created_at_asc":
        order_col = CommitSignatureTable.created_at.asc()
    else:  # "created_at_desc" — the default
        order_col = CommitSignatureTable.created_at.desc()

    stmt = (
        select(CommitSignatureTable)
        .where(CommitSignatureTable.run_id == run_id)
        .order_by(order_col)
    )

    def _factory(mapping: Any) -> CommitSignatureRecord:
        shim = SimpleNamespace(**{k: v for k, v in mapping.items() if k != "__page_total__"})
        return _commit_signature_from_row(cast(CommitSignatureTable, shim))

    with session_factory() as session:
        return paginate_sa_select(
            session.connection(),
            stmt,
            params=params,
            row_factory=_factory,
        )


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
    # ``engine.url.database`` is the filesystem path for SQLite URLs.
    # When it's missing (in-memory backend, non-file URL) we still
    # surface a ``db_path`` derived from the rendered URL for
    # legibility, but skip the portable-path derivation — calling
    # project_root_and_relative on ``sqlite://`` would produce
    # misleading values.
    db_url_database = engine.url.database
    db_path = db_url_database or str(engine.url)
    pragmas = detect_journal_state(engine)
    tables = _list_user_tables(engine)
    row_counts = {name: _count_table_rows(engine, name) for name in tables}
    revision = _alembic_revision(engine)
    journal_breakdown = _journal_status_breakdown(session_factory)
    lock_count = _lock_table_count(session_factory)

    project_root_str: str | None = None
    db_path_relative: str | None = None
    # `looks_like_filesystem_db_path` filters out :memory: and the URI
    # in-memory variant (and the empty string), so we never derive a
    # fake project_root for a non-file backend.
    if looks_like_filesystem_db_path(db_url_database):
        project_root_path, db_path_relative = project_root_and_relative(db_url_database)
        project_root_str = str(project_root_path)
        # Normalise db_path to absolute (matches the field's doc
        # contract). When the engine was opened with a relative path
        # the raw url.database would be relative too — `Path.resolve`
        # plus the canonical layout walk-up keeps the wire shape
        # uniform across hosts.
        db_path = str(Path(db_url_database).resolve())
    return DoctorReport(
        db_path=db_path,
        project_root=project_root_str,
        db_path_relative=db_path_relative,
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
    response_model=Page[JournalTxn],
    summary="List journal transactions across every run (admin).",
)
async def list_journal_txns(
    project: ProjectDep,
    params: Annotated[PageParams, Depends(JournalTxnsPageParams)],
    status: Annotated[
        JournalStatus | None,
        Query(
            description=(
                "Optional status filter. One of ``pending``, "
                "``ready_to_finalise``, ``finalised``."
            ),
        ),
    ] = None,
) -> Page[JournalTxn]:
    """Return journal-txn rows for forensic inspection.

    The admin view is global (no ``run_id`` filter required) because
    the most common operator question is "which runs have stuck
    transactions right now?" — a query that needs to see every row.
    """
    return await run_in_threadpool(
        _select_journal_txns_page,
        project.session_factory,
        status=status,
        params=params,
    )


@router.get(
    "/locks",
    response_model=Page[Lock],
    summary="List every currently-held lock row (admin).",
)
async def list_locks(
    project: ProjectDep,
    params: Annotated[PageParams, Depends(LocksPageParams)],
) -> Page[Lock]:
    """Return every row in the ``locks`` table, freshest acquisition first.

    The table is intentionally tiny (one row per actively-locked run),
    so a single page typically holds every row. The :class:`Page`
    envelope is still returned for wire-format consistency with the
    other list endpoints — an empty page (``total=0``) is the normal
    quiescent state.
    """
    return await run_in_threadpool(
        _select_locks_page,
        project.session_factory,
        params=params,
    )


@router.get(
    "/tool_calls",
    response_model=Page[ToolCall],
    summary="Tool-call audit log scoped to a run (W12 substrate).",
)
async def list_tool_calls(
    project: ProjectDep,
    params: Annotated[PageParams, Depends(ToolCallsPageParams)],
    run_id: Annotated[
        str,
        Query(
            min_length=1,
            description="Run id whose tool calls should be returned.",
        ),
    ],
) -> Page[ToolCall]:
    """Return the most recent tool calls for ``run_id``.

    ``run_id`` is required because the underlying repo only exposes a
    per-run query (off-allowlist global scans would be expensive on
    busy installations and offer little operational value — the audit
    log is run-scoped by design).
    """
    return await run_in_threadpool(
        _select_tool_calls_page,
        project.session_factory,
        run_id=run_id,
        params=params,
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
    response_model=Page[CommitSignatureRecord],
    summary="Commit-signature timeline for a run (W12 substrate).",
)
async def list_commit_signatures(
    project: ProjectDep,
    params: Annotated[PageParams, Depends(CommitSignaturesPageParams)],
    run_id: Annotated[
        str,
        Query(
            min_length=1,
            description="Run id whose commit signatures should be returned.",
        ),
    ],
) -> Page[CommitSignatureRecord]:
    """Return every commit-signature envelope captured for ``run_id``.

    Sorted newest-first by default so the most recent commit is
    element zero — matches the shape
    :meth:`CommitSignaturesRepo.last_for_run` returns and the order
    operators expect when reading a timeline. ``?sort=created_at_asc``
    flips the order for chronological reads.
    """
    return await run_in_threadpool(
        _select_commit_signatures_page,
        project.session_factory,
        run_id=run_id,
        params=params,
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
