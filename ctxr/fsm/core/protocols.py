"""Structural protocols for the FSM persistence and runtime substrate.

This module defines the typing :class:`~typing.Protocol` surface that the
SQLite-backed implementation in ``ctxr.fsm.sqlite`` (workstream W2) will
satisfy. The engine in ``ctxr.fsm.core.engine`` imports these as
typing-only references so that the core package stays free of any
SQLAlchemy / Alembic / SQLite dependencies.

The protocols are intentionally *minimal* — they list the methods named
in the plan's W2 ``Repository`` specification but make no claim about
internal implementation details (locking strategies, batching, JSON
encoding, etc.). They are :func:`runtime_checkable` so that test fakes
and in-memory shims can be type-checked with :func:`isinstance` at
boundaries.

Cross-cutting value objects (``Event``, ``LockResult``,
``ReleaseResult``, ``JournalTxn``) live in this module too because the
Protocol return types need them: putting them anywhere else would force
implementers to import a non-protocol module just to satisfy the
interface.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ctxr.fsm.core.models import (
    Brief,
    CommitSignature,
    CommitToken,
    DeliveryStatus,
    EventKind,
    FsmSpec,
    PostValidationResult,
    RunStatus,
    SignalKind,
    StateStatus,
    TransitionKind,
    ValidationResult,
    VerifierVerdict,
)

# ---------------------------------------------------------------------------
# Shared value-object configs
# ---------------------------------------------------------------------------

_DOMAIN_CFG = ConfigDict(strict=True, frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Cross-cutting value objects
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """A single emitted domain event as observed by the event bus.

    Carries the identifiers needed to route the event to subscribers
    (``run_id``, ``producer_id``, ``kind``) plus the opaque payload and
    the per-run monotonic sequence number that backs replay.
    """

    model_config = _DOMAIN_CFG

    id: uuid.UUID
    run_id: uuid.UUID | None
    producer_id: uuid.UUID
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    seq: int
    created_at: datetime


class LockAcquisitionStatus(StrEnum):
    """The outcome of an attempt to acquire a per-run write lock."""

    acquired = "acquired"
    contended = "contended"
    reentrant = "reentrant"
    taken_over = "taken_over"


class LockReleaseStatus(StrEnum):
    """The outcome of an attempt to release a per-run write lock."""

    released = "released"
    not_held = "not_held"
    expired = "expired"


class Lock(BaseModel):
    """A snapshot of the per-run single-writer lock state."""

    model_config = _DOMAIN_CFG

    run_id: uuid.UUID
    holder_session_id: str
    acquired_at: datetime
    expires_at: datetime


class LockResult(BaseModel):
    """The structured return of :meth:`LockProtocol.acquire`."""

    model_config = _DOMAIN_CFG

    status: LockAcquisitionStatus
    lock: Lock | None = None
    held_by: str | None = None


class ReleaseResult(BaseModel):
    """The structured return of :meth:`LockProtocol.release`."""

    model_config = _DOMAIN_CFG

    status: LockReleaseStatus
    released_at: datetime | None = None


class JournalTxnStatus(StrEnum):
    """Lifecycle status of a single atomic-tx journal row."""

    pending = "pending"
    ready_to_finalise = "ready_to_finalise"
    finalised = "finalised"
    discarded = "discarded"


class JournalTxn(BaseModel):
    """A snapshot of an entry in the atomic-tx journal."""

    model_config = _DOMAIN_CFG

    id: uuid.UUID
    run_id: uuid.UUID
    status: JournalTxnStatus
    staged_writes: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    ready_at: datetime | None = None
    finalised_at: datetime | None = None


# ---------------------------------------------------------------------------
# Repository sub-protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ProjectsRepo(Protocol):
    """The projects aggregate root."""

    def create(self, slug: str, metadata: dict[str, Any] | None = None) -> uuid.UUID:
        """Create a project row and return its id."""
        ...

    def get(self, project_id: uuid.UUID) -> Any:
        """Return the project row for ``project_id`` (or raise ``KeyError``)."""
        ...

    def list(self) -> list[Any]:
        """Return every project row known to this repository."""
        ...


@runtime_checkable
class SpecsRepo(Protocol):
    """The fsm_specs aggregate root."""

    def register(self, project_id: uuid.UUID, spec: FsmSpec) -> uuid.UUID:
        """Register ``spec`` for ``project_id`` and return the spec row id."""
        ...

    def get(self, spec_id: uuid.UUID) -> FsmSpec:
        """Return the ``FsmSpec`` row for ``spec_id`` (or raise ``KeyError``)."""
        ...

    def list_versions(self, project_id: uuid.UUID, slug: str) -> list[FsmSpec]:
        """Return every version of the spec identified by ``(project_id, slug)``."""
        ...


@runtime_checkable
class RunsRepo(Protocol):
    """The runs aggregate root."""

    def create(
        self,
        project_id: uuid.UUID,
        fsm_spec_id: uuid.UUID,
        args: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        parent_run_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Create a run row and return its id."""
        ...

    def get(self, run_id: uuid.UUID) -> Any:
        """Return the run row for ``run_id`` (or raise ``KeyError``)."""
        ...

    def latest(self, limit: int = 20) -> list[Any]:
        """Return the most recently active runs (newest first)."""
        ...

    def incomplete(self) -> list[Any]:
        """Return runs that have neither completed nor aborted."""
        ...

    def resumable(self) -> list[Any]:
        """Return runs whose status permits a resume operation."""
        ...

    def by_status(self, status: RunStatus) -> list[Any]:
        """Return runs filtered by ``status``."""
        ...

    def by_session(self, session_id: str) -> list[Any]:
        """Return runs ever attached to ``session_id``."""
        ...

    def by_project(self, project_id: uuid.UUID) -> list[Any]:
        """Return runs scoped to ``project_id``."""
        ...

    def aborted(self) -> list[Any]:
        """Return runs whose terminal status is ``aborted``."""
        ...

    def failed(self) -> list[Any]:
        """Return runs whose terminal status is ``faulted``."""
        ...

    def completed(self) -> list[Any]:
        """Return runs whose terminal status is ``completed``."""
        ...

    def state_tree(self, run_id: uuid.UUID) -> Any:
        """Return a nested state-tree value object for ``run_id``."""
        ...

    def events(
        self,
        run_id: uuid.UUID,
        since_seq: int | None = None,
        kinds: list[EventKind] | None = None,
    ) -> Iterator[Event]:
        """Iterate replayable events for ``run_id`` in ascending ``seq`` order."""
        ...


