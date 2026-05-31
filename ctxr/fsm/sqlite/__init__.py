"""Public surface of the ctxr.fsm SQLite substrate.

The single entry point most callers want is :class:`Project` -- the
facade that bundles the engine plus every sub-repository plus the
convenience operations (``register_spec``, ``start_run``, ``subscribe``,
...). Lower-level repository classes, connection helpers, transaction
machinery, and the Pydantic value objects each repository speaks are
all re-exported here so a caller never has to remember which
submodule owns which symbol.

Naming caveats
--------------

Two of the lifecycle value objects share a name with concepts already
exposed at this level, so we rename them on re-export to keep the
namespace unambiguous:

* :class:`Project` (the facade in :mod:`ctxr.fsm.sqlite.project`) wins
  the bare ``Project`` name. The lifecycle row value-object from
  :mod:`ctxr.fsm.sqlite.repos_core` is re-exported as
  :class:`ProjectRecord`.
* :class:`State` and :class:`Transition` from
  :mod:`ctxr.fsm.sqlite.repos_states` are re-exported as
  :class:`StateRecord` and :class:`TransitionRecord` respectively.
  This keeps the door open for higher layers (W3 engine, W4 MCP
  server) to ship their own ``State`` / ``Transition`` domain types
  without colliding with the persistence-layer shapes.

Two modules each define an ``Event`` class with overlapping shapes
(:mod:`ctxr.fsm.sqlite.repos_core` for the lifecycle journal,
:mod:`ctxr.fsm.sqlite.repos_events` for the in-process event bus).
We re-export the lifecycle ``Event`` because that is the shape the
:meth:`Project.subscribe` facade yields; the bus-side ``Event`` lives
behind ``EventWithDelivery`` and stays addressable via
``ctxr.fsm.sqlite.repos_events.Event`` for advanced callers.

Likewise, :class:`StateNode` is sourced from
:mod:`ctxr.fsm.sqlite.repos_states` (built by ``build_state_tree``);
the older shape in :mod:`ctxr.fsm.sqlite.repos_core` remains importable
from that submodule for backward compatibility.
"""

from __future__ import annotations

from ctxr.fsm.sqlite.connection import (
    detect_journal_state,
    ensure_strict_tables,
    open_engine,
    open_session,
)
from ctxr.fsm.sqlite.project import Project, run_migrations
from ctxr.fsm.sqlite.repos_core import (
    Event,
    ProjectsRepo,
    RegisteredSpec,
    Run,
    RunSession,
    RunSessionsRepo,
    RunsRepo,
    RunSummary,
    SpecsRepo,
)
from ctxr.fsm.sqlite.repos_core import (
    Project as ProjectRecord,
)
from ctxr.fsm.sqlite.repos_enforcement import (
    CommitSignatureRecord,
    CommitSignaturesRepo,
    CommitTokenRecord,
    CommitTokensRepo,
    ConsumeResult,
    DriftSignal,
    DriftSignalsRepo,
    ToolCall,
    ToolCallsRepo,
)
from ctxr.fsm.sqlite.repos_events import (
    Consumer,
    ConsumersRepo,
    EventDeliveriesRepo,
    EventsRepo,
    EventWithDelivery,
    Producer,
    ProducersRepo,
)
from ctxr.fsm.sqlite.repos_locks_journal import (
    JournalRepo,
    JournalTxn,
    Lock,
    LockResult,
    LocksRepo,
    ReleaseResult,
)
from ctxr.fsm.sqlite.repos_states import (
    Aggregate,
    AggregatesRepo,
    StateNode,
    StatesRepo,
    TransitionsRepo,
    WorkerArtifact,
    WorkerArtifactsRepo,
)
from ctxr.fsm.sqlite.repos_states import (
    State as StateRecord,
)
from ctxr.fsm.sqlite.repos_states import (
    Transition as TransitionRecord,
)
from ctxr.fsm.sqlite.transactions import (
    AtomicError,
    JournalRefusedError,
    TransactionContext,
    atomic,
)

__all__ = [
    "Aggregate",
    "AggregatesRepo",
    "AtomicError",
    "CommitSignatureRecord",
    "CommitSignaturesRepo",
    "CommitTokenRecord",
    "CommitTokensRepo",
    "ConsumeResult",
    "Consumer",
    "ConsumersRepo",
    "DriftSignal",
    "DriftSignalsRepo",
    "Event",
    "EventDeliveriesRepo",
    "EventWithDelivery",
    "EventsRepo",
    "JournalRefusedError",
    "JournalRepo",
    "JournalTxn",
    # ── Concurrency + journal value objects ──────────────────────────
    "Lock",
    "LockResult",
    # ── Concurrency + journal repositories ───────────────────────────
    "LocksRepo",
    # ── Event-bus value objects ──────────────────────────────────────
    "Producer",
    # ── Event-bus repositories ───────────────────────────────────────
    "ProducersRepo",
    # ── Facade + migration helper ────────────────────────────────────
    "Project",
    # ── Lifecycle value objects ──────────────────────────────────────
    "ProjectRecord",
    # ── Lifecycle repositories ───────────────────────────────────────
    "ProjectsRepo",
    "RegisteredSpec",
    "ReleaseResult",
    "Run",
    "RunSession",
    "RunSessionsRepo",
    "RunSummary",
    "RunsRepo",
    "SpecsRepo",
    "StateNode",
    # ── State-tree value objects ─────────────────────────────────────
    "StateRecord",
    # ── State-tree repositories ──────────────────────────────────────
    "StatesRepo",
    # ── Enforcement value objects ────────────────────────────────────
    "ToolCall",
    # ── Enforcement repositories ─────────────────────────────────────
    "ToolCallsRepo",
    "TransactionContext",
    "TransitionRecord",
    "TransitionsRepo",
    "WorkerArtifact",
    "WorkerArtifactsRepo",
    # ── Transaction machinery ────────────────────────────────────────
    "atomic",
    "detect_journal_state",
    "ensure_strict_tables",
    # ── Connection helpers ───────────────────────────────────────────
    "open_engine",
    "open_session",
    "run_migrations",
]
