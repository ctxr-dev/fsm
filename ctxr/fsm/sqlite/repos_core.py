"""Repository layer for the ctxr.fsm SQLite lifecycle tables.

This module is the *only* sanctioned surface for reading and writing the
lifecycle / event tables declared in :mod:`ctxr.fsm.sqlite.models_core` and
:mod:`ctxr.fsm.sqlite.models_events`. Higher layers (engine, MCP server,
CLI) consume the Pydantic value objects defined here and MUST never reach
for the SQLModel table classes directly — that would couple them to the
storage schema and defeat the whole point of having a repository boundary.

Design contract
---------------
* Every repository method accepts a ``sqlalchemy.orm.Session`` so the
  ``@atomic`` decorator from W2-Transactions can wrap calls in
  ``Session.begin()`` blocks. We deliberately do NOT open or commit
  sessions here — that is the caller's responsibility.
* Public method signatures use Pydantic value objects (``Project``,
  ``RegisteredSpec``, ``Run``, ``RunSummary``, ``StateNode``,
  ``Event``, ``RunSession``). SQLModel table classes never leak past
  the repository boundary.
* All timestamps are written as ISO-8601 UTC strings with millisecond
  precision via :func:`_iso_now_ms`. Timestamps are passed through
  unmodified on read — callers may parse with
  ``datetime.fromisoformat`` if they need a Python ``datetime``.
* All UUIDs are minted via :func:`uuid_utils.uuid7` and stored as the
  36-char hyphenated string form. UUIDv7 gives us insertion-order ==
  sort-order for cursor pagination "for free".
* JSON-bearing columns are canonicalised on write via
  :func:`_canonical_json` (``sort_keys=True``, compact separators) so
  two semantically equal payloads produce byte-identical TEXT — a
  prerequisite for content-hash stability and diff-friendly fixtures.
* No business logic. Repositories are CRUD + minimal aggregation
  queries. Anything that decides *what* to write belongs in the engine
  or the calling service.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import uuid_utils
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, over, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ctxr.fsm.core.models import FsmSpec
from ctxr.fsm.core.spec import fsm_spec_hash as _compute_spec_hash
from ctxr.fsm.sqlite.models_core import (
    FsmSpecTable,
    ProjectTable,
    RunSessionTable,
    RunTable,
    StateTable,
    TransitionTable,
)
from ctxr.fsm.sqlite.models_events import EventTable

__all__ = [
    "Event",
    # Value objects (Pydantic)
    "Project",
    # Repositories
    "ProjectsRepo",
    "RegisteredSpec",
    "Run",
    "RunSession",
    "RunSessionsRepo",
    "RunSummary",
    "RunsRepo",
    "SpecRegistered",
    "SpecsRepo",
    "StateNode",
    # Helpers
    "get_session_factory",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _uuid7_str() -> str:
    """Mint a fresh UUIDv7 as a 36-char hyphenated string.

    UUIDv7 is the project-wide identity shape because its first 48 bits
    encode the Unix epoch in milliseconds, which means a B-tree index on
    the PK stays append-friendly and a plain lexicographic sort over PKs
    is equivalent to insertion order.
    """
    return str(uuid_utils.uuid7())


def _iso_now_ms() -> str:
    """Return ``datetime.now(timezone.utc)`` as ISO-8601 with ms precision.

    We use ``isoformat(timespec='milliseconds')`` and keep the explicit
    ``+00:00`` offset rather than rewriting to ``Z``. The reason: STRICT
    TEXT columns sort lexicographically, and as long as every row uses
    the same offset suffix the ordering is correct. Other sqlite tooling
    in the project (event journal) uses ``Z``; we pick ``+00:00`` here to
    match the brief verbatim, and rely on every writer in this module
    going through this helper so the suffix stays consistent within the
    lifecycle tables.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _canonical_json(obj: Any) -> str:
    """Serialise ``obj`` to canonical JSON text.

    Canonical here means ``sort_keys=True`` plus the most compact
    separators (no extraneous whitespace). The result is byte-identical
    for any two semantically equal Python objects, which keeps the
    on-disk TEXT comparable and the spec-hash calculation stable.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _load_json(value: str | None) -> Any:
    """Parse JSON text, tolerating None / empty strings as ``None``.

    Returns ``None`` when the column was NULL or stored an empty string;
    otherwise returns whatever ``json.loads`` produces. Callers that
    expect a dict / list shape do their own assertion downstream.
    """
    if value is None or value == "":
        return None
    return json.loads(value)


# ---------------------------------------------------------------------------
# Pydantic value objects (the repo public surface)
# ---------------------------------------------------------------------------


_VO_CFG = ConfigDict(strict=True, frozen=True, extra="forbid")


class Project(BaseModel):
    """A project row exposed across the repo boundary."""

    model_config = _VO_CFG

    id: str
    slug: str
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegisteredSpec(BaseModel):
    """A versioned FSM spec row exposed across the repo boundary."""

    model_config = _VO_CFG

    id: str
    project_id: str
    slug: str
    version: int
    hash: str
    definition: dict[str, Any]
    created_at: str


class SpecRegistered(BaseModel):
    """Return value of :meth:`SpecsRepo.register`.

    ``created`` is True when the call inserted a new row (either because
    the spec was unknown to the project, or because its hash differed
    from the latest version and a bump was performed). ``created`` is
    False when the incoming spec matched the latest registered version
    byte-for-byte — in that case ``spec`` points to the existing row.
    """

    model_config = _VO_CFG

    spec: RegisteredSpec
    created: bool


class RunSession(BaseModel):
    """A run-session row exposed across the repo boundary."""

    model_config = _VO_CFG

    id: str
    run_id: str
    session_id: str
    acquired_at: str
    released_at: str | None = None
    release_reason: str | None = None


class Run(BaseModel):
    """The full run record exposed across the repo boundary."""

    model_config = _VO_CFG

    id: str
    project_id: str
    fsm_spec_id: str
    fsm_spec_hash: str
    status: str
    current_state: str | None = None
    next_state: str | None = None
    verdict: str | None = None
    started_at: str
    ended_at: str | None = None
    last_update_at: str
    paused_at: str | None = None
    pause_reason: str | None = None
    parent_run_id: str | None = None
    resume_history: list[Any] = Field(default_factory=list)
    args: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    transitions_count: int = 0


class RunSummary(BaseModel):
    """A trimmed-down run row used for list endpoints.

    The summary intentionally drops the heavyweight JSON columns
    (``args``, ``metadata``, ``resume_history``) so listing 20+ runs
    stays cheap. Callers that need the full picture call
    :meth:`RunsRepo.get`.
    """

    model_config = _VO_CFG

    id: str
    project_id: str
    fsm_spec_id: str
    status: str
    current_state: str | None = None
    next_state: str | None = None
    verdict: str | None = None
    started_at: str
    ended_at: str | None = None
    last_update_at: str
    transitions_count: int = 0


class StateNode(BaseModel):
    """A node in the run's state-entry tree returned by :meth:`RunsRepo.state_tree`.

    ``state_id`` is the FSM state name (e.g. ``"draft"``); ``entry_id``
    is the row PK of the specific entry (``states.id``) that activated
    this state. ``children`` are the downstream state entries reached
    via transitions whose ``from_state_id`` equals this node's
    ``entry_id``.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    entry_id: str
    state_id: str
    entry_seq: int
    entered_at: str
    exited_at: str | None = None
    status: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    iteration_n: int | None = None
    children: list[StateNode] = Field(default_factory=list)