@runtime_checkable
class StatesRepo(Protocol):
    """The states aggregate root."""

    def create(
        self,
        run_id: uuid.UUID,
        state_id: str,
        entry_seq: int,
        inputs: dict[str, Any] | None = None,
        iteration_n: int | None = None,
    ) -> uuid.UUID:
        """Insert a state instance row and return its id."""
        ...

    def get(self, state_row_id: uuid.UUID) -> Any:
        """Return the state-instance row identified by ``state_row_id``."""
        ...

    def mark_exited(
        self,
        state_row_id: uuid.UUID,
        outputs: dict[str, Any],
    ) -> None:
        """Mark a state as ``exited`` and persist its outputs."""
        ...

    def mark_faulted(
        self,
        state_row_id: uuid.UUID,
        error: str,
    ) -> None:
        """Mark a state as ``faulted`` and persist the error."""
        ...


@runtime_checkable
class TransitionsRepo(Protocol):
    """The transitions aggregate root."""

    def create(
        self,
        run_id: uuid.UUID,
        from_state_id: uuid.UUID,
        to_state_id: str,
        kind: TransitionKind,
        predicate: str | None,
        predicate_result: bool | None,
    ) -> uuid.UUID:
        """Insert a transition row and return its id."""
        ...

    def by_status(
        self,
        predicate_result: bool | None = None,
    ) -> list[Any]:
        """Return transitions filtered by their predicate evaluation result."""
        ...


