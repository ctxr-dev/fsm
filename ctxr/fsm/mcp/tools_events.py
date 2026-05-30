"""Event-bus, journal-inspection, and registry MCP tools.

This module owns the *observability slice* of the W4 plumbing wave:
clients use these tools to tail events emitted by the FSM runtime,
peek at the journal's pre-commit ledger, recover a half-written txn,
and enumerate the bus's producers / consumers. Every tool here is
read-mostly — :func:`recover_journal` is the only mutator, and even it
only acts on a single journal-txn row whose lifecycle the engine has
abandoned.

Tool surface
------------

* ``fsm.subscribe_events`` — long-polling event subscription. The MCP
  SDK's tool calls are unary (request → single response) so we do not
  stream; instead each call registers / refreshes the consumer, waits
  up to ``timeout_seconds`` for new events, and returns a batch. The
  follow-up call uses the same ``consumer_name`` (idempotent
  registration) and naturally resumes where the previous call left off
  because every yielded event is ack'd before it is returned.
* ``fsm.inspect_journal`` — a flat read of
  :meth:`JournalRepo.inspect`; returns the newest unfinalised txn for
  a run (or ``null`` if the run is quiescent).
* ``fsm.recover_journal`` — operator hatch for the case where the
  engine crashed mid-commit: ``"discard"`` drops the staged writes
  (effective rollback) and ``"replay"`` re-marks the txn finalised
  (effective roll-forward). W4 is plumbing only — we do NOT yet
  attempt to materialise the staged writes, just transition the txn
  lifecycle so the engine on next boot sees a clean slate.
* ``fsm.list_consumers`` / ``fsm.list_producers`` — registry dumps
  for the dev-loop and for any UI that wants to render the bus
  topology.

Stdout discipline
-----------------
MCP stdio uses **stdout** for the JSON-RPC framing — every log line
in this module goes to stderr via the module-level :data:`_LOG`
(``logging`` is configured stderr-only by
:func:`ctxr.fsm.mcp.server._configure_stderr_logging`). Tool bodies
NEVER ``print()``.

Error contract
--------------
Each tool body is wrapped in ``try/except``: on failure we return an
:class:`McpToolError` envelope (the legacy JS contract) rather than
letting the exception escape to FastMCP — propagation would surface
as a JSON-RPC error frame, which is a different code path older
clients did not have to handle. The success-vs-error discriminator
is the presence of the ``"error"`` key in the structured tool
output (FastMCP wraps both arms of a ``Union[...]`` return type
under ``{"result": ...}`` — clients branch on
``result.error is not None``).

Enforcement note
----------------
W4 is the *plumbing* wave. These tools surface the bus and journal
substrate; they do NOT yet impose the hard W12 invariants (token
lifetimes, consumer auth, recovery-policy quorums). Wiring is
exhaustive; enforcement is deferred so the schema can be exercised
before it is locked down.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ctxr.fsm.core.models import JournalStatus
from ctxr.fsm.mcp import mcp
from ctxr.fsm.mcp._drain_decorator import drain_aware
from ctxr.fsm.mcp._errors import McpToolError, as_error
from ctxr.fsm.mcp._state import get_project
from ctxr.fsm.mcp.tools_runs import JournalAction
from ctxr.fsm.sqlite.repos_events import Consumer, Event, Producer
from ctxr.fsm.sqlite.repos_locks_journal import JournalTxn

__all__ = [
    "EventBatch",
    "JournalRecovered",
    "JournalState",
    "inspect_journal",
    "list_consumers",
    "list_producers",
    "recover_journal",
    "subscribe_events",
]


# Logger pinned to this module's name so the journal of a running
# server can be grep'd for ``ctxr.fsm.mcp.tools_events`` to isolate
# the bus-observability path from the rest of the server chatter.
_LOG = logging.getLogger("ctxr.fsm.mcp.tools_events")


# Subscription poll cadence inside :func:`subscribe_events`. We avoid
# importing the SQLite-layer default (``DEFAULT_POLL_INTERVAL_SECONDS``)
# directly because this is a different decision: the MCP long-poll
# trades a slightly slower cadence for cheaper SQLite hits, since the
# total wait is bounded by the caller-supplied ``timeout_seconds``.
_SUBSCRIBE_POLL_INTERVAL_SECONDS: float = 0.1

# Default ``consumer.kind`` for subscriptions registered through MCP.
# Distinct from the engine's own ``"engine"`` / ``"verifier"`` kinds so
# operators can tell "this consumer is an MCP client tailing events"
# apart from "this consumer is part of the FSM runtime" in
# :func:`list_consumers` dumps.
_MCP_CONSUMER_KIND: str = "mcp_subscriber"


# Soft cap on ``max_events`` so a misconfigured client cannot ask for
# a million rows in one round-trip. The cap is generous (1000) — the
# typical UI-side tail asks for 50 — but bounded so the server's
# memory footprint per call is predictable.
_MAX_EVENTS_HARD_CAP: int = 1000


# ---------------------------------------------------------------------------
# Tool input / output models
# ---------------------------------------------------------------------------


class EventBatch(BaseModel):
    """A batch of events returned by :func:`subscribe_events`.

    Attributes
    ----------
    events:
        Events delivered in this poll cycle, in producer-emit order
        (``EventTable.created_at`` ascending, which on UUIDv7-keyed
        rows is also the insertion order). Empty list when the
        long-poll timed out with nothing to deliver — that is a
        normal "still alive" response, not an error.
    next_cursor:
        Opaque cursor for the next call. Currently always ``None``
        because the underlying ``EventDeliveriesRepo`` maintains the
        per-consumer cursor server-side via the delivery rows; the
        field is reserved so a future revision can add an explicit
        client-held cursor without breaking the wire contract.
    """

    model_config = ConfigDict(frozen=True)

    events: list[Event] = Field(default_factory=list)
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Reserved for future use; the bus tracks the cursor "
            "server-side via per-consumer delivery rows so callers "
            "do not need to thread one through today."
        ),
    )


class JournalState(BaseModel):
    """The journal-inspection view returned by :func:`inspect_journal`.

    The repo's :meth:`JournalRepo.inspect` already returns ``None``
    when there is no unfinalised txn, but the MCP wire contract
    prefers a structured shape over a literal null so clients can
    branch on a field rather than on the absence of a value.

    Attributes
    ----------
    run_id:
        The run whose journal was inspected, echoed back for
        correlation when a UI dispatches many inspect calls in
        parallel.
    txn:
        The newest unfinalised journal txn, or ``None`` if the run
        is quiescent (every prior txn already finalised, or no txn
        ever opened).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    txn: JournalTxn | None = None