# StateNode is self-referential; finalise the forward reference now that
# the class is fully constructed.
StateNode.model_rebuild()


class Event(BaseModel):
    """An event row exposed across the repo boundary."""

    model_config = _VO_CFG

    id: str
    run_id: str | None
    kind: str
    producer_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    seq: int | None = None


# ---------------------------------------------------------------------------
# Row → value-object converters
# ---------------------------------------------------------------------------


def _project_from_row(row: ProjectTable) -> Project:
    return Project(
        id=row.id,
        slug=row.slug,
        created_at=row.created_at,
        metadata=_load_json(row.metadata_json) or {},
    )


def _spec_from_row(row: FsmSpecTable) -> RegisteredSpec:
    definition = _load_json(row.definition_json)
    if not isinstance(definition, dict):
        # Should never happen — the column is NOT NULL and we always write
        # a JSON object — but guard against legacy / hand-edited rows.
        definition = {}
    return RegisteredSpec(
        id=row.id,
        project_id=row.project_id,
        slug=row.slug,
        version=row.version,
        hash=row.hash,
        definition=definition,
        created_at=row.created_at,
    )


def _run_from_row(row: RunTable) -> Run:
    return Run(
        id=row.id,
        project_id=row.project_id,
        fsm_spec_id=row.fsm_spec_id,
        fsm_spec_hash=row.fsm_spec_hash,
        status=row.status,
        current_state=row.current_state,
        next_state=row.next_state,
        verdict=row.verdict,
        started_at=row.started_at,
        ended_at=row.ended_at,
        last_update_at=row.last_update_at,
        paused_at=row.paused_at,
        pause_reason=row.pause_reason,
        parent_run_id=row.parent_run_id,
        resume_history=_load_json(row.resume_history_json) or [],
        args=_load_json(row.args_json) or {},
        metadata=_load_json(row.metadata_json) or {},
        transitions_count=row.transitions_count,
    )