@runtime_checkable
class EventsRepo(Protocol):
    """The events aggregate root."""

    def emit(
        self,
        producer_id: uuid.UUID,
        kind: EventKind,
        payload: dict[str, Any],
        run_id: uuid.UUID | None = None,
    ) -> Event:
        """Emit an event, fan it out to matching consumers, and return it."""
        ...

    def by_producer(
        self,
        producer_id: uuid.UUID,
        kinds: list[EventKind] | None = None,
    ) -> list[Event]:
        """Return events filtered by ``producer_id`` and optional ``kinds``."""
        ...


@runtime_checkable
class ProducersRepo(Protocol):
    """The producers aggregate root."""

    def upsert(
        self,
        kind: str,
        name: str,
        metadata: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Idempotently register a producer; returns its stable id."""
        ...

    def get(self, producer_id: uuid.UUID) -> Any:
        """Return the producer row identified by ``producer_id``."""
        ...


@runtime_checkable
class ConsumersRepo(Protocol):
    """The consumers aggregate root."""

    def register(
        self,
        kind: str,
        name: str,
        filter_kind: list[EventKind] | None = None,
        filter_run_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Register a consumer subscription; returns the consumer id."""
        ...

    def subscribe(self, consumer_id: uuid.UUID) -> Iterator[Event]:
        """Yield pending events for ``consumer_id`` until the iterator closes."""
        ...

    def ack(
        self,
        event_id: uuid.UUID,
        consumer_id: uuid.UUID,
        status: DeliveryStatus = DeliveryStatus.acked,
    ) -> None:
        """Mark the ``(event_id, consumer_id)`` delivery as ``status``."""
        ...


@runtime_checkable
class WorkerArtifactsRepo(Protocol):
    """The worker_artifacts aggregate root."""

    def create(
        self,
        run_id: uuid.UUID,
        state_row_id: uuid.UUID,
        prompt_text: str,
        prompt_hash: str,
        output: dict[str, Any],
        validated: bool,
        iteration_n: int | None = None,
    ) -> uuid.UUID:
        """Persist a worker artifact row and return its id."""
        ...

    def by_state(self, state_row_id: uuid.UUID) -> list[Any]:
        """Return every artifact recorded against ``state_row_id``."""
        ...


@runtime_checkable
class AggregatesRepo(Protocol):
    """The aggregates aggregate root."""

    def create(
        self,
        run_id: uuid.UUID,
        field: str,
        from_state_ids: list[uuid.UUID],
        items: list[Any],
    ) -> uuid.UUID:
        """Persist an aggregate row and return its id."""
        ...

    def get(self, aggregate_id: uuid.UUID) -> Any:
        """Return the aggregate row for ``aggregate_id``."""
        ...


@runtime_checkable
class LocksRepo(Protocol):
    """The locks aggregate root: thin CRUD over the lock table."""

    def acquire(
        self,
        run_id: uuid.UUID,
        session_id: str,
        ttl_seconds: int,
    ) -> LockResult:
        """Attempt to acquire the per-run lock."""
        ...

    def release(self, run_id: uuid.UUID, session_id: str) -> ReleaseResult:
        """Release the per-run lock if held by ``session_id``."""
        ...

    def inspect(self, run_id: uuid.UUID) -> Lock | None:
        """Return the current lock snapshot or ``None`` if not held."""
        ...


@runtime_checkable
class JournalRepo(Protocol):
    """The atomic-tx journal aggregate root."""

    def open(self, run_id: uuid.UUID) -> JournalTxn:
        """Open a new pending journal row for ``run_id``."""
        ...

    def mark_ready(self, txn_id: uuid.UUID) -> None:
        """Flip a pending journal row to ``ready_to_finalise``."""
        ...

    def finalise(self, txn_id: uuid.UUID) -> None:
        """Mark a journal row as ``finalised``."""
        ...

    def discard(self, txn_id: uuid.UUID) -> None:
        """Mark a journal row as ``discarded`` (rollback completed)."""
        ...

    def inspect(self, run_id: uuid.UUID) -> JournalTxn | None:
        """Return the currently-open journal txn for ``run_id`` (if any)."""
        ...


@runtime_checkable
class CommitSignaturesRepo(Protocol):
    """The commit_signatures aggregate root (layer 5 cosignature ledger)."""

    def record(
        self,
        run_id: uuid.UUID,
        state_row_id: uuid.UUID,
        brief_id: uuid.UUID,
        signature: CommitSignature,
        verified: bool,
        iteration_n: int | None = None,
    ) -> uuid.UUID:
        """Persist a commit-signature row and return its id."""
        ...

    def get(self, signature_id: uuid.UUID) -> Any:
        """Return the commit-signature row identified by ``signature_id``."""
        ...


@runtime_checkable
class CommitTokensRepo(Protocol):
    """The commit_tokens aggregate root (layer 12 two-phase commit)."""

    def issue(self, token: CommitToken) -> None:
        """Persist a newly-minted commit token."""
        ...

    def consume(
        self,
        token: uuid.UUID,
        expected_next_state: str,
    ) -> CommitToken:
        """Atomically consume ``token`` and assert ``expected_next_state``."""
        ...

    def get(self, token: uuid.UUID) -> CommitToken | None:
        """Return the token row if present and unconsumed."""
        ...


@runtime_checkable
class ToolCallsRepo(Protocol):
    """The tool_calls aggregate root (layer 7 audit log)."""

    def record(
        self,
        producer_id: uuid.UUID,
        tool_name: str,
        args_redacted: dict[str, Any],
        succeeded: bool,
        run_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Persist a tool-call audit row and return its id."""
        ...

    def by_run(self, run_id: uuid.UUID) -> list[Any]:
        """Return every tool-call audit row recorded against ``run_id``."""
        ...


@runtime_checkable
class DriftSignalsRepo(Protocol):
    """The drift_signals aggregate root (layer 8 drift detector inputs)."""

    def record(
        self,
        run_id: uuid.UUID,
        producer_id: uuid.UUID,
        signal_kind: SignalKind,
        weight: float,
        payload: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Persist a drift-signal row and return its id."""
        ...

    def by_run(self, run_id: uuid.UUID) -> list[Any]:
        """Return every drift-signal row recorded against ``run_id``."""
        ...


# ---------------------------------------------------------------------------
# Top-level Repository facade
# ---------------------------------------------------------------------------


@runtime_checkable
class Repository(Protocol):
    """The aggregated repository surface exposed by ``ctxr.fsm.sqlite``.

    Implementations expose each sub-protocol as a same-named attribute.
    The engine accesses them via dotted paths (``repo.runs.latest()``,
    ``repo.events.emit(...)``) so adding a new aggregate root requires
    only a new sub-Protocol plus a same-named attribute on the
    implementing class.
    """

    projects: ProjectsRepo
    specs: SpecsRepo
    runs: RunsRepo
    states: StatesRepo
    transitions: TransitionsRepo
    events: EventsRepo
    producers: ProducersRepo
    consumers: ConsumersRepo
    worker_artifacts: WorkerArtifactsRepo
    aggregates: AggregatesRepo
    locks: LocksRepo
    journal: JournalRepo
    commit_signatures: CommitSignaturesRepo
    commit_tokens: CommitTokensRepo
    tool_calls: ToolCallsRepo
    drift_signals: DriftSignalsRepo


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


@runtime_checkable
class EventBus(Protocol):
    """The minimal event-bus surface the engine relies on.

    Implementations are free to back the bus with the same SQLite
    tables that :class:`EventsRepo` and :class:`ConsumersRepo` cover or
    to layer an in-memory pub/sub on top. The Protocol does not
    constrain that choice — it only insists on the three operations
    the engine, MCP server, and API need.
    """

    def emit(
        self,
        producer_id: uuid.UUID,
        kind: EventKind,
        payload: dict[str, Any],
        run_id: uuid.UUID | None = None,
    ) -> Event:
        """Publish a new event onto the bus and return the persisted row."""
        ...

    def subscribe(self, consumer_id: uuid.UUID) -> Iterator[Event]:
        """Yield pending events for ``consumer_id`` until the iterator closes."""
        ...

    def ack(self, event_id: uuid.UUID, consumer_id: uuid.UUID) -> None:
        """Acknowledge delivery of ``event_id`` to ``consumer_id``."""
        ...


# ---------------------------------------------------------------------------
# Lock protocol (per-run single-writer)
# ---------------------------------------------------------------------------


@runtime_checkable
class LockProtocol(Protocol):
    """The per-run single-writer lock contract.

    A separate Protocol from :class:`LocksRepo` because the engine
    sometimes wants to call ``acquire`` / ``release`` / ``inspect``
    through a façade that may layer extra behaviour (telemetry, retry
    policy, in-process re-entrancy tracking) on top of the bare repo.
    """

    def acquire(
        self,
        run_id: uuid.UUID,
        session_id: str,
        ttl_seconds: int,
    ) -> LockResult:
        """Attempt to acquire the per-run lock for ``session_id``."""
        ...

    def release(self, run_id: uuid.UUID, session_id: str) -> ReleaseResult:
        """Release the per-run lock if held by ``session_id``."""
        ...

    def inspect(self, run_id: uuid.UUID) -> Lock | None:
        """Return the current lock snapshot for ``run_id``."""
        ...


# ---------------------------------------------------------------------------
# Journal protocol (atomic-tx journal)
# ---------------------------------------------------------------------------


@runtime_checkable
class JournalProtocol(Protocol):
    """The atomic-tx journal contract used by the engine's commit path.

    Mirrors the surface exposed by :class:`JournalRepo` but is exposed
    as its own Protocol so the engine can depend on a thin facade
    without dragging the rest of the repository graph into its type
    surface.
    """

    def open(self, run_id: uuid.UUID) -> JournalTxn:
        """Open a new pending journal row for ``run_id``."""
        ...

    def mark_ready(self, txn_id: uuid.UUID) -> None:
        """Flip a pending journal row to ``ready_to_finalise``."""
        ...

    def finalise(self, txn_id: uuid.UUID) -> None:
        """Mark a journal row as ``finalised``."""
        ...

    def discard(self, txn_id: uuid.UUID) -> None:
        """Mark a journal row as ``discarded`` (rollback completed)."""
        ...

    def inspect(self, run_id: uuid.UUID) -> JournalTxn | None:
        """Return the currently-open journal txn for ``run_id`` (if any)."""
        ...


__all__ = [
    "AggregatesRepo",
    # re-exported references commonly needed at the protocol boundary
    "Brief",
    "CommitSignature",
    "CommitSignaturesRepo",
    "CommitToken",
    "CommitTokensRepo",
    "ConsumersRepo",
    "DeliveryStatus",
    "DriftSignalsRepo",
    # value objects
    "Event",
    "EventBus",
    "EventKind",
    "EventsRepo",
    "FsmSpec",
    "JournalProtocol",
    "JournalRepo",
    "JournalTxn",
    "JournalTxnStatus",
    "Lock",
    "LockAcquisitionStatus",
    "LockProtocol",
    "LockReleaseStatus",
    "LockResult",
    "LocksRepo",
    "PostValidationResult",
    "ProducersRepo",
    # sub-protocols
    "ProjectsRepo",
    "ReleaseResult",
    # top-level protocols
    "Repository",
    "RunStatus",
    "RunsRepo",
    "SignalKind",
    "SpecsRepo",
    "StateStatus",
    "StatesRepo",
    "ToolCallsRepo",
    "TransitionKind",
    "TransitionsRepo",
    "ValidationResult",
    "VerifierVerdict",
    "WorkerArtifactsRepo",
]