class JournalRecovered(BaseModel):
    """The structured outcome of a :func:`recover_journal` call.

    Attributes
    ----------
    run_id:
        Echo of the input ``run_id`` so a client dispatching a fleet
        of recovery calls can route the response without keeping
        per-request state.
    action:
        The recovery action that was applied — ``"discard"`` deletes
        the staged-write ledger row (effective rollback) and
        ``"replay"`` re-marks the txn ``finalised`` (effective
        roll-forward).
    txn_id:
        The journal-txn id that was acted upon, or ``None`` when
        the run had no unfinalised txn to recover (in which case
        the call is a structured no-op rather than an error).
    previous_status:
        The status of the txn *before* the recovery action ran. A
        sanity-check field for the audit trail; the engine's
        recovery policy logs this on every successful recovery.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    action: JournalAction
    txn_id: str | None = None
    previous_status: JournalStatus | None = None


# ---------------------------------------------------------------------------
# fsm.subscribe_events
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fsm.subscribe_events",
    description=(
        "Long-poll the FSM event bus for up to `max_events` events "
        "within `timeout_seconds`. The same `consumer_name` resumes "
        "where the previous call stopped — registration is idempotent."
    ),
)
@drain_aware
def subscribe_events(
    consumer_name: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Durable identity for this subscription. Re-using the "
                "same name on a subsequent call resumes the cursor."
            ),
        ),
    ],
    kinds: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Optional list of `EventKind` string values to filter "
                "on. None means deliver every kind."
            ),
        ),
    ] = None,
    filter_run_id: Annotated[
        UUID | None,
        Field(
            default=None,
            description=(
                "Optional run-id to scope the subscription to a single "
                "run. None means every run."
            ),
        ),
    ] = None,
    max_events: Annotated[
        int,
        Field(
            ge=1,
            le=_MAX_EVENTS_HARD_CAP,
            description=(
                "Maximum number of events to return in this batch. "
                "Hard-capped server-side."
            ),
        ),
    ] = 50,
    timeout_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            le=60.0,
            description=(
                "Maximum wall-clock seconds the server will wait for "
                "events to appear before returning an empty batch."
            ),
        ),
    ] = 5.0,
) -> EventBatch | McpToolError:
    """Long-poll for bus events, returning at most ``max_events``.

    On the first call we register (or refresh) the consumer with the
    bus; subsequent calls with the same ``consumer_name`` are
    idempotent on the consumer row and resume the cursor because every
    delivered event is ack'd before being returned.

    Returns an empty :class:`EventBatch` on timeout — that's a
    structured "still alive, nothing new" response and is NOT an
    error.
    """
    try:
        project = get_project()
        filter_run_id_str = str(filter_run_id) if filter_run_id is not None else None

        # Register / refresh the consumer up front so the bus knows
        # our filters before the first poll. Re-registering is
        # idempotent at (kind, name) and updates the filters in
        # place — see ConsumersRepo.register.
        with project.session_factory() as session, session.begin():
            consumer = project.consumers.register(
                session,
                kind=_MCP_CONSUMER_KIND,
                name=consumer_name,
                filter_kind=kinds,
                filter_run_id=filter_run_id_str,
            )
        consumer_id = consumer.id

        deadline = time.monotonic() + timeout_seconds
        collected: list[Event] = []

        # Long-poll loop. Each cycle pulls a bounded batch, ack's
        # every row inside the same txn (at-least-once semantics —
        # a crash between pull and ack would re-deliver on the next
        # call), and either returns immediately if we hit the cap or
        # naps until the next cycle.
        while True:
            remaining = max_events - len(collected)
            if remaining <= 0:
                break

            with project.session_factory() as session, session.begin():
                pending = project.event_deliveries.pending_for(
                    session,
                    consumer_id=consumer_id,
                    limit=remaining,
                )
                if pending:
                    for ewd in pending:
                        project.event_deliveries.mark_delivered(
                            session,
                            event_id=ewd.event.id,
                            consumer_id=consumer_id,
                        )
                        project.event_deliveries.ack(
                            session,
                            event_id=ewd.event.id,
                            consumer_id=consumer_id,
                        )
                    project.consumers.touch_last_seen(session, consumer_id)

            # Project the bus-side EventWithDelivery rows down to the
            # plain Event value-object the client cares about. Doing
            # the projection outside the txn keeps the transaction
            # short.
            for ewd in pending:
                collected.append(ewd.event)

            if collected:
                # First non-empty cycle wins: return immediately rather
                # than waiting for the full timeout. This matches the
                # "long-poll" idiom — the timeout is a *ceiling* on how
                # long the server may wait, not a fixed delay.
                break

            now = time.monotonic()
            if now >= deadline:
                # Timed out with nothing to deliver — structured empty
                # batch is the contract, not an error.
                break

            # Take a short nap, but never sleep past the deadline so
            # the response is bounded by ``timeout_seconds`` even
            # when the configured poll interval would overshoot.
            nap = min(_SUBSCRIBE_POLL_INTERVAL_SECONDS, deadline - now)
            if nap > 0:
                time.sleep(nap)

        _LOG.debug(
            "subscribe_events consumer=%s delivered=%d",
            consumer_name,
            len(collected),
        )
        return EventBatch(events=collected, next_cursor=None)
    except KeyboardInterrupt:
        # Never swallow KeyboardInterrupt — let the server's signal
        # handler shut down cleanly.
        raise
    except Exception as exc:
        _LOG.exception("subscribe_events failed for consumer %s", consumer_name)
        return as_error(
            "subscribe_events_failed",
            detail=str(exc),
            consumer_name=consumer_name,
        )


# ---------------------------------------------------------------------------
# fsm.inspect_journal
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fsm.inspect_journal",
    description=(
        "Return the newest unfinalised journal txn for the given run, "
        "or null when the run is quiescent. Read-only."
    ),
)
@drain_aware
def inspect_journal(
    run_id: Annotated[
        UUID,
        Field(description="The run whose journal you want to peek at."),
    ],
) -> JournalState | McpToolError:
    """Return the newest pending / ready-to-finalise journal txn for ``run_id``.

    Thin wrapper around :meth:`JournalRepo.inspect`. The repo returns
    ``None`` when nothing is in flight; we project that into a
    :class:`JournalState` with ``txn=None`` so the wire shape is
    consistent (clients always get an object, never a literal null
    at the tool-result level).
    """
    try:
        project = get_project()
        run_id_str = str(run_id)
        with project.session_factory() as session:
            txn = project.journal.inspect(session, run_id=run_id_str)
        return JournalState(run_id=run_id_str, txn=txn)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("inspect_journal failed for run %s", run_id)
        return as_error(
            "inspect_journal_failed",
            detail=str(exc),
            run_id=str(run_id),
        )


# ---------------------------------------------------------------------------
# fsm.recover_journal
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fsm.recover_journal",
    description=(
        "Recover the newest unfinalised journal txn for a run. "
        "`discard` rolls it back; `replay` rolls it forward to "
        "`finalised`. Returns a structured no-op if nothing is in flight."
    ),
)
@drain_aware
def recover_journal(
    run_id: Annotated[
        UUID,
        Field(description="The run whose journal needs recovering."),
    ],
    action: Annotated[
        JournalAction,
        Field(
            description=(
                "`discard` deletes the staged-write row (rollback). "
                "`replay` transitions the txn to `finalised` "
                "(roll-forward — W4 plumbing only; the engine itself "
                "is what re-materialises staged writes on next boot)."
            ),
        ),
    ],
) -> JournalRecovered | McpToolError:
    """Roll the newest unfinalised journal txn for ``run_id`` forward or back.

    The operation is best-effort + idempotent: if no unfinalised
    txn exists, we return a structured no-op (``txn_id=None``)
    rather than raising — the engine's recovery loop calls this in
    a tight loop and the absence of a recoverable row is the
    common "already clean" outcome.

    W4 caveat: ``"replay"`` only *transitions the txn status* to
    ``finalised``; it does not re-execute the staged writes. The
    next-boot engine is what actually re-materialises them. This
    keeps the plumbing wave honest about what is and is not
    enforced yet — W12 will wire the actual roll-forward
    semantics.
    """
    try:
        project = get_project()
        run_id_str = str(run_id)
        with project.session_factory() as session, session.begin():
            txn = project.journal.inspect(session, run_id=run_id_str)
            if txn is None:
                # No-op success: nothing in flight to recover.
                return JournalRecovered(
                    run_id=run_id_str,
                    action=action,
                    txn_id=None,
                    previous_status=None,
                )

            previous_status = txn.status
            if action is JournalAction.discard:
                project.journal.discard(session, txn_id=txn.id)
            else:  # action is JournalAction.replay
                # Only ``ready_to_finalise`` rows are legal to
                # roll forward — a still-``pending`` row has no
                # staged writes to finalise. We refuse with a
                # structured error rather than silently doing the
                # wrong thing.
                if txn.status is not JournalStatus.ready_to_finalise:
                    return as_error(
                        "journal_replay_not_ready",
                        detail=(
                            "Cannot replay a journal txn whose status "
                            f"is {txn.status!r}; only "
                            "'ready_to_finalise' is replayable."
                        ),
                        run_id=run_id_str,
                        txn_id=txn.id,
                        current_status=txn.status,
                    )
                project.journal.finalise(session, txn_id=txn.id)

        _LOG.info(
            "recover_journal run=%s action=%s txn=%s prev_status=%s",
            run_id_str,
            action,
            txn.id,
            previous_status,
        )
        return JournalRecovered(
            run_id=run_id_str,
            action=action,
            txn_id=txn.id,
            previous_status=previous_status,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception(
            "recover_journal failed for run %s action %s", run_id, action
        )
        return as_error(
            "recover_journal_failed",
            detail=str(exc),
            run_id=str(run_id),
            action=action,
        )


# ---------------------------------------------------------------------------
# fsm.list_consumers / fsm.list_producers
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fsm.list_consumers",
    description=(
        "Return every registered event-bus consumer (engine "
        "subscribers, MCP tail clients, verifiers, …)."
    ),
)
@drain_aware
def list_consumers() -> list[Consumer] | McpToolError:
    """Enumerate every consumer registered on the event bus.

    Ordered by ``id`` (UUIDv7) which approximates registration order.
    Returned as a plain list so the FastMCP structured-output wrapping
    is just ``{"result": [...]}``.
    """
    try:
        project = get_project()
        with project.session_factory() as session:
            return project.consumers.list(session)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("list_consumers failed")
        return as_error("list_consumers_failed", detail=str(exc))


@mcp.tool(
    name="fsm.list_producers",
    description=(
        "Return every registered event-bus producer (the FSM runtime, "
        "workers, verifiers, the CLI)."
    ),
)
@drain_aware
def list_producers() -> list[Producer] | McpToolError:
    """Enumerate every producer registered on the event bus.

    Same shape and ordering rationale as :func:`list_consumers`.
    """
    try:
        project = get_project()
        with project.session_factory() as session:
            return project.producers.list(session)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("list_producers failed")
        return as_error("list_producers_failed", detail=str(exc))
