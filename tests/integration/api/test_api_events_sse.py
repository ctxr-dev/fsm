"""Integration test: ``GET /api/v1/events/stream`` over a real uvicorn socket.

This module covers the W5 SSE surface in the most production-equivalent
way the test harness can reach for: a programmatic
:class:`uvicorn.Server` is booted in a background thread against the
FastAPI ``app`` from :mod:`ctxr.fsm.api`, an :class:`httpx.AsyncClient`
opens a streaming GET against ``/api/v1/events/stream``, and a separate
in-process :class:`Project` handle emits an event that the stream is
expected to surface within one second.

Why not :class:`fastapi.testclient.TestClient`?
----------------------------------------------

``TestClient`` is the right tool for the synchronous routes — it runs
the ASGI app in-process, handles the lifespan hooks, honours
``Depends`` wiring, and avoids the cost of spinning uvicorn — but it
does not consume ``text/event-stream`` responses the way a real client
does. The body comes back as one buffered chunk on connection close,
which defeats the entire point of an SSE test: the goal is to prove
that events emitted *after* the client connected are pushed down the
wire *while it stays connected*, with reasonable latency. Driving
uvicorn for real is the only way to assert that contract end to end.

Why a single :class:`Project` handle shared by the API and the test?
------------------------------------------------------------------

The SSE generator inside :mod:`ctxr.fsm.api.routes_events` calls
``project.session_factory()`` against the handle that
:func:`ctxr.fsm.api._state.set_project` bound at boot. The test emits
events through the *same* handle, so the consumer fan-out inside
``EventsRepo.emit`` lands rows in the same database file the stream is
polling — the only safe way to get cross-thread coordination through
SQLite's WAL is to share the engine / connection pool, which sharing
the handle gives us for free.

Cleanup discipline
------------------

* The uvicorn ``Server`` is asked to exit via ``server.should_exit =
  True`` and the worker thread is joined with a generous timeout. We do
  *not* call ``server.shutdown()`` — it is an async coroutine that must
  be awaited from inside the server's own event loop, which we don't
  have a hook into from the test thread.
* The :class:`Project` is closed in a ``finally`` block, then the
  module-level state binding is reset so the next test starts from a
  blank slate (the binding is process-wide, not test-scoped).
* The :class:`tempfile.TemporaryDirectory` context manager cleans up
  the on-disk SQLite file last; ``Project.close`` releases the file
  lock before the directory is deleted, avoiding the macOS "directory
  not empty" race that bites tests that try to delete in the wrong
  order.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

from ctxr.fsm.api import _state, app
from ctxr.fsm.core.models import EventKind, FsmSpec, State, Transition
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


# Loopback bind. The test never reaches the network — uvicorn binds
# 127.0.0.1 on a kernel-picked ephemeral port so concurrent test
# workers cannot collide.
_HOST: str = "127.0.0.1"


# How long we are willing to wait for the uvicorn worker thread to
# come online (i.e. ``Server.started`` flips to True). 5 s absorbs slow
# CI containers and the cold-start cost of the FastAPI lifespan hook.
_SERVER_BOOT_TIMEOUT_SECONDS: float = 5.0


# The end-to-end SLO the brief calls for: an event emitted on the
# server side must surface on the SSE client within one second. The
# SSE generator polls every 250 ms (see ``_SSE_POLL_INTERVAL_SECONDS``
# in :mod:`ctxr.fsm.api.routes_events`); 1 s is four poll cycles —
# comfortably above the worst-case scheduling jitter while still
# tight enough that a regression would not slip past the assertion.
_EVENT_DELIVERY_TIMEOUT_SECONDS: float = 1.0


# Generous overall timeout on the streaming HTTP request itself. We
# never want this to fire under normal operation; it only exists to
# stop a buggy server from holding the test runner forever. The
# read-timeout shape (``connect=1, read=None``) leaves the stream open
# indefinitely while still failing fast if the listener never comes up.
_HTTP_CONNECT_TIMEOUT_SECONDS: float = 5.0


# Durable consumer name for the SSE subscription. Mirrors the
# convention from ``tests/integration/mcp/test_mcp_subscribe_events.py``
# — reconnecting with the same name resumes the cursor because every
# delivery is acked inline by the SSE generator.
_CONSUMER_NAME: str = "sse_integration_test_consumer"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Bind a transient socket to ``(127.0.0.1, 0)`` and return the assigned port.

    Mirrors :func:`ctxr.fsm.api.server.pick_free_port` but kept local
    so the test does not depend on the server module's helper layout.
    The kernel guarantees no other process holds this port for the
    brief window between close and uvicorn re-binding; in practice the
    only collision risk is another test in the same process, and the
    OS will not hand the same ephemeral port to two concurrent callers.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((_HOST, 0))
        return int(sock.getsockname()[1])


def _build_one_state_spec() -> FsmSpec:
    """Return the smallest valid spec that lets ``Project.start_run`` succeed.

    The test does not drive the engine — we only need a registered
    spec + a run row so the events we emit by hand have a valid
    ``run_id`` foreign key. A single-state spec with no transitions is
    the cheapest legal shape.
    """
    return FsmSpec(
        id="sse_integration_demo",
        version=1,
        entry="only",
        states=[
            State(
                id="only",
                purpose="single state used solely to satisfy spec validation",
                transitions=[Transition(to="only", when="never")],
            ),
        ],
    )


@contextmanager
def _running_uvicorn(project: Project, port: int) -> Iterator[uvicorn.Server]:
    """Boot ``ctxr.fsm.api.app`` against ``project`` on ``port`` in a thread.

    Binding the project *before* uvicorn starts is the documented
    fast-path in :func:`ctxr.fsm.api.lifespan_handler`: the lifespan
    hook sees the binding is already in place and skips its own open /
    close, leaving the test in control of the project's lifecycle. The
    server thread is asked to shut down via ``should_exit = True`` on
    exit; we never call the async ``shutdown`` coroutine because that
    would need to be awaited from inside the server's own loop.

    Yields the :class:`uvicorn.Server` instance so a test can inspect
    ``server.started`` while waiting for boot.
    """
    _state.set_project(project)

    config = uvicorn.Config(
        app,
        host=_HOST,
        port=port,
        log_level="warning",
        # ``lifespan="on"`` is the default but stated explicitly here
        # so a reader knows we *are* relying on the lifespan hook
        # firing (it's how the project handle stays bound when uvicorn
        # is driven programmatically instead of via ``uvicorn.run``).
        lifespan="on",
    )
    server = uvicorn.Server(config)

    # ``Server.run`` builds its own asyncio event loop, runs ``serve``
    # to completion, then tears the loop down. Running it in a daemon
    # thread keeps pytest's main thread free for the HTTP client.
    thread = threading.Thread(target=server.run, name="uvicorn-sse-test", daemon=True)
    thread.start()

    # Poll ``server.started`` until uvicorn finishes its boot dance.
    # The flag flips after the lifespan startup hook completes and the
    # listening socket is accepting connections.
    deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(timeout=2.0)
            raise RuntimeError(
                "uvicorn did not report 'started' within "
                f"{_SERVER_BOOT_TIMEOUT_SECONDS} s"
            )
        time.sleep(0.02)

    try:
        yield server
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        # The lifespan teardown does NOT close the project because we
        # bound it ourselves (see the lifespan branch in
        # :mod:`ctxr.fsm.api`). Reset the binding here so subsequent
        # tests start from a clean slate; the caller owns
        # ``project.close()`` so the SQLite file lock is released
        # before the temp directory disappears.
        _state.reset_project()


async def _sse_event_frames(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
) -> AsyncIterator[tuple[str, str]]:
    """Yield ``(event_name, data_string)`` tuples from an SSE response.

    A line-based parser is the simplest correct shape: SSE frames are
    blocks of ``key: value`` lines terminated by a blank line. We
    accumulate the in-progress frame, emit it when the blank line
    arrives, and reset for the next one. Heartbeat ``ping`` frames are
    yielded too so the caller can distinguish them from real data
    frames if it cares (this test ignores them by filtering on
    ``event == "event"``).
    """
    async with client.stream("GET", url, params=params) as response:
        # Surface non-200 responses immediately rather than letting the
        # iterator return zero frames — a 401 / 404 here would otherwise
        # masquerade as "the event never arrived" downstream.
        response.raise_for_status()

        current_event: str | None = None
        current_data_lines: list[str] = []

        async for raw_line in response.aiter_lines():
            # ``aiter_lines`` strips the trailing newline; an empty
            # string marks the end of an SSE frame.
            if raw_line == "":
                if current_event is not None or current_data_lines:
                    yield (
                        current_event or "message",
                        "\n".join(current_data_lines),
                    )
                current_event = None
                current_data_lines = []
                continue

            # SSE allows ``:`` comment lines (used by some servers as
            # a keep-alive). Skip them so they don't pollute the frame
            # being assembled. ``sse-starlette`` does not currently emit
            # them but a future version might.
            if raw_line.startswith(":"):
                continue

            field, _, value = raw_line.partition(":")
            # Per the SSE spec the value may have a leading space that
            # the producer included for readability; trim it but only
            # once so genuine leading whitespace inside ``data`` is
            # preserved.
            if value.startswith(" "):
                value = value[1:]

            if field == "event":
                current_event = value
            elif field == "data":
                current_data_lines.append(value)
            # Other fields (``id``, ``retry``) are accepted but
            # ignored — the SSE generator under test does not emit them.


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_emitted_arrives_on_sse_stream_within_one_second() -> None:
    """Emit an event on the server side; assert it surfaces on the SSE stream.

    The flow:

    1. Open a project on a temp SQLite file; register a spec; start a
       run so we have a valid ``run_id`` to attach events to.
    2. Boot uvicorn against ``ctxr.fsm.api.app`` in a background
       thread, with the project bound process-wide via
       :func:`ctxr.fsm.api._state.set_project`.
    3. Open a streaming ``GET /api/v1/events/stream`` from
       :class:`httpx.AsyncClient`, with a ``consumer_name`` query
       parameter so the server registers the SSE consumer and starts
       fanning future emits to it.
    4. Wait briefly for the consumer registration round-trip to land
       inside the database (the SSE generator does it inside the
       handler, before the first poll), then emit a fresh event via
       the shared project.
    5. Consume frames from the stream until either a real ``event``
       frame appears or :data:`_EVENT_DELIVERY_TIMEOUT_SECONDS` elapses.
       Assert the payload matches what was emitted.

    The whole test runs in roughly one to two seconds on a warm cache.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        project = Project.open(db_path, migrate=True)
        try:
            # Pre-create the spec + run so the emit below has a valid
            # ``run_id`` foreign key. The ``start_run`` call also emits
            # a ``run_started`` event, but the SSE consumer is
            # registered *after* that emit, so the run-started event
            # is never delivered to this consumer — the consumer fan-out
            # only sees emits that happen after registration.
            registered = project.register_spec(_build_one_state_spec())
            run = project.start_run(registered.spec.id)

            port = _pick_free_port()
            base_url = f"http://{_HOST}:{port}"

            with _running_uvicorn(project, port):
                async with httpx.AsyncClient(
                    base_url=base_url,
                    timeout=httpx.Timeout(
                        connect=_HTTP_CONNECT_TIMEOUT_SECONDS,
                        read=None,
                        write=None,
                        pool=None,
                    ),
                ) as client:
                    # Buffer for collected frames so the assertion at
                    # the bottom can show context if the test fails.
                    seen_events: list[tuple[str, str]] = []

                    async def consume_until_event() -> tuple[str, str] | None:
                        """Pull frames until the first non-heartbeat arrives."""
                        async for name, data in _sse_event_frames(
                            client,
                            "/api/v1/events/stream",
                            params={"consumer_name": _CONSUMER_NAME},
                        ):
                            seen_events.append((name, data))
                            if name == "event":
                                return (name, data)
                        return None

                    consumer_task = asyncio.create_task(consume_until_event())

                    # Give the SSE handler a tick to register the
                    # consumer in the DB before we emit. Without this
                    # tiny delay there is a race where the emit's
                    # fan-out runs before the consumer row exists, and
                    # no delivery row is created — the bus is
                    # at-least-once only for emits that happen *after*
                    # registration, by design.
                    await asyncio.sleep(0.2)

                    with (
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
                            kind=EventKind.state_entered.value,
                            payload={
                                "run_id": run.id,
                                "state": "only",
                                "entry_seq": 1,
                            },
                            run_id=run.id,
                        )

                    try:
                        frame = await asyncio.wait_for(
                            consumer_task,
                            timeout=_EVENT_DELIVERY_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        consumer_task.cancel()
                        # Await the cancellation so the streaming
                        # response is torn down before the
                        # ``async with`` block exits.
                        try:
                            await consumer_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        pytest.fail(
                            "no SSE 'event' frame arrived within "
                            f"{_EVENT_DELIVERY_TIMEOUT_SECONDS} s; "
                            f"frames seen so far: {seen_events!r}"
                        )

                    assert frame is not None, (
                        "SSE iterator exited before producing an event "
                        f"frame; frames seen so far: {seen_events!r}"
                    )
                    name, data = frame
                    assert name == "event", (
                        f"expected first non-heartbeat frame to be 'event'; "
                        f"got {name!r} with payload {data!r}"
                    )

                    parsed = json.loads(data)
                    # The Event Pydantic model serialises with these
                    # field names; a regression that re-shaped the
                    # payload would surface here loudly.
                    assert (
                        parsed["kind"] == EventKind.state_entered.value
                    ), f"unexpected kind in delivered event: {parsed!r}"
                    assert parsed["run_id"] == run.id, (
                        "delivered event was for a different run: "
                        f"{parsed!r} (expected run_id={run.id!r})"
                    )
                    assert parsed["payload"]["state"] == "only", (
                        "delivered event payload did not echo the "
                        f"emitted state: {parsed!r}"
                    )

                    # Tear down the streaming generator cleanly. Cancelling
                    # the task (which has already completed) is a no-op; if
                    # the assertion path above raised we already handled it
                    # in the ``except TimeoutError`` branch.
                    if not consumer_task.done():
                        consumer_task.cancel()
                        try:
                            await consumer_task
                        except (asyncio.CancelledError, Exception):
                            pass
        finally:
            project.close()