def _run_summary_from_row(row: RunTable) -> RunSummary:
    return RunSummary(
        id=row.id,
        project_id=row.project_id,
        fsm_spec_id=row.fsm_spec_id,
        status=row.status,
        current_state=row.current_state,
        next_state=row.next_state,
        verdict=row.verdict,
        started_at=row.started_at,
        ended_at=row.ended_at,
        last_update_at=row.last_update_at,
        transitions_count=row.transitions_count,
    )


def _run_session_from_row(row: RunSessionTable) -> RunSession:
    return RunSession(
        id=row.id,
        run_id=row.run_id,
        session_id=row.session_id,
        acquired_at=row.acquired_at,
        released_at=row.released_at,
        release_reason=row.release_reason,
    )


def _event_from_row(row: EventTable) -> Event:
    payload = _load_json(row.payload_json) or {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return Event(
        id=row.id,
        run_id=row.run_id,
        kind=row.kind,
        producer_id=row.producer_id,
        payload=payload,
        created_at=row.created_at,
        seq=row.seq,
    )


# ---------------------------------------------------------------------------
# Public session-factory helper
# ---------------------------------------------------------------------------


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a ``sessionmaker`` bound to ``engine``.

    The ``@atomic`` decorator from W2-Transactions calls this once at
    composition time and reuses the result across requests. We set
    ``expire_on_commit=False`` so value objects built from rows survive
    a commit boundary without triggering surprise reloads.
    """
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


# ---------------------------------------------------------------------------
# ProjectsRepo
# ---------------------------------------------------------------------------


class ProjectsRepo:
    """CRUD over the ``projects`` table."""

    @staticmethod
    def create(
        session: Session,
        slug: str,
        metadata: dict[str, Any] | None = None,
    ) -> Project:
        """Insert a new project and return its value-object form.

        ``slug`` is the natural identifier; it is UNIQUE at the DB
        level, so a duplicate slug raises ``IntegrityError`` from the
        wrapping ``@atomic`` boundary. ``metadata`` defaults to an
        empty dict and is canonicalised on write.
        """
        row = ProjectTable(
            id=_uuid7_str(),
            slug=slug,
            created_at=_iso_now_ms(),
            metadata_json=_canonical_json(metadata or {}),
        )
        session.add(row)
        session.flush()
        return _project_from_row(row)

    @staticmethod
    def get(session: Session, id: str) -> Project | None:
        """Return the project with the given PK, or ``None``."""
        row = session.get(ProjectTable, id)
        return _project_from_row(row) if row is not None else None

    @staticmethod
    def get_by_slug(session: Session, slug: str) -> Project | None:
        """Return the project with the given slug, or ``None``."""
        stmt = select(ProjectTable).where(ProjectTable.slug == slug)
        row = session.execute(stmt).scalar_one_or_none()
        return _project_from_row(row) if row is not None else None

    @staticmethod
    def list(session: Session) -> list[Project]:
        """Return every project, ordered by ``created_at`` ascending.

        Insertion order is preserved because we mint UUIDv7 PKs, but we
        sort on ``created_at`` to keep the contract explicit and
        independent of the PK shape.
        """
        stmt = select(ProjectTable).order_by(ProjectTable.created_at.asc())
        rows = session.execute(stmt).scalars().all()
        return [_project_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# SpecsRepo
# ---------------------------------------------------------------------------


class SpecsRepo:
    """CRUD over the ``fsm_specs`` table."""

    @staticmethod
    def register(
        session: Session,
        spec: FsmSpec,
        project_id: str,
    ) -> SpecRegistered:
        """Register ``spec`` under ``project_id`` with content-hash dedup.

        The flow is:

        1. Compute the canonical hash via :func:`fsm_spec_hash`.
        2. Look up the *latest* registered version for
           ``(project_id, spec.id)``.
        3. If the latest row's hash equals the new hash, return it
           unchanged with ``created=False``. Idempotency is the whole
           point: re-registering an unchanged spec must be a no-op.
        4. Otherwise insert a new row with ``version = latest.version
           + 1`` (or 1 when there is no prior version).

        Note that the spec is canonicalised via
        ``spec.model_dump(mode='json', by_alias=True, exclude_none=True)``
        so the on-disk JSON matches the bytes used to compute the hash —
        a prerequisite for "same hash ⇒ same bytes" reproducibility.
        """
        new_hash = _compute_spec_hash(spec)
        slug = spec.id

        stmt = (
            select(FsmSpecTable)
            .where(
                FsmSpecTable.project_id == project_id,
                FsmSpecTable.slug == slug,
            )
            .order_by(FsmSpecTable.version.desc())
            .limit(1)
        )
        latest = session.execute(stmt).scalar_one_or_none()

        if latest is not None and latest.hash == new_hash:
            return SpecRegistered(spec=_spec_from_row(latest), created=False)

        next_version = (latest.version + 1) if latest is not None else 1
        definition = spec.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )

        row = FsmSpecTable(
            id=_uuid7_str(),
            project_id=project_id,
            slug=slug,
            version=next_version,
            hash=new_hash,
            definition_json=_canonical_json(definition),
            created_at=_iso_now_ms(),
        )
        session.add(row)
        session.flush()
        return SpecRegistered(spec=_spec_from_row(row), created=True)

    @staticmethod
    def get(session: Session, spec_id: str) -> RegisteredSpec | None:
        """Return the spec row with the given PK, or ``None``."""
        row = session.get(FsmSpecTable, spec_id)
        return _spec_from_row(row) if row is not None else None

    @staticmethod
    def list_versions(
        session: Session,
        project_id: str,
        slug: str,
    ) -> list[RegisteredSpec]:
        """Return every registered version for ``(project_id, slug)``.

        Sorted by ``version`` ascending so the caller sees the full
        history in chronological order — useful for "what did this spec
        look like at v3?" style queries.
        """
        stmt = (
            select(FsmSpecTable)
            .where(
                FsmSpecTable.project_id == project_id,
                FsmSpecTable.slug == slug,
            )
            .order_by(FsmSpecTable.version.asc())
        )
        rows = session.execute(stmt).scalars().all()
        return [_spec_from_row(row) for row in rows]

    @staticmethod
    def get_latest_by_slug(
        session: Session,
        slug: str,
        project_id: str | None = None,
    ) -> RegisteredSpec | None:
        """Return the highest-version registered spec for ``slug``.

        Used by the MCP / API surface to let consumers reference a spec
        by its human-readable slug (``"code-reviewer"``) instead of its
        UUID primary key. The MCP ``fsm.start_run`` tool accepts either
        shape because SKILL.md authors don't know the UUID at write
        time.

        When ``project_id`` is omitted, the lookup is global — there's
        a single registered spec per slug in single-project deployments,
        which is the dominant case today. When ``project_id`` is
        supplied, the lookup is scoped to that project (forward-compat
        with the multi-project schema).

        Returns ``None`` when no row matches.
        """
        stmt = select(FsmSpecTable).where(FsmSpecTable.slug == slug)
        if project_id is not None:
            stmt = stmt.where(FsmSpecTable.project_id == project_id)
        stmt = stmt.order_by(FsmSpecTable.version.desc()).limit(1)
        row = session.execute(stmt).scalar_one_or_none()
        return _spec_from_row(row) if row is not None else None


# ---------------------------------------------------------------------------
# RunsRepo
# ---------------------------------------------------------------------------


# Status sets used by the convenience accessors. We pin them here rather
# than importing :class:`RunStatus` purely for value lookups so the repo
# layer stays decoupled from changes to the enum's *member* set — when
# new statuses appear, this list is the single place to update.
_INCOMPLETE_STATUSES: tuple[str, ...] = (
    "in_progress",
    "paused",
    "faulted",
    "drift_paused",
)
_RESUMABLE_STATUSES: tuple[str, ...] = (
    "paused",
    "faulted",
    "drift_paused",
)


class RunsRepo:
    """CRUD + aggregation queries over the ``runs`` table."""

    @staticmethod
    def create(
        session: Session,
        project_id: str,
        spec_id: str,
        args: dict[str, Any],
        fsm_spec_hash: str,
    ) -> Run:
        """Insert a new run in the ``in_progress`` state.

        ``fsm_spec_hash`` is the hash observed at run start — the
        "hash lock" the engine consults to detect mid-run spec drift on
        resume. We accept it as an explicit parameter (rather than
        re-computing from ``spec_id``) so the engine can record exactly
        what it loaded, even if the underlying spec row is later
        edited by an operator.
        """
        now = _iso_now_ms()
        row = RunTable(
            id=_uuid7_str(),
            project_id=project_id,
            fsm_spec_id=spec_id,
            fsm_spec_hash=fsm_spec_hash,
            status="in_progress",
            current_state=None,
            next_state=None,
            verdict=None,
            started_at=now,
            ended_at=None,
            last_update_at=now,
            paused_at=None,
            pause_reason=None,
            parent_run_id=None,
            resume_history_json=_canonical_json([]),
            args_json=_canonical_json(args or {}),
            metadata_json=_canonical_json({}),
            transitions_count=0,
        )
        session.add(row)
        session.flush()
        return _run_from_row(row)

    @staticmethod
    def get(session: Session, run_id: str) -> Run | None:
        """Return the run with the given PK, or ``None``."""
        row = session.get(RunTable, run_id)
        return _run_from_row(row) if row is not None else None

    # ------------------------------------------------------------------
    # W22b2 paginated variants — native SQL pagination with a window-
    # function count, so :class:`Page.total` is honest even on databases
    # with millions of runs. The non-paginated ``latest`` /
    # ``incomplete`` / ``resumable`` / ``by_status`` methods below are
    # kept for callers (CLI, engine, tests) that already operate on the
    # full result set and don't want a slice. Route handlers should
    # call :meth:`list_paged` instead.
    # ------------------------------------------------------------------

    @staticmethod
    def list_paged(
        session: Session,
        *,
        status_filter: str | None,
        sort_axis: str,
        offset: int,
        limit: int,
    ) -> tuple[list[RunSummary], int]:
        """Return a paginated, sorted slice + total count.

        ``status_filter`` selects the WHERE-clause family:

        * ``None`` -> no filter ("latest" semantics, every run).
        * ``"incomplete"`` -> ``status IN _INCOMPLETE_STATUSES``.
        * ``"resumable"`` -> ``status IN _RESUMABLE_STATUSES``.
        * any other string -> exact ``status = value`` (matches the
          previous ``by_status`` behaviour).

        ``sort_axis`` selects ORDER BY:

        * ``"last_update_at_desc"`` (the historical default ordering).
        * ``"started_at_desc"`` / ``"started_at_asc"``.

        ``total`` is computed in the SAME round-trip via
        ``COUNT(*) OVER ()`` so concurrent writes cannot decorrelate
        the row slice from the population count (which would happen
        if we issued two separate queries).

        Returns ``(items, total)``. ``items`` is the page slice;
        ``total`` is the FULL population size matching ``status_filter``
        (pre-slice).
        """
        if limit < 1:
            return [], 0

        stmt = select(RunTable)
        if status_filter is None:
            pass
        elif status_filter == "incomplete":
            stmt = stmt.where(RunTable.status.in_(_INCOMPLETE_STATUSES))
        elif status_filter == "resumable":
            stmt = stmt.where(RunTable.status.in_(_RESUMABLE_STATUSES))
        else:
            stmt = stmt.where(RunTable.status == status_filter)

        if sort_axis == "started_at_desc":
            order_col = RunTable.started_at.desc()
        elif sort_axis == "started_at_asc":
            order_col = RunTable.started_at.asc()
        else:
            order_col = RunTable.last_update_at.desc()
        stmt = stmt.order_by(order_col)

        # Append the window-function count as a second selected column
        # so the page slice and the population total fall out of one
        # SQL statement. The label is internal — never exposed past this
        # method.
        count_col = over(func.count()).label("__page_total__")
        paged = stmt.add_columns(count_col).offset(offset).limit(limit)

        rows = list(session.execute(paged))
        if not rows:
            # Empty page either because the filter matches zero rows
            # OR the offset is past the last page. Recover the true
            # total via a separate COUNT so ``Page.total`` stays
            # honest in that off-the-end case (without the recovery
            # query, the caller would see total=0 even when many rows
            # exist before the requested offset).
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = int(session.execute(count_stmt).scalar_one())
            return [], total

        total = int(rows[0]._mapping["__page_total__"])
        items = [_run_summary_from_row(row[0]) for row in rows]
        return items, total

    @staticmethod
    def events_paged(
        session: Session,
        run_id: str,
        *,
        since_seq: int | None,
        kinds: list[str] | None,
        sort_axis: str,
        offset: int,
        limit: int,
    ) -> tuple[list[Event], int]:
        """Paginated per-run event slice + true total.

        Mirrors :meth:`events` (the iterator-based variant) but with
        native SQL pagination + a window-function count, so callers
        don't have to materialise the entire journal to serve a single
        page. Filters (``since_seq``, ``kinds``) are applied in the
        WHERE clause and counted into ``total``.

        ``sort_axis`` accepts ``"seq_asc"`` (the canonical chronological
        replay order) or ``"seq_desc"`` (most-recent event first, used
        when an operator scrolls a long journal from the tail backwards).
        """
        if limit < 1:
            return [], 0

        stmt = select(EventTable).where(EventTable.run_id == run_id)
        if since_seq is not None:
            stmt = stmt.where(EventTable.seq > since_seq)
        if kinds is not None:
            stmt = stmt.where(EventTable.kind.in_(kinds))

        if sort_axis == "seq_desc":
            stmt = stmt.order_by(EventTable.seq.desc(), EventTable.created_at.desc())
        else:
            stmt = stmt.order_by(EventTable.seq.asc(), EventTable.created_at.asc())

        count_col = over(func.count()).label("__page_total__")
        paged = stmt.add_columns(count_col).offset(offset).limit(limit)

        rows = list(session.execute(paged))
        if not rows:
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = int(session.execute(count_stmt).scalar_one())
            return [], total

        total = int(rows[0]._mapping["__page_total__"])
        items = [_event_from_row(row[0]) for row in rows]
        return items, total

    @staticmethod
    def latest(session: Session, limit: int = 20) -> list[RunSummary]:
        """Return the most recently updated runs, newest first.

        Sorted by ``last_update_at DESC`` — the "what is happening right
        now" query the CLI and dashboard hit first. The composite index
        ``idx_runs_status_last_update`` is sized for this access
        pattern.
        """
        stmt = (
            select(RunTable)
            .order_by(RunTable.last_update_at.desc())
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        return [_run_summary_from_row(row) for row in rows]

    @staticmethod
    def incomplete(session: Session) -> list[RunSummary]:
        """Return every run not in a terminal state.

        "Incomplete" means status is one of ``in_progress``, ``paused``,
        ``faulted``, ``drift_paused`` — anything that could still
        transition further. Sorted by ``last_update_at DESC`` so the
        caller sees the freshest active work first.
        """
        stmt = (
            select(RunTable)
            .where(RunTable.status.in_(_INCOMPLETE_STATUSES))
            .order_by(RunTable.last_update_at.desc())
        )
        rows = session.execute(stmt).scalars().all()
        return [_run_summary_from_row(row) for row in rows]

    @staticmethod
    def resumable(session: Session) -> list[RunSummary]:
        """Return runs that the engine can resume.

        Resumable is a strict subset of incomplete — only ``paused``,
        ``faulted``, and ``drift_paused`` runs qualify. ``in_progress``
        runs are excluded because a worker is presumed to still hold
        the lock.
        """
        stmt = (
            select(RunTable)
            .where(RunTable.status.in_(_RESUMABLE_STATUSES))
            .order_by(RunTable.last_update_at.desc())
        )
        rows = session.execute(stmt).scalars().all()
        return [_run_summary_from_row(row) for row in rows]

    @staticmethod
    def by_status(session: Session, status: str) -> list[RunSummary]:
        """Return every run with the given status, freshest first."""
        stmt = (
            select(RunTable)
            .where(RunTable.status == status)
            .order_by(RunTable.last_update_at.desc())
        )
        rows = session.execute(stmt).scalars().all()
        return [_run_summary_from_row(row) for row in rows]

    @staticmethod
    def by_session(session: Session, session_id: str) -> list[RunSummary]:
        """Return every run that was ever bound to ``session_id``.

        We join via the ``run_sessions`` table because a session may
        bind a run multiple times. ``DISTINCT`` on ``runs.id`` collapses
        those repeats so the caller sees each run at most once.
        """
        stmt = (
            select(RunTable)
            .join(RunSessionTable, RunSessionTable.run_id == RunTable.id)
            .where(RunSessionTable.session_id == session_id)
            .order_by(RunTable.last_update_at.desc())
            .distinct()
        )
        rows = session.execute(stmt).scalars().all()
        return [_run_summary_from_row(row) for row in rows]

    @staticmethod
    def by_project(session: Session, project_id: str) -> list[RunSummary]:
        """Return every run for ``project_id``, freshest first.

        Backed by the ``idx_runs_project_started`` composite index;
        ordering on ``last_update_at`` here means we re-sort outside the
        index, which is acceptable at typical per-project run counts.
        """
        stmt = (
            select(RunTable)
            .where(RunTable.project_id == project_id)
            .order_by(RunTable.last_update_at.desc())
        )
        rows = session.execute(stmt).scalars().all()
        return [_run_summary_from_row(row) for row in rows]

    @staticmethod
    def aborted(session: Session) -> list[RunSummary]:
        """Shortcut for ``by_status('aborted')``."""
        return RunsRepo.by_status(session, "aborted")

    @staticmethod
    def failed(session: Session) -> list[RunSummary]:
        """Shortcut for runs that ended in failure.

        "Failed" here means ``faulted`` — the terminal-fault status.
        ``drift_paused`` is reserved for runs the engine *paused* due
        to drift and is therefore NOT included in this view.
        """
        return RunsRepo.by_status(session, "faulted")

    @staticmethod
    def completed(session: Session) -> list[RunSummary]:
        """Shortcut for ``by_status('completed')``."""
        return RunsRepo.by_status(session, "completed")

    @staticmethod
    def state_tree(session: Session, run_id: str) -> StateNode | None:
        """Return the run's state-entry tree.

        Edges are reconstructed from the ``transitions`` table: for each
        transition row we know the source state entry (``from_state_id``
        is a FK into ``states.id``) and the destination FSM state name
        (``to_state_id`` is the *name*, not a row PK). We resolve each
        destination to the *earliest unattached* state entry with a
        matching name within the same run — that is, the entry whose
        ``entry_seq`` is strictly greater than the source's seq and is
        not already a child of another transition.

        Returns the root node (the first state activation for this run,
        i.e. ``entry_seq = 1``), or ``None`` when the run has no
        recorded state entries yet.
        """
        # Pull every state entry once and index by id + by name.
        state_stmt = (
            select(StateTable)
            .where(StateTable.run_id == run_id)
            .order_by(StateTable.entry_seq.asc())
        )
        state_rows = list(session.execute(state_stmt).scalars().all())
        if not state_rows:
            return None

        by_id: dict[str, StateTable] = {row.id: row for row in state_rows}

        # Pull transitions in decision order so we attach children in the
        # order the engine took them.
        trans_stmt = (
            select(TransitionTable)
            .where(TransitionTable.run_id == run_id)
            .order_by(TransitionTable.decided_at.asc())
        )
        trans_rows = list(session.execute(trans_stmt).scalars().all())

        # Build a per-source-entry adjacency list of state-entry rows.
        # The destination resolution rule: pick the earliest entry whose
        # name matches ``to_state_id`` and whose ``entry_seq`` strictly
        # exceeds the source's seq AND has not yet been claimed as a
        # child by an earlier transition. This produces a forest of state
        # activations even when a state is re-entered later in the run.
        claimed: set[str] = set()
        adjacency: dict[str, list[StateTable]] = {row.id: [] for row in state_rows}

        for trans in trans_rows:
            source = by_id.get(trans.from_state_id)
            if source is None:
                continue
            target_row: StateTable | None = None
            for candidate in state_rows:
                if candidate.id in claimed:
                    continue
                if candidate.state_id != trans.to_state_id:
                    continue
                if candidate.entry_seq <= source.entry_seq:
                    continue
                target_row = candidate
                break
            if target_row is not None:
                adjacency[source.id].append(target_row)
                claimed.add(target_row.id)

        def _to_node(row: StateTable) -> StateNode:
            return StateNode(
                entry_id=row.id,
                state_id=row.state_id,
                entry_seq=row.entry_seq,
                entered_at=row.entered_at,
                exited_at=row.exited_at,
                status=row.status,
                inputs=_load_json(row.inputs_json) or {},
                outputs=_load_json(row.outputs_json) or {},
                iteration_n=row.iteration_n,
                children=[_to_node(child) for child in adjacency[row.id]],
            )

        # The root is the first state entry (entry_seq == 1, or simply
        # the earliest entry by seq if for some reason seq doesn't start
        # at 1).
        root_row = state_rows[0]
        return _to_node(root_row)

    @staticmethod
    def events(
        session: Session,
        run_id: str,
        since_seq: int | None = None,
        kinds: list[str] | None = None,
    ) -> Iterator[Event]:
        """Yield events for ``run_id``, optionally filtered.

        ``since_seq`` is exclusive: only events with ``seq > since_seq``
        are returned. ``kinds`` is a whitelist of :class:`EventKind`
        string values; when ``None`` (the default), every kind passes.

        Returns an iterator — *not* a list — so callers tailing the
        journal can stream rows without materialising the full result
        set in memory. We use ``Session.scalars`` (which yields rows
        lazily) and wrap it in a generator to keep the Pydantic
        conversion lazy too.
        """
        stmt = select(EventTable).where(EventTable.run_id == run_id)
        if since_seq is not None:
            stmt = stmt.where(EventTable.seq > since_seq)
        if kinds is not None:
            stmt = stmt.where(EventTable.kind.in_(kinds))
        stmt = stmt.order_by(EventTable.seq.asc(), EventTable.created_at.asc())

        for row in session.execute(stmt).scalars():
            yield _event_from_row(row)

    @staticmethod
    def update_status(
        session: Session,
        run_id: str,
        status: str,
        ended_at: str | None = None,
        verdict: str | None = None,
    ) -> Run | None:
        """Mutate a run's lifecycle fields.

        Used by the engine's abort / complete / fault handlers. The
        method is deliberately permissive about ``status`` — it accepts
        any string — because the calling layer (engine) already owns
        the state-machine of allowed transitions. Validating here would
        duplicate that logic and create a second source of truth.

        ``last_update_at`` is bumped to ``now`` unconditionally; the
        caller MAY supply an explicit ``ended_at`` (for terminal
        statuses) and ``verdict``. Returns ``None`` when no row with
        ``run_id`` exists, so callers can distinguish "updated" from
        "missing".
        """
        row = session.get(RunTable, run_id)
        if row is None:
            return None
        row.status = status
        row.last_update_at = _iso_now_ms()
        if ended_at is not None:
            row.ended_at = ended_at
        if verdict is not None:
            row.verdict = verdict
        session.flush()
        return _run_from_row(row)


# ---------------------------------------------------------------------------
# RunSessionsRepo
# ---------------------------------------------------------------------------


class RunSessionsRepo:
    """CRUD over the ``run_sessions`` table."""

    @staticmethod
    def open(
        session: Session,
        run_id: str,
        session_id: str,
    ) -> RunSession:
        """Insert a new acquire row binding ``session_id`` to ``run_id``.

        A run accumulates many session rows over its lifetime — one per
        acquire/release cycle. ``released_at`` is left NULL to mean
        "currently attached"; the matching :meth:`close` call sets it.
        """
        row = RunSessionTable(
            id=_uuid7_str(),
            run_id=run_id,
            session_id=session_id,
            acquired_at=_iso_now_ms(),
            released_at=None,
            release_reason=None,
        )
        session.add(row)
        session.flush()
        return _run_session_from_row(row)

    @staticmethod
    def close(
        session: Session,
        session_id: str,
        reason: str,
    ) -> list[RunSession]:
        """Release every currently-open session matching ``session_id``.

        A single session_id may hold multiple runs concurrently (one
        worker driving several FSMs); ``close`` releases all of them
        atomically with the same reason. Returns the list of value
        objects that were just released — useful for the engine's audit
        events. Returns an empty list when nothing was open.
        """
        stmt = (
            select(RunSessionTable)
            .where(
                RunSessionTable.session_id == session_id,
                RunSessionTable.released_at.is_(None),
            )
        )
        rows = list(session.execute(stmt).scalars().all())
        if not rows:
            return []
        now = _iso_now_ms()
        for row in rows:
            row.released_at = now
            row.release_reason = reason
        session.flush()
        return [_run_session_from_row(row) for row in rows]
