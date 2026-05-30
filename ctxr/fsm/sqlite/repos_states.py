"""Sub-repositories for the run state tree (W2 — SQLite persistence).

This module groups the four CRUD-shaped sub-repositories that read and write
the *per-run* tables hanging off :class:`~ctxr.fsm.sqlite.models_core.RunTable`:

* :class:`StatesRepo` — rows in ``states``: one row per state *entry*.
* :class:`TransitionsRepo` — rows in ``transitions``: one row per decided
  guard evaluation.
* :class:`WorkerArtifactsRepo` — rows in ``worker_artifacts``: captured worker
  prompts and structured responses.
* :class:`AggregatesRepo` — rows in ``aggregates``: persisted cross-state
  aggregation results.

Plus :func:`build_state_tree`, the helper :class:`~ctxr.fsm.sqlite.repos_runs`
will surface as ``RunsRepo.state_tree``.

Design rules (from the W2 brief)
--------------------------------

* **No business logic.** These are CRUD + minimal aggregation queries; the
  engine, the predicate evaluator, and the aggregator stay free of database
  awareness. Repositories format timestamps, canonicalise JSON, and translate
  rows into Pydantic value-objects — nothing more.
* **Session injection.** Every method takes a ``sqlalchemy.orm.Session`` as
  its first argument; the W2 ``@atomic`` decorator wraps the calls in a
  ``Session.begin()`` block so mutating methods never need to ``flush()`` or
  ``commit()`` themselves.
* **Timestamps.** Every datetime written is rendered through
  :func:`_now_iso_ms` (``YYYY-MM-DDTHH:MM:SS.sssZ`` shape, UTC, millisecond
  precision). The TEXT-ISO contract is the storage primitive — there is no
  ``datetime`` coercion happening at the column boundary.
* **UUIDs.** PKs and FKs are 36-char hyphenated strings; generation goes
  through :func:`_uuid7_str` so insertion order matches lexicographic order
  (UUIDv7's first 48 bits are a Unix-ms timestamp).
* **JSON canonicalisation.** Anything destined for a ``*_json`` column is
  serialised with ``json.dumps(obj, sort_keys=True, separators=(",", ":"))``
  so the bytes-on-disk are deterministic and content-hashable.
* **Return shapes.** Read methods return Pydantic ``BaseModel`` value-objects
  (declared in this module) — never raw ``SQLModel`` rows — so the SQL layer
  stays an implementation detail. List methods materialise the rows eagerly
  (they return ``list[...]``, not iterators) because their callers (engine,
  CLI, MCP server) almost always need to count or index the results.
* **Pure ``ctxr.fsm.sqlite``.** No FastAPI / MCP imports — repository
  consumers wire those layers themselves.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import uuid_utils
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ctxr.fsm.sqlite.models_core import (
    AggregateTable,
    StateTable,
    TransitionTable,
    WorkerArtifactTable,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso_ms() -> str:
    """Return the current UTC time as an ISO-8601 string with ms precision.

    Matches the canonical timestamp format the schema documents: a 24-char
    ``YYYY-MM-DDTHH:MM:SS.sss+00:00`` string. The repository layer is the
    single owner of this formatting decision; callers must NOT build the
    string themselves.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _uuid7_str() -> str:
    """Return a fresh UUIDv7 as a 36-char hyphenated string.

    UUIDv7's first 48 bits are a millisecond Unix timestamp, so lexicographic
    PK order matches insertion order — important for cursor pagination over
    ``states`` and friends.
    """
    return str(uuid_utils.uuid7())


