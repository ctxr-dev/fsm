"""Public entry point for the W2 SQLite substrate.

This module wraps the engine, every sub-repository, the active
sessionmaker, and a handful of convenience operations into a single
:class:`Project` facade. Higher layers (W3 CLI, W4+ MCP server, tests,
notebooks) instantiate exactly one ``Project`` per database and use it
as the only handle they need.

What this facade is — and is not
--------------------------------

The facade is *thin*. It does not invent a new domain language; it
simply binds the engine produced by :func:`open_engine` to the family
of repositories declared elsewhere in this package and offers four
convenience operations on top:

* :meth:`open` — class-method constructor (engine + optional migration
  upgrade + every sub-repo wired up).
* :meth:`register_spec` — one-shot wrapper around
  :meth:`ProjectsRepo.get_by_slug` + :meth:`SpecsRepo.register`. Used
  by the CLI's ``fsm spec register`` command and by ad-hoc scripts.
* :meth:`start_run` — opens an ``@atomic``-wrapped unit-of-work that
  inserts the run row, registers a producer for the runtime, and
  emits a single ``run_started`` event so subscribers see the new run
  immediately.
* :meth:`get_run` — flat read-through to :meth:`RunsRepo.get`.
* :meth:`subscribe` — a polling iterator that yields events as they
  appear on the bus. The poll interval is 250 ms by default and can
  be overridden via the ``poll_interval_seconds`` keyword.
* :meth:`close` plus ``__enter__`` / ``__exit__`` for context-manager
  use so test fixtures can write ``with Project.open(...) as proj:``.

What this facade is *not*: a state-machine engine. The actual
``advance``/``loop_decide``/``aggregate`` logic lives in
:mod:`ctxr.fsm.core` and is driven by W3+. Here we only own the
plumbing.

Migration handling
------------------

When ``migrate=True`` (the default), :meth:`open` runs Alembic's
``upgrade head`` against ``db_path`` *before* the sub-repositories are
constructed. We do this programmatically via :class:`alembic.config.Config`
plus :func:`alembic.command.upgrade` — no subprocess, no extra
environment variables required beyond the ``CTXR_FSM_DB_URL`` override
that :mod:`migrations.env` already honours.

The companion helper :func:`run_migrations` is exposed so callers that
need to upgrade a database *without* opening a project (e.g. a
deployment script that runs migrations before the server boots) can
reuse the same logic.

Pure ``ctxr.fsm.sqlite`` — no FastAPI / MCP imports.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ctxr.fsm.core.models import EventKind, FsmSpec
from ctxr.fsm.sqlite.connection import open_engine
from ctxr.fsm.sqlite.repos_core import (
    Event,
    ProjectsRepo,
    Run,
    RunSessionsRepo,
    RunsRepo,
    SpecRegistered,
    SpecsRepo,
    get_session_factory,
)
from ctxr.fsm.sqlite.repos_enforcement import (
    CommitSignaturesRepo,
    CommitTokensRepo,
    DriftSignalsRepo,
    ToolCallsRepo,
)
from ctxr.fsm.sqlite.repos_events import (
    ConsumersRepo,
    EventDeliveriesRepo,
    EventsRepo,
    ProducersRepo,
)
from ctxr.fsm.sqlite.repos_gates import GatesRepo
from ctxr.fsm.sqlite.repos_locks_journal import JournalRepo, LocksRepo
from ctxr.fsm.sqlite.repos_states import (
    AggregatesRepo,
    StatesRepo,
    TransitionsRepo,
    WorkerArtifactsRepo,
)
from ctxr.fsm.sqlite.transactions import (
    TransactionContext,
    set_active_session_factory,
)

__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "Project",
    "run_migrations",
]


# Default polling cadence for :meth:`Project.subscribe`. 250 ms is fast
# enough that an interactive CLI feels live and slow enough that an idle
# database is not hammered with empty SELECTs. Callers that want a
# different cadence (longer for cost-sensitive deployments, shorter for
# integration tests) pass ``poll_interval_seconds=...`` to ``subscribe``.
DEFAULT_POLL_INTERVAL_SECONDS: float = 0.25

# The producer name we register for the FSM runtime itself. Every event
# emitted by the engine (run_started, state_entered, …) is attributed to
# this producer so downstream consumers can filter on it.
_RUNTIME_PRODUCER_KIND: str = "engine"
_RUNTIME_PRODUCER_NAME: str = "fsm.runtime"


def _find_alembic_ini() -> Path:
    """Locate ``alembic.ini`` for the ctxr-fsm package.

    Walks up from this module's filesystem location looking for an
    ``alembic.ini`` sibling of a ``migrations/`` directory. We do not
    rely on the current working directory because callers (tests,
    one-off scripts, MCP servers spawned by other processes) frequently
    start from elsewhere on the filesystem and would otherwise see
    ``FileNotFoundError`` from Alembic.

    Raises :class:`FileNotFoundError` if no suitable file is found —
    that almost always means the package was installed without its
    sibling ``migrations/`` directory, which is a packaging bug worth
    surfacing loudly.
    """
    here = Path(__file__).resolve()
    # Walk up looking for ``alembic.ini``. The repo layout puts
    # ``alembic.ini`` at ``<repo_root>/alembic.ini`` and this module at
    # ``<repo_root>/ctxr/fsm/sqlite/project.py``, so four parents up is
    # the typical answer; we still walk to keep the search robust to
    # alternative install layouts.
    for candidate in (here, *here.parents):
        ini = candidate / "alembic.ini"
        if ini.is_file():
            return ini
    raise FileNotFoundError(
        "Could not locate alembic.ini for ctxr.fsm.sqlite — the package "
        "must be installed alongside its migrations/ directory."
    )


def run_migrations(db_path: Path | str) -> None:
    """Run ``alembic upgrade head`` against ``db_path``.

    Invoked at ``Project.open`` time when ``migrate=True`` and exposed
    publicly so deployment scripts can run migrations standalone (e.g.
    before booting an MCP server pointed at the same database).

    The function sets ``CTXR_FSM_DB_URL`` in the process environment
    for the duration of the call so :mod:`migrations.env` (which
    already reads that var) targets ``db_path`` regardless of what
    ``alembic.ini`` declares as ``sqlalchemy.url``. The previous value
    is restored on exit so we do not leak state into the parent process.
    """
    db_path = Path(db_path)
    # Ensure the parent directory exists before Alembic tries to open
    # the file — ``upgrade`` opens the SQLite database directly and
    # SQLite refuses to create the parent for us.
    db_path.parent.mkdir(parents=True, exist_ok=True)

    ini_path = _find_alembic_ini()
    config = AlembicConfig(str(ini_path))
    # Point at the resolved absolute path so Alembic's
    # ``_sqlite_path_from_url`` helper in env.py sees an absolute
    # location instead of resolving against alembic.ini's directory.
    db_url = f"sqlite:///{db_path}"
    config.set_main_option("sqlalchemy.url", db_url)

    previous = os.environ.get("CTXR_FSM_DB_URL")
    os.environ["CTXR_FSM_DB_URL"] = db_url
    try:
        alembic_command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("CTXR_FSM_DB_URL", None)
        else:
            os.environ["CTXR_FSM_DB_URL"] = previous


class Project:
    """The public entry point for the W2 SQLite substrate.

    A ``Project`` instance owns one SQLAlchemy ``Engine``, one
    ``sessionmaker`` bound to that engine, and one instance of every
    sub-repository. Construction is via :meth:`open` (or, in tests,
    :meth:`from_engine`); direct instantiation through the constructor
    is also supported but most callers prefer the classmethods because
    they handle the engine wiring.

    Lifecycle:

    1. :meth:`open` (or :meth:`from_engine`) creates the engine and
       wires every sub-repo onto ``self``.
    2. The composition root publishes the project's sessionmaker via
       :func:`set_active_session_factory` so ``@atomic``-decorated
       functions can find it. ``open`` does this automatically.
    3. Callers use the sub-repos directly (``proj.runs.get(session,
       ...)``) or the convenience methods (:meth:`register_spec`,
       :meth:`start_run`, :meth:`subscribe`).
    4. :meth:`close` disposes the engine. The class is also a context
       manager so ``with Project.open(...) as proj:`` works.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        engine: Engine,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        """Wire ``engine`` plus every sub-repository onto ``self``.

        Prefer :meth:`open` for the common path — it handles ``open_engine``
        plus the optional ``alembic upgrade head`` for you. The bare
        constructor exists so tests and advanced callers can inject a
        pre-built engine (e.g. an in-memory SQLite engine with the schema
        already applied).

        ``session_factory`` defaults to ``get_session_factory(engine)`` —
        we accept an override so tests can swap in a sessionmaker
        configured with non-default flags (autoflush on, expire-on-commit,
        etc.) without monkey-patching.
        """
        self._engine: Engine = engine
        self._session_factory: sessionmaker[Session] = (
            session_factory if session_factory is not None else get_session_factory(engine)
        )

        # The active sessionmaker is published on the context-var so
        # @atomic-decorated callables find it. We remember the previous
        # binding so close() can restore it — this matters when nested
        # Projects are used in tests.
        self._previous_factory_token = set_active_session_factory(self._session_factory)

        # ── Core lifecycle repos ──────────────────────────────────────
        self.projects = ProjectsRepo()
        self.specs = SpecsRepo()
        self.runs = RunsRepo()
        self.run_sessions = RunSessionsRepo()

        # ── State-tree repos ──────────────────────────────────────────
        self.states = StatesRepo()
        self.transitions = TransitionsRepo()
        self.worker_artifacts = WorkerArtifactsRepo()
        self.aggregates = AggregatesRepo()

        # ── Concurrency control + journal ─────────────────────────────
        self.locks = LocksRepo()
        self.journal = JournalRepo()

        # ── Enforcement substrate ─────────────────────────────────────
        self.commit_signatures = CommitSignaturesRepo()
        self.commit_tokens = CommitTokensRepo()
        self.tool_calls = ToolCallsRepo()
        self.drift_signals = DriftSignalsRepo()

        # ── Event bus ─────────────────────────────────────────────────
        self.producers = ProducersRepo()
        self.consumers = ConsumersRepo()
        self.events = EventsRepo()
        self.event_deliveries = EventDeliveriesRepo()

        # ── W23g cross-FSM gates ──────────────────────────────────────
        # Persistence half of the cross-FSM gate substrate. Records one
        # row per resolved gate (`run_output` source kind populates
        # source_run_id; `llm_supplied` leaves it NULL). See
        # ``ctxr/fsm/memory/GATE_CONTRACT.md`` for the protocol; the
        # repo itself is stateless and reused across requests.
        self.gates = GatesRepo()

        # Cache the engine producer's id lazily — many openings of a
        # Project will never emit events at all (e.g. read-only tools),
        # so we avoid the round-trip until ``start_run`` (or any other
        # caller) actually needs it.
        self._runtime_producer_id: str | None = None

        # Track close state so accidental double-close doesn't try to
        # dispose an already-disposed engine.
        self._closed: bool = False

    @classmethod
    def open(
        cls,
        db_path: Path | str,
        *,
        migrate: bool = True,
        echo: bool = False,
    ) -> Project:
        """Open a project bound to ``db_path``, optionally migrating it.

        This is the canonical constructor — almost all callers should
        use it rather than the bare ``__init__``.

        ``migrate=True`` (default) runs ``alembic upgrade head`` against
        ``db_path`` before the engine is bound. The migration step uses
        the project's own :func:`open_engine` under the hood (see
        :mod:`migrations.env`) so the same PRAGMAs apply during the
        upgrade as during steady-state operation.

        ``echo=True`` forwards to :func:`open_engine`; SQLAlchemy will
        log every statement to stdout. Useful for "what query is the
        engine running" debugging during tests.
        """
        if migrate:
            # Migrations open and close their own engine; we run them
            # before constructing ours so the schema is in place by the
            # time the project's session factory is published.
            run_migrations(db_path)

        engine = open_engine(db_path, echo=echo)
        return cls(engine)

    @classmethod
    def from_engine(
        cls,
        engine: Engine,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> Project:
        """Construct a project from a pre-built engine.

        Test fixtures that maintain their own in-memory engine (e.g.
        ``sqlite+pysqlite:///:memory:`` with the schema applied via
        ``SQLModel.metadata.create_all``) call this to skip the
        migration step entirely.
        """
        return cls(engine, session_factory=session_factory)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def engine(self) -> Engine:
        """The underlying SQLAlchemy engine.

        Exposed so callers that need to run raw ``text(...)`` queries
        or attach their own ``event.listens_for`` listeners can do so
        without reaching into ``self._engine``.
        """
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """The sessionmaker bound to ``self.engine``.

        Used by ``@atomic``-decorated functions via the context-var
        plumbing in :mod:`ctxr.fsm.sqlite.transactions`, but also
        available directly for callers that prefer to open sessions
        explicitly (``with proj.session_factory() as session: ...``).
        """
        return self._session_factory

    # ------------------------------------------------------------------
    # Convenience operations
    # ------------------------------------------------------------------

    def register_spec(
        self,
        spec: FsmSpec,
        *,
        project_slug: str = "default",
    ) -> SpecRegistered:
        """Register ``spec`` under the project identified by ``project_slug``.

        The flow:

        1. Look up the project by slug; create it on first use so the
           call works for blank databases without a separate
           ``create_project`` step.
        2. Delegate to :meth:`SpecsRepo.register` for the actual
           insert-or-dedupe logic.

        Returns the :class:`SpecRegistered` envelope produced by the
        repository — callers can inspect ``.created`` to tell whether
        a new version was minted or an existing one was matched.
        """
        with self._session_factory() as session, session.begin():
            project = self.projects.get_by_slug(session, project_slug)
            if project is None:
                project = self.projects.create(session, slug=project_slug)
            result = self.specs.register(
                session, spec=spec, project_id=project.id
            )
        return result

    def start_run(
        self,
        spec_id: str,
        args: dict[str, Any] | None = None,
    ) -> Run:
        """Start a new run against the registered spec ``spec_id``.

        Atomic semantics: the run row, the runtime-producer upsert, and
        the ``run_started`` event are all inserted inside one
        ``Session.begin()`` block so a crash between any two of them
        leaves the database in a consistent state (either every row is
        visible or none are).

        Returns the freshly-created :class:`Run` value-object. The
        caller is expected to then drive the engine (in W3+) using the
        ``run.id`` it pulls from this return value.

        Raises :class:`LookupError` when ``spec_id`` does not point at a
        registered spec — we refuse to create a run with no associated
        FSM definition because the engine would have nothing to load.
        """
        with self._session_factory() as session, session.begin():
            spec = self.specs.get(session, spec_id)
            if spec is None:
                raise LookupError(
                    f"cannot start run: no registered spec with id {spec_id!r}"
                )

            # Insert the run row. The repo seeds ``status='in_progress'``
            # and stamps the timestamps for us; ``fsm_spec_hash`` is the
            # hash observed at run start, used later by the drift
            # detector when the run resumes.
            run = self.runs.create(
                session,
                project_id=spec.project_id,
                spec_id=spec.id,
                args=args or {},
                fsm_spec_hash=spec.hash,
            )

            # Ensure the engine producer exists. We cache the id on
            # ``self`` so subsequent ``start_run`` calls skip the
            # round-trip; the underlying repo's ``upsert`` is also
            # idempotent so a stale cache (e.g. after a process
            # restart) cannot cause duplicate rows.
            producer = self.producers.upsert(
                session,
                kind=_RUNTIME_PRODUCER_KIND,
                name=_RUNTIME_PRODUCER_NAME,
            )
            self._runtime_producer_id = producer.id

            # Emit the canonical "run started" event. The bus
            # fan-out happens inside ``emit``, so any consumer
            # registered with a matching filter sees this event on
            # its next poll.
            self.events.emit(
                session,
                producer_id=producer.id,
                kind=EventKind.run_started.value,
                payload={
                    "run_id": run.id,
                    "spec_id": spec.id,
                    "spec_hash": spec.hash,
                    "entry_state": spec.definition.get("entry"),
                },
                run_id=run.id,
            )

        return run

    def get_run(self, run_id: str) -> Run | None:
        """Return the run with the given id, or ``None`` if it is unknown.

        Thin pass-through to :meth:`RunsRepo.get`; exists on the facade
        because it is by far the most common read operation and the
        CLI / MCP layers should not have to remember which sub-repo
        owns it.
        """
        with self._session_factory() as session:
            return self.runs.get(session, run_id)

    def subscribe(
        self,
        consumer_name: str,
        kinds: list[EventKind] | None = None,
        filter_run_id: str | None = None,
        *,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        stop_after: float | None = None,
    ) -> Iterator[Event]:
        """Yield events as they appear on the bus.

        ``consumer_name`` is the durable identity for this subscription;
        the bus tracks a per-consumer cursor (``last_seen_seq`` from
        the events table) plus per-event delivery rows so a long-lived
        consumer reconnecting later picks up exactly where it left off.

        ``kinds`` filters to the supplied :class:`EventKind` members; a
        ``None`` value (the default) yields every kind.

        ``filter_run_id`` scopes the subscription to a single run.
        Particularly useful for the CLI's ``fsm run tail`` command which
        wants the journal for a single run and nothing else.

        ``poll_interval_seconds`` controls how often the bus is polled
        for new events; the default of 250 ms is the sweet spot for
        interactive use.

        ``stop_after``, when set, is a wall-clock duration (in seconds)
        after which the iterator stops polling and returns. ``None``
        (the default) means "poll forever" — the caller is expected to
        ``break`` out of the loop when they have what they need. A
        bounded timeout is mostly useful in tests, where we want a
        subscription that does not hang the suite if no event arrives.

        Implementation notes
        --------------------

        We register / refresh the consumer once (so re-running
        ``subscribe`` with the same ``consumer_name`` rebinds the
        filter, which is also the bus contract). On each poll cycle we
        pull every pending delivery for the consumer, yield the
        underlying events one at a time, and ack each as it is yielded
        — this is at-least-once semantics: if the caller crashes
        mid-iteration, the un-acked rows will be redelivered on the
        next ``subscribe`` call with the same consumer_name.
        """
        # Register the consumer up front so the bus knows our filters
        # before the first event arrives. Re-registering is idempotent
        # at the (kind, name) granularity — see ConsumersRepo.register.
        kind_strings: list[str] | None = (
            [k.value for k in kinds] if kinds is not None else None
        )
        with self._session_factory() as session, session.begin():
            consumer = self.consumers.register(
                session,
                kind="subscriber",
                name=consumer_name,
                filter_kind=kind_strings,
                filter_run_id=filter_run_id,
            )
        consumer_id = consumer.id

        deadline = (time.monotonic() + stop_after) if stop_after is not None else None

        while True:
            # Pull a batch of pending deliveries for this consumer.
            # ``pending_for`` returns ``EventWithDelivery`` rows ordered
            # by ``EventTable.created_at ASC`` so the consumer sees
            # events in producer-emit order.
            with self._session_factory() as session, session.begin():
                pending = self.event_deliveries.pending_for(
                    session, consumer_id=consumer_id
                )
                if pending:
                    # Mark delivered + ack inside the same txn so a
                    # crash between yield and ack still re-delivers
                    # the row on the next poll (at-least-once).
                    for ewd in pending:
                        self.event_deliveries.mark_delivered(
                            session,
                            event_id=ewd.event.id,
                            consumer_id=consumer_id,
                        )
                        self.event_deliveries.ack(
                            session,
                            event_id=ewd.event.id,
                            consumer_id=consumer_id,
                        )
                    self.consumers.touch_last_seen(session, consumer_id)

            for ewd in pending:
                # Re-shape the bus-side ``Event`` into the lifecycle-
                # repo ``Event`` so consumers see one type regardless
                # of which repo produced it. The two share a column
                # layout so the projection is mechanical.
                yield Event(
                    id=ewd.event.id,
                    run_id=ewd.event.run_id,
                    kind=ewd.event.kind,
                    producer_id=ewd.event.producer_id,
                    payload=ewd.event.payload,
                    created_at=ewd.event.created_at,
                    seq=ewd.event.seq,
                )

            if deadline is not None and time.monotonic() >= deadline:
                return

            # Quiet path: nothing new this cycle, take a nap. We avoid
            # ``time.sleep(0)`` because it busy-loops; the configured
            # interval is always > 0 in practice.
            time.sleep(max(0.0, poll_interval_seconds))

    def transaction(self, *, run_id: str) -> TransactionContext:
        """Return a :class:`TransactionContext` bound to this project.

        Convenience wrapper so callers can write
        ``with proj.transaction(run_id=...) as ctx:`` instead of
        importing :class:`TransactionContext` and threading the engine
        through themselves.
        """
        return TransactionContext(self._engine, run_id=run_id)

    def commit_and_advance(
        self,
        *,
        run_id: str,
        outputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Persistence-complete worker-output commit + engine advance.

        The Python-direct equivalent of MCP's ``fsm.commit_outputs`` +
        ``fsm.confirm_commit`` pair (W14k). Drives the engine through
        the current state's worker output, persists every state /
        transition / event row the MCP path persists, walks any
        following inline-state chain server-side via the W14a
        ``execute_inline`` helper, and updates ``runs.status`` /
        ``runs.current_state`` so the UI + REST API see a coherent
        view of the run.

        Why this exists: drivers that talk to the engine via the
        Python facade (test harnesses, in-process consumers, the W14h
        cycle battery) need the same DB hygiene MCP gives over the
        wire. Previously they had to either reimplement the
        persistence calls (error-prone) or accept that the runs/states/
        transitions/events tables stayed empty (UI showed no data).
        This method closes that gap.

        Parameters
        ----------
        run_id:
            The run's primary key (UUIDv7 string) returned by
            :meth:`start_run`.
        outputs:
            The worker's structured output dict for the current state.
            Validated against the state's ``response_schema`` if one
            is declared.

        Returns
        -------
        dict[str, Any]
            ``{"result_kind": "advance"|"terminal"|"fault",
               "next_state_id": str|None,
               "next_brief": dict|None,
               "chain_length": int}`` — the same shape the MCP path's
            replay produces. ``chain_length`` reports how many inline
            states the engine walked after the worker commit.

        See also
        --------
        :func:`ctxr.fsm.mcp.tools_runs._drive_inline_chain` — the
        canonical inline-chain walker this method shares.
        """

        # Lazy import: tools_runs imports the engine + repos heavily;
        # importing it at module top would couple ``Project`` to the
        # MCP layer, which violates the layer-dependency rule. The
        # MCP layer can import the Project facade, never the other
        # way around. Localising the import here keeps the dependency
        # graph one-way while still letting us share the well-tested
        # inline-driver implementation.
        import uuid as _uuid

        from ctxr.fsm.core import RunCtx
        from ctxr.fsm.core.engine import advance as engine_advance
        from ctxr.fsm.core.models import (
            EngineAdvanceKind,
            EventKind,
            FsmSpec,
            TransitionKind,
        )
        from ctxr.fsm.mcp.tools_runs import (
            _drive_inline_chain,
            _ensure_engine_producer,
            _persist_state_entry,
        )

        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run_id: {run_id!r}")

        with self.session_factory() as session:
            registered = self.specs.get(session, run.fsm_spec_id)
        if registered is None:
            raise ValueError(
                f"spec {run.fsm_spec_id!r} not registered for run {run_id!r}"
            )
        spec: FsmSpec = FsmSpec.model_validate(registered.definition)

        producer_id = _ensure_engine_producer(self)

        # Lazy entry-state persistence: ``Project.start_run`` leaves
        # ``runs.current_state`` as None for backward compatibility
        # with the MCP layer (which does its own ``_persist_state_entry``
        # right after start_run). When ``commit_and_advance`` is the
        # first thing that touches the run after ``start_run``, we
        # persist the entry-state row here so the rest of the method
        # has a valid ``current_state`` + an open state-entry PK to
        # work with. Idempotent for runs whose current_state is
        # already set (i.e., MCP-driven runs that already persisted).
        current_state_id = run.current_state
        if current_state_id is None:
            entry_state_id = registered.definition.get("entry")
            if not entry_state_id:
                raise ValueError(
                    f"spec {registered.id!r} has no entry state declared"
                )
            entry_state_obj = spec.get_state(entry_state_id)
            entry_inputs: dict[str, Any] = {}
            entry_worker = entry_state_obj.worker or (
                entry_state_obj.loop.worker
                if entry_state_obj.loop is not None
                else None
            )
            if entry_worker is not None:
                entry_inputs = {
                    name: (run.args or {}).get(name) for name in entry_worker.inputs
                }
            _persist_state_entry(
                self,
                run_id=run_id,
                state_id=entry_state_id,
                inputs=entry_inputs,
                producer_id=producer_id,
            )
            # Re-fetch so the rest of the method sees current_state set.
            run = self.get_run(run_id)
            assert run is not None and run.current_state == entry_state_id
            current_state_id = entry_state_id

        # Find the open state-entry PK for the current state (the row
        # start_run / a prior commit_and_advance created).
        with self.session_factory() as session:
            entries = self.states.list_by_run(session, run_id)
        from_state_pk: str | None = None
        for entry in reversed(entries):
            if entry.state_id == current_state_id and entry.status == "entered":
                from_state_pk = entry.id
                break

        # Build the RunCtx the engine expects, threading the run's args
        # through so transition predicates can read them.
        run_ctx = RunCtx(
            run_id=_uuid.UUID(run_id),
            fsm_id=spec.id,
            current_state=current_state_id,
            env=dict(run.args or {}),
        )

        advance_result = engine_advance(spec, run_ctx, outputs)
        result_kind = advance_result.kind.value

        if advance_result.kind is EngineAdvanceKind.fault:
            # Fault path: surface to caller without further persistence
            # state mutations. The engine's contract guarantees no
            # partial advance on fault.
            return {
                "result_kind": result_kind,
                "next_state_id": current_state_id,
                "next_brief": None,
                "chain_length": 0,
                "fault_reason": advance_result.reason,
            }

        # Mark the worker state's row as exited + emit state_exited
        # + (if advancing) record the transition + persist the next
        # state's entry row + emit transition_taken + state_entered.
        # The transaction shape mirrors what MCP confirm_commit does.
        if from_state_pk is not None:
            with self.session_factory() as session, session.begin():
                self.states.mark_exited(session, from_state_pk, outputs)
                self.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=EventKind.state_exited.value,
                    payload={"run_id": run_id, "state_pk": from_state_pk},
                    run_id=run_id,
                )

        if advance_result.kind is EngineAdvanceKind.terminal:
            from ctxr.fsm.sqlite.models_core import RunTable
            from ctxr.fsm.sqlite.repos_core import _iso_now_ms

            verdict_val = (
                {**(run.args or {}), **outputs}.get("verdict")
            )
            with self.session_factory() as session, session.begin():
                run_row = session.get(RunTable, run_id)
                if run_row is not None:
                    run_row.status = "completed"
                    run_row.ended_at = _iso_now_ms()
                    run_row.last_update_at = run_row.ended_at
                    if verdict_val is not None:
                        run_row.verdict = str(verdict_val)
                    session.add(run_row)
                self.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=EventKind.run_completed.value,
                    payload={"run_id": run_id, "verdict": verdict_val},
                    run_id=run_id,
                )
            return {
                "result_kind": result_kind,
                "next_state_id": None,
                "next_brief": None,
                "chain_length": 0,
            }

        # EngineAdvanceKind.advance: record transition + persist new entry.
        next_state_id = advance_result.next_state
        assert next_state_id is not None, "advance result must carry next_state"
        # ``from_state_pk`` was set unless the run is in a malformed
        # state — e.g. a state row went missing between start_run and
        # the first commit_and_advance. We require it for the
        # transition row's FK, so surface the malformed-state case
        # rather than letting the type checker carry an Optional all
        # the way into the repo.
        assert from_state_pk is not None, (
            f"no open state-entry row found for {current_state_id!r}"
        )
        next_state = spec.get_state(next_state_id)
        next_inputs: dict[str, Any] = {}
        worker = next_state.worker or (
            next_state.loop.worker if next_state.loop is not None else None
        )
        if worker is not None:
            env_after = {**(run.args or {}), **outputs}
            next_inputs = {name: env_after.get(name) for name in worker.inputs}

        with self.session_factory() as session, session.begin():
            transition_eval = next(
                iter(advance_result.evaluations or []), None
            )
            tkind = (
                transition_eval.kind
                if transition_eval is not None and transition_eval.kind is not None
                else TransitionKind.always.value
            )
            self.transitions.create(
                session,
                run_id=run_id,
                from_state_pk=from_state_pk,
                to_state_id=next_state_id,
                kind=tkind,
                predicate=transition_eval.expression if transition_eval else None,
                predicate_result=transition_eval.result if transition_eval else None,
            )
            self.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.transition_taken.value,
                payload={
                    "run_id": run_id,
                    "from_state_pk": from_state_pk,
                    "to_state_id": next_state_id,
                    "kind": tkind,
                },
                run_id=run_id,
            )

        _persist_state_entry(
            self,
            run_id=run_id,
            state_id=next_state_id,
            inputs=next_inputs,
            producer_id=producer_id,
        )

        # Walk any following inline-state chain server-side. The W14k
        # inline-driver handles persistence + terminal detection for
        # us, so a chain that ends at terminal correctly flips
        # runs.status to completed without further work here.
        env_for_chain = {**(run.args or {}), **outputs}
        inline_result = _drive_inline_chain(
            self,
            spec=spec,
            run_id=run_id,
            producer_id=producer_id,
            start_state_id=next_state_id,
            env_at_entry=env_for_chain,
        )

        if inline_result.get("chain_length", 0) > 0:
            return {
                "result_kind": inline_result["result_kind"],
                "next_state_id": inline_result["next_state_id"],
                "next_brief": inline_result.get("next_brief"),
                "chain_length": inline_result["chain_length"],
            }

        # Worker-direct-to-terminal: the engine reports the transition
        # INTO a terminal-shaped state as ``advance`` (not ``terminal``)
        # because ``EngineAdvanceKind.terminal`` is computed from the
        # CURRENT state's body. When we have just landed at a state
        # with no body and no transitions, finalise the run here.
        # Mirrors the same fallback the inline-chain driver applies at
        # the end of a chain. Skips when an inline chain already ran
        # (it does its own terminal detection).
        from ctxr.fsm.core.models import StateKind as _StateKind
        from ctxr.fsm.sqlite.models_core import RunTable
        from ctxr.fsm.sqlite.repos_core import _iso_now_ms

        landed_state = spec.get_state(next_state_id)
        landed_is_terminal = landed_state.kind is _StateKind.terminal or (
            not landed_state.transitions
            and landed_state.worker is None
            and landed_state.loop is None
            and landed_state.inline is None
        )
        if landed_is_terminal:
            verdict_val = env_for_chain.get("verdict")
            with self.session_factory() as session, session.begin():
                run_row = session.get(RunTable, run_id)
                if run_row is not None:
                    run_row.status = "completed"
                    run_row.ended_at = _iso_now_ms()
                    run_row.last_update_at = run_row.ended_at
                    if verdict_val is not None:
                        run_row.verdict = str(verdict_val)
                    session.add(run_row)
                self.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=EventKind.run_completed.value,
                    payload={"run_id": run_id, "verdict": verdict_val},
                    run_id=run_id,
                )
            return {
                "result_kind": EngineAdvanceKind.terminal.value,
                "next_state_id": next_state_id,
                "next_brief": None,
                "chain_length": 0,
            }

        return {
            "result_kind": result_kind,
            "next_state_id": next_state_id,
            "next_brief": None,
            "chain_length": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Dispose the engine and unbind the sessionmaker.

        Idempotent: calling close on an already-closed project is a no-op.
        We deliberately do NOT raise here because tests frequently call
        close from a finally block and from ``__exit__``; raising would
        mask the test's real failure.
        """
        if self._closed:
            return
        self._closed = True

        # Restore the previous sessionmaker binding before disposing the
        # engine so any in-flight @atomic call does not race a half-torn-
        # down state.
        with contextlib.suppress(LookupError, ValueError):
            # ``ContextVar.reset`` raises LookupError if the token came
            # from a different context — defensively suppress so close
            # works from a thread that didn't create the project.
            from ctxr.fsm.sqlite.transactions import _active_session_factory

            _active_session_factory.reset(self._previous_factory_token)

        self._engine.dispose()

    def __enter__(self) -> Project:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Returning None / False propagates any in-flight exception; we
        # never swallow.
        self.close()
