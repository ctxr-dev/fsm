"""Integration test: ``fsm.subscribe_events`` over the real MCP stdio transport.

This test spawns ``ctxr-fsm mcp`` as a subprocess against a freshly
migrated per-test SQLite database, drives it through the official
``mcp`` Python SDK's stdio client, and asserts the two contracts the
W4 brief calls out for the subscribe-events tool:

1. A pre-populated run + event log delivered through a brand-new
   consumer name returns the matching events (filtered by ``kinds``).
2. A *second* call from the same consumer name returns only the
   *new* events emitted after the first call — the per-consumer
   cursor is advanced by the ack performed inside the tool body, so
   the same name effectively resumes where it left off.

Why a subprocess?
-----------------

The whole point of the MCP layer is the JSON-RPC framing over stdio.
Exercising the tools in-process would skip that framing entirely;
a subprocess + the SDK's :func:`stdio_client` is the only way to get
production-equivalent coverage of the boot path (database open,
project bind, logger pinning, FastMCP registration, decorator side
effects, tool dispatch).

Performance note
----------------

Spawning ``uv run ctxr-fsm mcp`` + completing the MCP initialize
handshake takes a few seconds per test. The brief calls these tests
"slower (5-15s per test)". We minimise the per-test cost by
**pre-populating the DB on the main thread** (synchronous, in-process
SQLite) and only starting the subprocess once that is done — the
subprocess then only has to handle two tool calls before being torn
down.

MCP client API used
-------------------

* :class:`mcp.ClientSession` — the high-level wrapper that owns the
  initialize handshake and ``call_tool`` dispatch.
* :class:`mcp.client.stdio.StdioServerParameters` — declarative spec
  for the subprocess (``command="uv"``, ``args=["run", "ctxr-fsm",
  "mcp", "--db", str(tmp_db)]``).
* :func:`mcp.client.stdio.stdio_client` — async context manager
  yielding the ``(read, write)`` stream pair that
  :class:`ClientSession` is constructed against.

Tool results come back as :class:`mcp.types.CallToolResult` with
``structuredContent`` populated (FastMCP wraps the Pydantic return
value automatically). For tools whose return annotation is a
``Union[A, B]`` (as ``fsm.subscribe_events`` is —
``EventBatch | McpToolError``), FastMCP wraps the payload under a
``"result"`` key so the JSON-Schema output contract can describe both
arms. We branch on ``structuredContent["result"]`` rather than parsing
the text content blocks because the structured payload is the
documented machine contract.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ctxr.fsm.core.models import EventKind, FsmSpec, State, Transition
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Generous client-side timeouts. The MCP stdio handshake on a cold
# ``uv run`` typically completes within a couple of seconds on this
# project's CI image; the higher ceiling absorbs the slower local
# Rancher-on-macOS path without flake.
_INIT_TIMEOUT_SECONDS: float = 30.0
_CALL_TIMEOUT_SECONDS: float = 30.0


# Consumer durable identity used by both calls in the test. Re-using
# the same name is exactly how the cursor is resumed: the second call
# refreshes the consumer in place and pulls only the deliveries that
# became ``pending`` after the first call ack'd everything it had.
_CONSUMER_NAME: str = "t1"


# The subset of event kinds the subscriber filters on. Keeping it
# narrow makes the assertions tight: we will emit a mix of kinds and
# verify only ``state_entered`` / ``state_exited`` rows come back.
_KINDS_FILTER: list[str] = [
    EventKind.state_entered.value,
    EventKind.state_exited.value,
]


# A short timeout for ``fsm.subscribe_events`` itself so an empty
# batch returns quickly. The tool's contract allows up to 60s; 2s is
# more than enough for the test's "did we get the new events?" probe.
_SUBSCRIBE_TIMEOUT_SECONDS: float = 2.0


# ---------------------------------------------------------------------------
# Pre-population helpers (run inside the test, before the subprocess boots)
# ---------------------------------------------------------------------------


def _build_two_state_spec() -> FsmSpec:
    """Return a tiny two-state FSM spec used for pre-population.

    Linear ``state_a`` → ``state_b``. We do not actually *drive* the
    engine — we only need a registered spec + a run row so the events
    we emit by hand have a valid ``run_id`` foreign key.
    """
    return FsmSpec(
        id="subscribe_events_demo",
        version=1,
        entry="state_a",
        states=[
            State(
                id="state_a",
                purpose="entry state",
                transitions=[Transition(to="state_b", when="always")],
            ),
            State(
                id="state_b",
                purpose="terminal state",
                transitions=[],
            ),
        ],
    )


def _prepopulate_database(db_path: Path) -> str:
    """Migrate the DB, register a spec, start a run, register consumer ``t1``,
    and emit a mix of events.

    Returns the ``run_id`` so the test can correlate the delivered
    events back to the run it created.

    Why register the consumer *before* emitting events?
    --------------------------------------------------
    ``EventsRepo.emit`` performs the fan-out *at emit time* — it
    inserts one ``EventDeliveryTable`` row per matching consumer.
    Consumers that register *after* an event is emitted never see it
    because no delivery row exists for them. This is the bus's
    documented at-least-once contract.

    Pre-registering ``t1`` here means the subscribe-events tool's
    own idempotent re-registration on first call is a no-op on the
    delivery rows (it only updates the filter columns), so every
    event we emit below is already queued for ``t1`` when the
    subprocess wakes up.

    A mix of kinds is emitted so the ``kinds=[state_entered,
    state_exited]`` filter can prove it actually filters:

    * ``state_entered`` x 2 (state_a entry, state_b entry) — matched
    * ``state_exited``  x 1 (state_a exit)                — matched
    * ``transition_taken`` x 1                            — filtered out

    Plus the ``run_started`` event ``Project.start_run`` emits
    automatically — also filtered out because its kind is not in the
    list.
    """
    with Project.open(db_path, migrate=True) as project:
        registered = project.register_spec(_build_two_state_spec())
        spec_id = registered.spec.id

        run = project.start_run(spec_id, args={"prepopulated": True})
        run_id = run.id

        # The runtime producer is upserted by ``start_run``; fetch its
        # id so the hand-emitted events below share the same producer
        # identity as the automatic ``run_started`` event. This matches
        # what a real engine would do — every state-level emit is
        # attributed to the same runtime producer.
        with project.session_factory() as session, session.begin():
            producer = project.producers.upsert(
                session,
                kind="engine",
                name="fsm.runtime",
            )
            producer_id = producer.id

            # Register the consumer with the same filter the subscribe
            # call will use later. This is the critical step: the
            # delivery rows for the events emitted below are created
            # *at emit time* against this consumer, so the subprocess
            # only has to pull-and-ack them — not wait for new emits.
            project.consumers.register(
                session,
                kind="mcp_subscriber",
                name=_CONSUMER_NAME,
                filter_kind=_KINDS_FILTER,
                filter_run_id=run_id,
            )

            # Emit four hand-crafted events plus the automatic
            # ``run_started`` that already landed inside ``start_run``.
            # The mix exercises the kind filter: only state_entered /
            # state_exited should come back through subscribe_events.

            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.state_entered.value,
                payload={"run_id": run_id, "state": "state_a", "entry_seq": 1},
                run_id=run_id,
            )
            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.state_exited.value,
                payload={"run_id": run_id, "state": "state_a"},
                run_id=run_id,
            )
            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.transition_taken.value,
                payload={"run_id": run_id, "from": "state_a", "to": "state_b"},
                run_id=run_id,
            )
            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.state_entered.value,
                payload={"run_id": run_id, "state": "state_b", "entry_seq": 2},
                run_id=run_id,
            )

    return run_id


def _emit_post_subscribe_event(db_path: Path, run_id: str) -> None:
    """Emit one extra ``state_exited`` event for the second subscribe call.

    Re-opens the project (the previous handle was closed when its
    context manager exited inside :func:`_prepopulate_database`).
    The new event must match the consumer's filter so a delivery row
    is created against the already-registered ``t1`` consumer — which
    is precisely the row the second subscribe call should pull.
    """
    with (
        Project.open(db_path, migrate=False) as project,
        project.session_factory() as session,
        session.begin(),
    ):
        producer = project.producers.upsert(
            session,
            kind="engine",
            name="fsm.runtime",
        )
        project.events.emit(
            session,
            producer_id=producer.id,
            kind=EventKind.state_exited.value,
            payload={"run_id": run_id, "state": "state_b"},
            run_id=run_id,
        )


# ---------------------------------------------------------------------------
# MCP client helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _spawn_server(db_path: Path) -> AsyncIterator[ClientSession]:
    """Boot ``ctxr-fsm mcp`` as a subprocess and yield an initialized session.

    ``uv run`` is used as the command so the subprocess picks up the
    same project virtualenv that pytest itself is running under. The
    explicit ``--db`` flag bypasses the env-var / default-path
    precedence so the subprocess hits the same per-test temp file the
    in-process pre-population used.

    The ``stdio_client`` and ``ClientSession`` context managers are
    nested explicitly (rather than via a single ``async with``) so the
    teardown order is deterministic: the session is shut down first
    (sending the MCP shutdown frame), then the stdio pipes are torn
    down (closing the pipes signals the subprocess to exit). This
    matches the SDK's documented pattern.
    """
    params = StdioServerParameters(
        command="uv",
        args=["run", "ctxr-fsm", "mcp", "--db", str(db_path)],
    )
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        # The initialize handshake is what proves the server is
        # actually up — every subsequent call_tool depends on it.
        # We wrap in ``wait_for`` to fail loudly with a useful
        # timeout error if the subprocess never speaks the protocol
        # (e.g. an import error printed to stderr would leave us
        # blocking on stdin forever otherwise).
        await asyncio.wait_for(
            session.initialize(),
            timeout=_INIT_TIMEOUT_SECONDS,
        )
        yield session


async def _call_subscribe(
    session: ClientSession,
    *,
    consumer_name: str = _CONSUMER_NAME,
    kinds: list[str] | None = None,
    timeout_seconds: float = _SUBSCRIBE_TIMEOUT_SECONDS,
    max_events: int = 50,
) -> dict[str, Any]:
    """Call ``fsm.subscribe_events`` and return its structured content.

    Returns the parsed Pydantic-projected dict (FastMCP's
    ``structuredContent`` for a Pydantic return value). We assert
    ``isError is False`` here so any negative-path failure surfaces
    with a clear pytest stack rather than as a downstream KeyError.

    ``kinds`` is passed through verbatim; ``None`` means "every
    kind" (the tool default).
    """
    arguments: dict[str, Any] = {
        "consumer_name": consumer_name,
        "timeout_seconds": timeout_seconds,
        "max_events": max_events,
    }
    if kinds is not None:
        arguments["kinds"] = kinds

    result = await asyncio.wait_for(
        session.call_tool("fsm.subscribe_events", arguments=arguments),
        timeout=_CALL_TIMEOUT_SECONDS,
    )

    assert result.isError is False, (
        f"fsm.subscribe_events returned an error: "
        f"content={result.content!r} structured={result.structuredContent!r}"
    )
    assert result.structuredContent is not None, (
        "fsm.subscribe_events returned no structuredContent — FastMCP "
        "should always wrap a Pydantic return value into structuredContent"
    )
    # FastMCP wraps a ``Union[A, B]`` return annotation under the
    # ``"result"`` key so the output JSON Schema can describe both
    # arms. Unwrap it here so callers get the EventBatch dict
    # directly. If the underlying tool returned an ``McpToolError``
    # the dict will have an ``"error"`` key instead — callers branch
    # on the field shape rather than on Python type info.
    payload = result.structuredContent.get("result")
    assert isinstance(payload, dict), (
        f"expected structuredContent['result'] to be a dict, got "
        f"{type(payload).__name__}: {result.structuredContent!r}"
    )
    assert "error" not in payload, (
        f"fsm.subscribe_events returned an error envelope: {payload!r}"
    )
    return payload


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_events_returns_filtered_events_then_cursor_advances() -> None:
    """Single end-to-end test that covers both contractual properties.

    The two assertions are deliberately bundled into one test because
    they share an expensive subprocess fixture (boot + initialize +
    teardown ~5-10s) and a shared pre-populated database. Splitting
    them would double the wall-clock cost for no extra coverage —
    the second assertion *cannot* run in isolation because it
    structurally depends on the first call having advanced the cursor.

    Coverage:

    1. First call with ``kinds=[state_entered, state_exited]`` returns
       exactly the three matching events from the pre-populated set
       (two state_entered, one state_exited), in emit order, with
       payloads intact. The ``run_started`` and ``transition_taken``
       events are filtered out.
    2. A *second* call with the same ``consumer_name`` after one new
       matching event has been emitted returns only that single new
       event — the cursor advanced because the first call ack'd
       everything it pulled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"

        # Pre-populate synchronously, in-process, before the subprocess
        # boots. The subprocess will see the registered consumer and
        # all four hand-emitted events the moment it opens the DB.
        run_id = _prepopulate_database(db_path)

        async with _spawn_server(db_path) as session:
            # ── First call ───────────────────────────────────────────
            first_batch = await _call_subscribe(
                session,
                consumer_name=_CONSUMER_NAME,
                kinds=_KINDS_FILTER,
            )

            assert first_batch.get("next_cursor") is None, (
                "next_cursor is reserved and should always be None today"
            )
            first_events = first_batch.get("events")
            assert isinstance(first_events, list), (
                f"events must be a list, got {type(first_events).__name__}"
            )

            # Three matched events: two state_entered + one state_exited.
            # transition_taken and run_started are filtered out by the
            # kinds list above.
            assert len(first_events) == 3, (
                f"expected 3 filtered events, got {len(first_events)}: "
                f"{[(e.get('kind'), e.get('payload')) for e in first_events]}"
            )

            # Every returned event must have a kind in the filter set —
            # this is the property the kinds filter guarantees.
            for event in first_events:
                assert event["kind"] in _KINDS_FILTER, (
                    f"event with disallowed kind leaked through filter: {event!r}"
                )
                # Every event was emitted with a run_id; the bus
                # preserves that scoping in the delivered payload.
                assert event["run_id"] == run_id, (
                    f"event run_id {event['run_id']!r} != expected {run_id!r}"
                )

            # Emit-order: the first state_entered (state_a) precedes
            # the state_exited (state_a), which precedes the second
            # state_entered (state_b). Per-run ``seq`` is the wire-
            # level guarantee of that ordering.
            seqs = [event["seq"] for event in first_events]
            assert seqs == sorted(seqs), (
                f"events out of monotonic seq order: {seqs}"
            )

            kinds_in_order = [event["kind"] for event in first_events]
            assert kinds_in_order == [
                EventKind.state_entered.value,
                EventKind.state_exited.value,
                EventKind.state_entered.value,
            ], (
                f"unexpected delivery order of kinds: {kinds_in_order}"
            )

            # ── Emit one more matching event between the calls ───────
            # Do this *outside* the subprocess (the subprocess holds
            # its own SQLite handle but SQLite's default journal mode
            # is WAL via Alembic's migration, so a second writer can
            # land a row that the subprocess sees on its next poll).
            _emit_post_subscribe_event(db_path, run_id)

            # ── Second call — same consumer_name ─────────────────────
            second_batch = await _call_subscribe(
                session,
                consumer_name=_CONSUMER_NAME,
                kinds=_KINDS_FILTER,
            )
            second_events = second_batch.get("events")
            assert isinstance(second_events, list), (
                f"events must be a list, got {type(second_events).__name__}"
            )

            # The cursor advanced — only the single newly-emitted event
            # should come back. The three events from the first call
            # were ack'd inside the tool body and are no longer
            # ``pending`` for ``t1``.
            assert len(second_events) == 1, (
                f"expected 1 new event after cursor advance, got "
                f"{len(second_events)}: "
                f"{[(e.get('kind'), e.get('payload')) for e in second_events]}"
            )

            new_event = second_events[0]
            assert new_event["kind"] == EventKind.state_exited.value, (
                f"new event kind mismatch: {new_event['kind']!r}"
            )
            assert new_event["run_id"] == run_id, (
                f"new event run_id {new_event['run_id']!r} != expected {run_id!r}"
            )
            # The new event's seq must be strictly greater than every
            # seq returned in the first batch — that is the bus's per-
            # run monotonicity guarantee.
            assert new_event["seq"] > max(seqs), (
                f"new event seq {new_event['seq']} did not exceed "
                f"first-batch max {max(seqs)}"
            )