def _canonical_json(value: Any) -> str:
    """Canonicalise ``value`` to a stable JSON string.

    Sorted keys + the most compact separators give us byte-identical output
    for any equal Python value — that's the property the content hashing in
    :mod:`ctxr.fsm.core.models` relies on, and the property the audit/event
    journal reads back when reconstructing run state.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None, default: Any) -> Any:
    """Decode ``value`` (a JSON string) returning ``default`` on ``None``.

    Encapsulates the small bit of defensive parsing that read paths need so
    each repository method body stays a one-liner.
    """
    if value is None:
        return default
    return json.loads(value)


def _coerce_predicate_result(value: Any) -> bool | None:
    """Coerce a STRICT-mode INTEGER 0/1 column back into ``bool | None``.

    SQLite STRICT mode refuses the ``BOOLEAN`` affinity, so we store the
    column as INTEGER and round-trip in code. ``None`` (NULL) means "not
    yet decided" — most commonly for ``always`` / ``otherwise`` transitions
    whose evaluation was forced rather than predicate-based.
    """
    if value is None:
        return None
    return bool(value)


# ---------------------------------------------------------------------------
# Pydantic value objects (the public read surface)
# ---------------------------------------------------------------------------

_VO_CFG = ConfigDict(strict=True, frozen=True, extra="forbid", populate_by_name=True)


class State(BaseModel):
    """A row in the ``states`` table, decoded into a typed value object.

    Mirrors :class:`~ctxr.fsm.sqlite.models_core.StateTable` but exposes
    ``inputs`` / ``outputs`` as already-parsed Python objects rather than the
    JSON-text storage format.
    """

    model_config = _VO_CFG

    id: str
    run_id: str
    state_id: str
    entry_seq: int
    entered_at: str
    exited_at: str | None = None
    status: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    iteration_n: int | None = None


class Transition(BaseModel):
    """A row in the ``transitions`` table, decoded into a typed value object.

    ``predicate_result`` is a tri-state: ``True`` / ``False`` / ``None``. The
    last case is normal for ``always`` / ``otherwise`` kinds where there is no
    predicate to evaluate.
    """

    model_config = _VO_CFG

    id: str
    run_id: str
    from_state_id: str
    to_state_id: str
    kind: str
    predicate: str | None = None
    predicate_result: bool | None = None
    decided_at: str


class WorkerArtifact(BaseModel):
    """A row in the ``worker_artifacts`` table, decoded into a value object.

    ``output`` is the parsed structured-response object the worker returned;
    ``prompt_text`` is preserved verbatim so the audit trail can replay the
    exact prompt that produced it. ``prompt_hash`` is the SHA-256 hex digest
    of ``prompt_text`` — the caller is responsible for computing and passing
    it (the repository does not hash on the caller's behalf).
    """

    model_config = _VO_CFG

    id: str
    run_id: str
    state_id: str
    iteration_n: int | None = None
    prompt_text: str
    prompt_hash: str
    output: dict[str, Any] = Field(default_factory=dict)
    validated: bool = False
    created_at: str


class Aggregate(BaseModel):
    """A row in the ``aggregates`` table, decoded into a value object.

    ``from_state_ids`` is the list of source ``states.id`` rows whose outputs
    the aggregator combined into ``items``. ``merged_length`` is the
    pre-computed length of ``items`` (kept denormalised so cheap "did this
    aggregate produce anything?" checks don't need to parse the JSON).
    """

    model_config = _VO_CFG

    id: str
    run_id: str
    field: str
    from_state_ids: list[str]
    merged_length: int
    items: list[Any]
    created_at: str


class StateNode(BaseModel):
    """A single node in the run's state-entry tree as returned by
    :func:`build_state_tree`.

    The tree is built by walking ``transitions`` from the entry state out to
    its successors. Because the engine may take multiple outbound transitions
    over the life of a run (loops re-enter states), the same FSM state name
    may appear in multiple nodes — the ``state`` row id is the deduplication
    key, not the FSM state name.

    ``children`` are ordered by ``entered_at`` ascending so a depth-first
    walk matches the temporal order in which the run visited the states.
    """

    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)

    state: State
    children: list[StateNode] = Field(default_factory=list)


StateNode.model_rebuild()


# ---------------------------------------------------------------------------
# StatesRepo
# ---------------------------------------------------------------------------


class StatesRepo:
    """CRUD for the ``states`` table.

    A *state* row is one *entry* of an FSM state within a run — re-entering
    the same FSM state (e.g. inside a loop) produces a new row with a fresh
    ``entry_seq``. ``id`` is the per-row UUIDv7; ``state_id`` is the
    snake_case FSM state name.

    Repository methods do not begin transactions themselves — the W2
    ``@atomic`` decorator on the calling unit-of-work is responsible for
    ``Session.begin()``.
    """

    def create(
        self,
        session: Session,
        *,
        run_id: str,
        state_id: str,
        inputs: dict[str, Any],
        entry_seq: int,
    ) -> State:
        """Insert a brand-new state-entry row and return it as a value object.

        The row is created in ``status='entered'`` with an empty ``outputs``
        bag — the caller updates those fields via :meth:`mark_exited` or
        :meth:`mark_faulted` when the state finishes.
        """
        row = StateTable(
            id=_uuid7_str(),
            run_id=run_id,
            state_id=state_id,
            entry_seq=entry_seq,
            entered_at=_now_iso_ms(),
            exited_at=None,
            status="entered",
            inputs_json=_canonical_json(inputs),
            outputs_json=_canonical_json({}),
            iteration_n=None,
        )
        session.add(row)
        session.flush()
        return self._to_value(row)

    def get(self, session: Session, state_pk: str) -> State | None:
        """Fetch a single state-entry row by primary key, or ``None``."""
        row = session.get(StateTable, state_pk)
        if row is None:
            return None
        return self._to_value(row)

    def mark_exited(
        self,
        session: Session,
        state_pk: str,
        outputs: dict[str, Any],
    ) -> State:
        """Mark a state-entry as cleanly exited and persist its outputs.

        Raises ``LookupError`` when the row does not exist — repositories
        translate "row not found on a write" into an exception rather than
        silently no-op'ing because the caller is always mid-transaction.
        """
        row = session.get(StateTable, state_pk)
        if row is None:
            raise LookupError(f"state row not found: {state_pk!r}")
        row.status = "exited"
        row.exited_at = _now_iso_ms()
        row.outputs_json = _canonical_json(outputs)
        session.add(row)
        session.flush()
        return self._to_value(row)

    def mark_faulted(
        self,
        session: Session,
        state_pk: str,
        reason: str,
    ) -> State:
        """Mark a state-entry as faulted; stash ``reason`` in ``outputs.error``.

        The ``states`` table has no dedicated fault-reason column, so we keep
        the failure narrative in the outputs bag under the well-known
        ``error`` key. The engine reads it back when surfacing fault details
        to operators.
        """
        row = session.get(StateTable, state_pk)
        if row is None:
            raise LookupError(f"state row not found: {state_pk!r}")
        existing = _decode_json(row.outputs_json, default={})
        if not isinstance(existing, dict):
            existing = {}
        existing["error"] = reason
        row.status = "faulted"
        row.exited_at = _now_iso_ms()
        row.outputs_json = _canonical_json(existing)
        session.add(row)
        session.flush()
        return self._to_value(row)

    def list_by_run(self, session: Session, run_id: str) -> list[State]:
        """Return every state-entry for ``run_id`` ordered by ``entry_seq``.

        ``entry_seq`` is the canonical total order on entries — it is
        backed by a UNIQUE index so the order is deterministic even when
        wall-clock timestamps tie.
        """
        stmt = (
            select(StateTable)
            .where(StateTable.run_id == run_id)
            .order_by(StateTable.entry_seq.asc())
        )
        rows = session.execute(stmt).scalars().all()
        return [self._to_value(r) for r in rows]

    def next_entry_seq(self, session: Session, run_id: str) -> int:
        """Return the next ``entry_seq`` to use for a new entry under ``run_id``.

        Returns ``1`` when the run has no entries yet. The caller MUST be
        inside the same transaction as the subsequent :meth:`create` to keep
        the increment race-free; SQLite's single-writer model + the
        ``(run_id, entry_seq)`` UNIQUE index turn any accidental collision
        into a hard ``IntegrityError`` rather than a silent overwrite.
        """
        stmt = select(func.max(StateTable.entry_seq)).where(
            StateTable.run_id == run_id
        )
        current = session.execute(stmt).scalar_one_or_none()
        return (int(current) if current is not None else 0) + 1

    @staticmethod
    def _to_value(row: StateTable) -> State:
        """Translate a :class:`StateTable` ORM row into a :class:`State` VO."""
        return State(
            id=row.id,
            run_id=row.run_id,
            state_id=row.state_id,
            entry_seq=row.entry_seq,
            entered_at=row.entered_at,
            exited_at=row.exited_at,
            status=row.status,
            inputs=_decode_json(row.inputs_json, default={}),
            outputs=_decode_json(row.outputs_json, default={}),
            iteration_n=row.iteration_n,
        )


# ---------------------------------------------------------------------------
# TransitionsRepo
# ---------------------------------------------------------------------------


class TransitionsRepo:
    """CRUD for the ``transitions`` table.

    Each row records one *decided* transition evaluation: source-entry row id
    (``from_state_pk`` → ``states.id``), destination FSM state name
    (``to_state_id``), guard kind, optional predicate text, and the boolean
    result (or ``None`` for ``always`` / ``otherwise`` kinds).
    """

    def create(
        self,
        session: Session,
        *,
        run_id: str,
        from_state_pk: str,
        to_state_id: str,
        kind: str,
        predicate: str | None,
        predicate_result: bool | None,
    ) -> Transition:
        """Insert a transition decision and return it as a value object."""
        row = TransitionTable(
            id=_uuid7_str(),
            run_id=run_id,
            from_state_id=from_state_pk,
            to_state_id=to_state_id,
            kind=kind,
            predicate=predicate,
            # STRICT-friendly INTEGER 0/1 storage; SQLAlchemy round-trips bool
            # through the underlying Integer column.
            predicate_result=(
                None if predicate_result is None else int(bool(predicate_result))
            ),
            decided_at=_now_iso_ms(),
        )
        session.add(row)
        session.flush()
        return self._to_value(row)

    def by_status(
        self,
        session: Session,
        *,
        predicate_result: bool | None,
    ) -> list[Transition]:
        """List transitions filtered by the tri-state ``predicate_result``.

        Pass ``True`` for transitions that fired, ``False`` for transitions
        that were evaluated and rejected, ``None`` for guards that were not
        predicate-driven (``always`` / ``otherwise``).
        """
        if predicate_result is None:
            stmt = select(TransitionTable).where(
                TransitionTable.predicate_result.is_(None)
            )
        else:
            stmt = select(TransitionTable).where(
                TransitionTable.predicate_result == int(bool(predicate_result))
            )
        stmt = stmt.order_by(TransitionTable.decided_at.asc())
        rows = session.execute(stmt).scalars().all()
        return [self._to_value(r) for r in rows]

    @staticmethod
    def _to_value(row: TransitionTable) -> Transition:
        """Translate a :class:`TransitionTable` ORM row into a value object."""
        return Transition(
            id=row.id,
            run_id=row.run_id,
            from_state_id=row.from_state_id,
            to_state_id=row.to_state_id,
            kind=row.kind,
            predicate=row.predicate,
            predicate_result=_coerce_predicate_result(row.predicate_result),
            decided_at=row.decided_at,
        )


# ---------------------------------------------------------------------------
# WorkerArtifactsRepo
# ---------------------------------------------------------------------------


class WorkerArtifactsRepo:
    """CRUD for the ``worker_artifacts`` table.

    A worker artifact captures the dispatched prompt and the structured
    response returned by a worker for a single state entry (or, in the loop
    case, a single iteration within a state entry).
    """

    def create(
        self,
        session: Session,
        *,
        run_id: str,
        state_pk: str,
        iteration_n: int | None,
        prompt_text: str,
        prompt_hash: str,
        output: dict[str, Any],
        validated: bool,
    ) -> WorkerArtifact:
        """Insert a worker-artifact row and return it as a value object.

        ``prompt_hash`` is the SHA-256 hex digest of ``prompt_text``. The
        repository does *not* recompute it — the caller is closer to the
        prompt-rendering pipeline and already owns the hash for cache
        lookups elsewhere.
        """
        row = WorkerArtifactTable(
            id=_uuid7_str(),
            run_id=run_id,
            state_id=state_pk,
            iteration_n=iteration_n,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            output_json=_canonical_json(output),
            # STRICT-friendly INTEGER 0/1 storage.
            validated=int(bool(validated)),
            created_at=_now_iso_ms(),
        )
        session.add(row)
        session.flush()
        return self._to_value(row)

    def by_state(self, session: Session, state_pk: str) -> list[WorkerArtifact]:
        """Return every artifact for one state entry ordered by ``iteration_n``.

        Rows with ``iteration_n IS NULL`` (the non-loop case) sort first; the
        ``created_at`` tiebreaker keeps the order deterministic when two
        loop iterations happen to share the same iteration number (shouldn't
        happen in practice but we don't want a non-deterministic test).
        """
        stmt = (
            select(WorkerArtifactTable)
            .where(WorkerArtifactTable.state_id == state_pk)
            .order_by(
                WorkerArtifactTable.iteration_n.asc().nulls_first(),
                WorkerArtifactTable.created_at.asc(),
            )
        )
        rows = session.execute(stmt).scalars().all()
        return [self._to_value(r) for r in rows]

    @staticmethod
    def _to_value(row: WorkerArtifactTable) -> WorkerArtifact:
        """Translate a :class:`WorkerArtifactTable` ORM row into a value object."""
        return WorkerArtifact(
            id=row.id,
            run_id=row.run_id,
            state_id=row.state_id,
            iteration_n=row.iteration_n,
            prompt_text=row.prompt_text,
            prompt_hash=row.prompt_hash,
            output=_decode_json(row.output_json, default={}),
            validated=bool(row.validated),
            created_at=row.created_at,
        )


# ---------------------------------------------------------------------------
# AggregatesRepo
# ---------------------------------------------------------------------------


class AggregatesRepo:
    """CRUD for the ``aggregates`` table.

    An aggregate row is the persisted output of one cross-state aggregator
    (see :mod:`ctxr.fsm.core.aggregator`). It is keyed by ``(run_id, field)``
    in the engine's mental model — the schema does not yet enforce a UNIQUE
    constraint on that pair so :meth:`get` returns the *latest* matching row
    when more than one exists.
    """

    def create(
        self,
        session: Session,
        *,
        run_id: str,
        field: str,
        from_state_ids: Iterable[str],
        merged_length: int,
        items: list[Any],
    ) -> Aggregate:
        """Insert an aggregate row and return it as a value object.

        ``from_state_ids`` is consumed as any iterable but persisted as a
        concrete list (so a generator passed by the caller is materialised
        exactly once).
        """
        source_ids = list(from_state_ids)
        row = AggregateTable(
            id=_uuid7_str(),
            run_id=run_id,
            field=field,
            from_state_ids_json=_canonical_json(source_ids),
            merged_length=merged_length,
            items_json=_canonical_json(items),
            created_at=_now_iso_ms(),
        )
        session.add(row)
        session.flush()
        return self._to_value(row)

    def get(self, session: Session, run_id: str, field: str) -> Aggregate | None:
        """Return the latest aggregate for ``(run_id, field)`` or ``None``.

        Ordering by ``created_at DESC`` (with the UUIDv7 ``id`` as a stable
        tiebreaker) means we always hand back the freshest aggregate when a
        run accidentally produced more than one for the same field.
        """
        stmt = (
            select(AggregateTable)
            .where(
                AggregateTable.run_id == run_id,
                AggregateTable.field == field,
            )
            .order_by(
                AggregateTable.created_at.desc(),
                AggregateTable.id.desc(),
            )
            .limit(1)
        )
        row = session.execute(stmt).scalars().first()
        if row is None:
            return None
        return self._to_value(row)

    @staticmethod
    def _to_value(row: AggregateTable) -> Aggregate:
        """Translate an :class:`AggregateTable` ORM row into a value object."""
        return Aggregate(
            id=row.id,
            run_id=row.run_id,
            field=row.field,
            from_state_ids=_decode_json(row.from_state_ids_json, default=[]),
            merged_length=row.merged_length,
            items=_decode_json(row.items_json, default=[]),
            created_at=row.created_at,
        )


# ---------------------------------------------------------------------------
# build_state_tree
# ---------------------------------------------------------------------------


def build_state_tree(session: Session, run_id: str) -> StateNode | None:
    """Construct the nested state-entry tree for ``run_id``.

    Algorithm:

    1. Fetch every state-entry for the run in ``entry_seq`` order — that
       gives us the deterministic root (lowest ``entry_seq``) and lets us
       index by row id in a single pass.
    2. Fetch every transition for the run; group by ``from_state_id`` so we
       can build child lists by destination FSM state name.
    3. For each transition out of a parent entry, find the *next* entry of
       the destination FSM state whose ``entry_seq`` is greater than the
       parent's. The first such entry becomes the child; ties are broken
       deterministically by ``entry_seq`` ascending.
    4. Walk recursively from the lowest-``entry_seq`` row to assemble the
       tree.

    Returns ``None`` when the run has no state entries yet (i.e. the run was
    created but never ran a single state) — the caller's response shape
    distinguishes "no tree" from "empty tree".

    This helper is colocated with the sub-repos because :class:`RunsRepo` in
    the upcoming W2-runs module surfaces it as ``RunsRepo.state_tree`` and
    we want a single place that owns the tree-shaping logic.
    """
    states_repo = StatesRepo()
    transitions_repo = TransitionsRepo()
    entries = states_repo.list_by_run(session, run_id)
    if not entries:
        return None

    # Index entries by their row PK for O(1) lookup, and by FSM state name
    # for the "find the next entry of state X after entry_seq N" query.
    {e.id: e for e in entries}
    by_state_name: dict[str, list[State]] = {}
    for entry in entries:
        by_state_name.setdefault(entry.state_id, []).append(entry)
    # ``list_by_run`` already returns rows in ``entry_seq`` order, so the
    # bucketed lists are pre-sorted — no extra sort step needed.

    # Build a from_pk -> [transition...] index. We pull every transition for
    # this run and filter by source PK locally; this is a single query, and
    # the number of transitions per run is bounded by FSM size * iterations
    # so it comfortably fits in memory.
    all_transitions = transitions_repo.by_status(session, predicate_result=True)
    # We need ``always`` / ``otherwise`` (NULL predicate_result) edges too —
    # ``by_status(predicate_result=True)`` would miss them. Fetch both
    # buckets and concatenate.
    all_transitions += transitions_repo.by_status(session, predicate_result=None)
    out_edges: dict[str, list[Transition]] = {}
    for t in all_transitions:
        if t.run_id != run_id:
            continue
        out_edges.setdefault(t.from_state_id, []).append(t)
    # Stable ordering by ``decided_at`` ensures the resulting tree is
    # deterministic across runs of this function on the same data.
    for edges in out_edges.values():
        edges.sort(key=lambda e: e.decided_at)

    visited: set[str] = set()

    def _build(parent: State) -> StateNode:
        """Recursively materialise the subtree rooted at ``parent``."""
        visited.add(parent.id)
        node = StateNode(state=parent, children=[])
        for edge in out_edges.get(parent.id, []):
            # Find the first entry of the destination FSM state name that
            # was created after the parent (entry_seq strictly greater) and
            # has not yet been claimed by another branch of the walk.
            destination_entries = by_state_name.get(edge.to_state_id, [])
            child: State | None = None
            for candidate in destination_entries:
                if candidate.id in visited:
                    continue
                if candidate.entry_seq <= parent.entry_seq:
                    continue
                child = candidate
                break
            if child is None:
                # The transition was decided but the destination state never
                # produced an entry (e.g. the run halted between decision
                # and entry). Skip — there is no tree node to attach.
                continue
            node.children.append(_build(child))
        return node

    root_entry = entries[0]
    return _build(root_entry)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "Aggregate",
    "AggregatesRepo",
    "State",
    "StateNode",
    "StatesRepo",
    "Transition",
    "TransitionsRepo",
    "WorkerArtifact",
    "WorkerArtifactsRepo",
    "build_state_tree",
]
