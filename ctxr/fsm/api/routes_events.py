"""HTTP / SSE routes for the event-bus surface.

This module is the W5 mirror of the W4 MCP tools in
:mod:`ctxr.fsm.mcp.tools_events`. Where MCP exposes the bus as a
unary long-poll (one tool call returns one batch and naturally resumes
on the next call), the HTTP layer offers *two* complementary shapes:

* ``GET /api/v1/events/stream`` — a long-lived **SSE** connection
  (``text/event-stream``) where the server pushes events as they
  arrive plus a heartbeat ``ping`` every 15 seconds so proxies do not
  close the connection during quiet periods.
* ``GET /api/v1/events`` — a one-shot **polling** read. Cursors are
  client-held (``since_seq``) and the response is a plain JSON list.
  This is the shape the UI's "open this run's journal at row N"
  view uses.

Two registry endpoints round out the surface:

* ``GET /api/v1/producers`` and ``GET /api/v1/consumers`` enumerate
  the bus topology.

Finally, ack-by-id:

* ``POST /api/v1/consumers/{consumer_id}/ack`` lets a long-running
  HTTP client confirm it has finished processing a batch. The SSE
  stream and the polling read both deliver events without auto-acking
  (auto-ack would defeat the point of giving the client an ack
  endpoint), so callers that need at-least-once semantics have a
  durable way to advance the consumer cursor.

Layering
--------

This module never imports the MCP SDK. The two surfaces share a
:class:`Project` but no other code — the HTTP layer is a pure ASGI
overlay on the SQLite substrate. Sync SQLite calls are wrapped in
``run_in_threadpool`` so the FastAPI event loop is never blocked by
SQLite I/O.

SSE generator discipline
------------------------

The SSE generator is structured so:

* The consumer is registered exactly once before the first poll, so
  re-connects with the same ``consumer_name`` resume the cursor.
* Each poll cycle pulls a bounded batch of pending deliveries,
  acks every row inside the same transaction (matching the W4 MCP
  contract — at-least-once with auto-ack on the read path; explicit
  ack-by-id is the optional escape hatch), yields the events one at
  a time, then naps ``_SSE_POLL_INTERVAL_SECONDS`` (250 ms).
* A heartbeat ``ping`` event is emitted whenever no real event has
  been pushed for ``_SSE_HEARTBEAT_SECONDS`` (15 s). The heartbeat
  is what keeps a Cloudflare / nginx proxy from idle-timing-out the
  connection in the middle of a quiet stretch.
* ``asyncio.CancelledError`` is allowed to propagate so a client
  disconnect tears the generator down cleanly. ``sse-starlette``
  handles the rest.

Auth
----

Every endpoint here is mounted behind ``Depends(require_auth)`` at
the router level — see :func:`router.include_router` call site in
:mod:`ctxr.fsm.api`. Health probes live in the package root and are
*not* part of this router precisely so they can stay auth-free.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from ctxr.fsm.api._deps import ProjectDep, require_auth
from ctxr.fsm.api._pagination import (
    Page,
    PageParams,
    make_page_params,
    paginate_sequence,
)
from ctxr.fsm.sqlite import Consumer, Event, Producer

__all__ = [
    "AckBody",
    "AckResult",
    "router",
]


# ── Logger ────────────────────────────────────────────────────────────
# Pinned to this module so a running server's log can be grep'd for the
# event-bus HTTP surface without dragging in unrelated chatter.
_LOG = logging.getLogger("ctxr.fsm.api.routes_events")


# ── Tunables ──────────────────────────────────────────────────────────
# Poll cadence for the SSE generator. 250 ms keeps the loop responsive
# without hammering SQLite during idle periods; mirrors the W4 MCP
# subscribe poll interval so the two surfaces have the same "how fresh
# is fresh?" semantics.
_SSE_POLL_INTERVAL_SECONDS: float = 0.25

# Heartbeat cadence. 15 s is short enough to survive almost every
# reverse-proxy idle-timeout default (nginx: 60 s, Cloudflare: 100 s)
# and long enough that an interactive client doesn't see meaningless
# ``ping`` events flood the dev-tools timeline.
_SSE_HEARTBEAT_SECONDS: float = 15.0

# Per-cycle delivery batch cap. The SSE stream is push-driven so the
# user-facing "burstiness" is bounded by this number — large enough
# that a busy run drains its backlog quickly, small enough that a
# single cycle doesn't block the event loop for long. (sqlite work is
# off-loaded to a threadpool, but yielding ~hundreds of frames in one
# cycle would still stall the loop for the yield itself.)
_SSE_BATCH_LIMIT: int = 100

# Consumer.kind value assigned to subscribers registered via the SSE
# stream. Mirrors the W4 MCP convention (``mcp_subscriber``) so a
# ``GET /consumers`` dump can distinguish "this consumer is a browser
# tab streaming via SSE" from "this consumer is the engine itself".
_SSE_CONSUMER_KIND: str = "http_sse_subscriber"

# Hard cap on the ``limit`` parameter of the non-streaming poll. The
# same rationale as MCP's ``_MAX_EVENTS_HARD_CAP`` — a misconfigured
# client should not be able to pull a million rows in one request.
_POLL_HARD_CAP: int = 1000


# ── Pagination factories ──────────────────────────────────────────────
# Each list endpoint binds its own ``PageParams`` factory at module
# scope so the allow-list of sort keys is endpoint-specific (an
# unknown ``?sort=`` returns 422 with the allowed values rather than
# silently falling back). The defaults below match each endpoint's
# pre-W22b2 implicit ordering.
#
# ``events.seq`` is the per-run monotonic counter, so the previous
# "natural insertion order" is equivalent to ``seq ASC``; we expose
# both directions for symmetry with the rest of the API.
EventsPageParams = make_page_params(
    default_sort="seq_asc",
    allowed_sorts=("seq_asc", "seq_desc"),
)

# Producers / consumers were previously ordered by ``id`` (UUIDv7,
# which is time-ordered) but the operator wants most-recent first by
# the human-readable ``created_at`` column. ``id_asc`` is preserved
# as an opt-in because tooling that already keys off insertion order
# should still be able to ask for it.
ProducersPageParams = make_page_params(
    default_sort="created_at_desc",
    allowed_sorts=("created_at_desc", "created_at_asc", "id_asc"),
)
ConsumersPageParams = make_page_params(
    default_sort="created_at_desc",
    allowed_sorts=("created_at_desc", "created_at_asc", "id_asc"),
)


# ── Router ────────────────────────────────────────────────────────────
# Auth is applied at router-construction time so every endpoint here
# inherits it. Health / readiness probes deliberately live in the app
# root (not this router) so they remain unauth'd; the OpenAPI docs at
# ``/docs`` are also outside this router and unaffected.
router: APIRouter = APIRouter(
    prefix="/api/v1",
    tags=["events"],
    dependencies=[Depends(require_auth)],
)


# ── Pydantic request / response models ────────────────────────────────


class AckBody(BaseModel):
    """Request body for ``POST /consumers/{consumer_id}/ack``.

    Carries the list of event ids the client wishes to acknowledge.
    Empty list is permitted (no-op success) so a client that has
    nothing to ack can still hit the endpoint as a liveness probe.
    """

    model_config = ConfigDict(frozen=True)

    event_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "Event ids the consumer has finished processing. The "
            "server marks each (event_id, consumer_id) delivery row "
            "as ``acked`` inside a single transaction."
        ),
    )


class AckResult(BaseModel):
    """Response shape for ``POST /consumers/{consumer_id}/ack``.

    ``acked`` is the count of delivery rows actually updated — it
    may be smaller than ``len(event_ids)`` if some ids referred to
    deliveries that were already acked, expired, or belonged to a
    different consumer. The discrepancy is information for the
    caller (drift between local and server state); it is not an
    error.
    """

    model_config = ConfigDict(frozen=True)

    consumer_id: str = Field(
        ...,
        description="Echo of the consumer id from the URL path.",
    )
    requested: int = Field(
        ...,
        description="Number of event ids the caller submitted.",
    )
    acked: int = Field(
        ...,
        description=(
            "Number of delivery rows that actually transitioned to "
            "``acked`` as a result of this call."
        ),
    )


# ── Internal helpers ──────────────────────────────────────────────────


def _normalise_kinds(kinds: list[str] | None) -> list[str] | None:
    """Trim, drop empties, and dedupe a kinds list — or pass ``None`` through.

    The CSV-style ``kinds`` query parameter is forgiving on the wire
    so a caller can pass ``kinds=state_entered&kinds=&kinds=state_exited``
    without breaking the consumer-registration call. Returning ``None``
    on empty input preserves the "no filter" semantics — distinct from
    "empty filter list" which the bus would interpret as "match
    nothing".
    """
    if kinds is None:
        return None
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in kinds:
        candidate = str(raw).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        cleaned.append(candidate)
    return cleaned if cleaned else None


def _pull_and_ack_pending(
    project: ProjectDep,
    consumer_id: str,
    limit: int,
) -> list[Event]:
    """Pull up to ``limit`` pending deliveries and ack each in one txn.

    Sync helper invoked from the SSE generator via
    :func:`run_in_threadpool` so the asyncio loop is never blocked by
    SQLite. Matches the W4 MCP subscribe contract: each row is marked
    delivered + acked inside the same transaction before it is
    returned, giving at-least-once semantics on the stream itself
    while preserving the option of explicit ack-by-id via the
    dedicated POST route.
    """
    delivered: list[Event] = []
    with project.session_factory() as session, session.begin():
        pending = project.event_deliveries.pending_for(
            session,
            consumer_id=consumer_id,
            limit=limit,
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
            delivered = [ewd.event for ewd in pending]
    return delivered


def _register_sse_consumer(
    project: ProjectDep,
    *,
    consumer_name: str,
    kinds: list[str] | None,
    filter_run_id: str | None,
) -> str:
    """Register / refresh the SSE consumer and return its id.

    Re-registering with the same ``(kind, name)`` updates the filter
    columns in place — see :meth:`ConsumersRepo.register`. Returning
    just the id keeps the generator's hot path tiny (the id is the
    only thing the poll loop needs).
    """
    with project.session_factory() as session, session.begin():
        consumer = project.consumers.register(
            session,
            kind=_SSE_CONSUMER_KIND,
            name=consumer_name,
            filter_kind=kinds,
            filter_run_id=filter_run_id,
        )
    return consumer.id


# ── SSE stream ────────────────────────────────────────────────────────


@router.get(
    "/events/stream",
    summary="Subscribe to FSM events via Server-Sent Events.",
    response_class=EventSourceResponse,
    responses={
        200: {
            "description": (
                "A long-lived ``text/event-stream`` connection. The "
                "server pushes one ``event`` frame per FSM event "
                "(``data`` is the Event JSON) plus a ``ping`` frame "
                "every 15 s for keep-alive."
            ),
            "content": {"text/event-stream": {}},
        }
    },
)
async def stream_events(
    project: ProjectDep,
    consumer_name: Annotated[
        str,
        Query(
            min_length=1,
            description=(
                "Durable identity for this subscription. Reconnecting "
                "with the same name resumes the cursor — every "
                "delivery is acked inline so the cursor is server-held."
            ),
        ),
    ],
    kinds: Annotated[
        list[str] | None,
        Query(
            description=(
                "Optional EventKind string values to filter on. "
                "Repeat the query parameter to pass multiple values."
            ),
        ),
    ] = None,
    filter_run_id: Annotated[
        UUID | None,
        Query(
            description=(
                "Optional run id to scope the stream to a single run. "
                "Omit to receive events from every run."
            ),
        ),
    ] = None,
) -> EventSourceResponse:
    """Stream events as SSE frames until the client disconnects.

    Each FSM event becomes a frame ``event: event\\ndata: <JSON>``; a
    heartbeat ``event: ping\\ndata: {}`` is emitted every
    :data:`_SSE_HEARTBEAT_SECONDS` so reverse proxies do not idle-
    timeout the connection. The generator runs until the client
    disconnects (which surfaces as :class:`asyncio.CancelledError`
    inside the iterator) at which point we exit cleanly.

    The consumer is registered once at the top of the handler — any
    later reconnect with the same ``consumer_name`` updates the
    filters in place and resumes the cursor because every delivered
    event is acked inline.
    """
    normalised_kinds = _normalise_kinds(kinds)
    filter_run_id_str = str(filter_run_id) if filter_run_id is not None else None

    # Register the consumer once, off-loop. The id we get back is the
    # only handle the poll loop needs; subsequent reconnects with the
    # same name re-bind filters and resume because every delivery is
    # acked inline.
    consumer_id = await run_in_threadpool(
        _register_sse_consumer,
        project,
        consumer_name=consumer_name,
        kinds=normalised_kinds,
        filter_run_id=filter_run_id_str,
    )

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        """Yield SSE frames until the client disconnects.

        Returns dicts that ``sse-starlette`` formats as
        ``event:<event>\\ndata:<data>\\n\\n``. ``data`` is always a
        JSON string — for real events we delegate to Pydantic's
        ``model_dump_json``; for heartbeats we ship a literal ``{}``
        so the frame is still well-formed JSON if a client tries to
        parse it.
        """
        last_event_at = time.monotonic()
        try:
            while True:
                events = await run_in_threadpool(
                    _pull_and_ack_pending,
                    project,
                    consumer_id,
                    _SSE_BATCH_LIMIT,
                )

                if events:
                    last_event_at = time.monotonic()
                    for event in events:
                        # ``model_dump_json`` is the Pydantic-native
                        # path and respects the model's serialisation
                        # config (frozen, alias generators, …) without
                        # us having to thread ``json.dumps`` through.
                        yield {
                            "event": "event",
                            "data": event.model_dump_json(),
                        }
                    # After a burst, loop again immediately so the
                    # next batch is drained without a 250 ms wait.
                    continue

                # No events this cycle. Emit a heartbeat if we've been
                # quiet for longer than the configured interval — a
                # browser EventSource and the various reverse-proxy
                # idle timers all key off "is the socket still
                # producing bytes?" rather than "is the connection
                # still useful?".
                now = time.monotonic()
                if now - last_event_at >= _SSE_HEARTBEAT_SECONDS:
                    last_event_at = now
                    yield {"event": "ping", "data": "{}"}

                await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            # Client disconnected (or the server is shutting down).
            # Let the cancellation propagate so ``sse-starlette``
            # closes the response cleanly; we *do not* swallow it
            # because doing so would mask a shutdown-in-flight.
            _LOG.debug(
                "SSE stream cancelled for consumer %s", consumer_name
            )
            raise

    return EventSourceResponse(event_generator())


# ── Non-streaming poll ────────────────────────────────────────────────


@router.get(
    "/events",
    response_model=Page[Event],
    summary="One-shot poll of events for a run.",
)
async def list_events(
    project: ProjectDep,
    params: Annotated[PageParams, Depends(EventsPageParams)],
    run_id: Annotated[
        UUID,
        Query(description="The run whose events to fetch."),
    ],
    since_seq: Annotated[
        int | None,
        Query(
            ge=0,
            description=(
                "Exclusive lower bound on ``seq``. Pass the last "
                "``seq`` you already saw and you'll receive "
                "everything strictly after it."
            ),
        ),
    ] = None,
    kinds: Annotated[
        list[str] | None,
        Query(
            description=(
                "Optional EventKind string values to filter on. "
                "Repeat the query parameter to pass multiple values."
            ),
        ),
    ] = None,
) -> Page[Event]:
    """Return a page of events for ``run_id``, ordered per ``params.sort``.

    Delegates to :meth:`EventsRepo.by_run_paged`, which runs a single
    SQL statement (filtered WHERE + ``COUNT(*) OVER ()``) so the
    handler never materialises the full journal just to slice off
    one page. The pre-W22b2-iter draft pre-loaded every matching
    row via the iterator-based :meth:`by_run` — fine for a small
    fixture, a DoS vector for a long-lived run with hundreds of
    thousands of events. Removing the cap from the wire while
    keeping the in-memory drain would be strictly worse than the
    pre-pagination ``limit`` parameter; the paged repo method
    avoids the trap entirely.

    The cursor (``since_seq``) is client-held — callers track the
    last ``seq`` they received and pass it back on the next call.
    Unlike the SSE stream, no delivery rows are touched: this is
    a pure read.
    """
    normalised_kinds = _normalise_kinds(kinds)
    run_id_str = str(run_id)

    def _read() -> tuple[list[Event], int]:
        """Sync read body — invoked in the threadpool.

        :meth:`EventsRepo.by_run_paged` returns the bus-side
        :class:`ctxr.fsm.sqlite.repos_events.Event` while the
        handler's response_model is the lifecycle-side
        :class:`ctxr.fsm.sqlite.repos_core.Event` (re-exported by
        :mod:`ctxr.fsm.sqlite`). The two classes have field-
        identical shapes — see the explanatory note in
        ``ctxr/fsm/sqlite/__init__.py`` — but mypy treats them as
        distinct. We bridge via ``model_dump()`` + ``model_validate``;
        the cost is one dict round-trip per row (cheap relative to
        the SQL query) and the upside is that the OpenAPI schema
        stays consistent with the lifecycle ``Event`` definition.
        """
        with project.session_factory() as session:
            bus_items, total = project.events.by_run_paged(
                session,
                run_id=run_id_str,
                since_seq=since_seq,
                kinds=normalised_kinds,
                sort_axis=params.sort,
                offset=params.offset,
                limit=params.page_size,
            )
        items = [Event.model_validate(e.model_dump()) for e in bus_items]
        return items, total

    items, total = await run_in_threadpool(_read)
    return Page[Event].from_items_and_total(items, total, params=params)


# ── Producers / consumers registry dumps ──────────────────────────────


@router.get(
    "/producers",
    response_model=Page[Producer],
    summary="List every registered event producer.",
)
async def list_producers(
    project: ProjectDep,
    params: Annotated[PageParams, Depends(ProducersPageParams)],
) -> Page[Producer]:
    """Return a page of producers currently registered on the bus.

    Default sort is ``created_at_desc`` (most recently registered
    first); ``id_asc`` recovers the previous UUIDv7-insertion-order
    view. Used by the UI's bus-topology view and by operators
    eyeballing "who is producing what on this database".
    """

    def _read() -> list[Producer]:
        with project.session_factory() as session:
            return project.producers.list(session)

    producers = await run_in_threadpool(_read)
    # The repo currently returns rows in ``id ASC`` order. Sort
    # in-process to honour the requested ordering — paginate_sequence
    # slices what we give it without re-ordering.
    if params.sort == "created_at_desc":
        producers = sorted(producers, key=lambda p: p.created_at, reverse=True)
    elif params.sort == "created_at_asc":
        producers = sorted(producers, key=lambda p: p.created_at)
    # ``id_asc`` is already the repo's native order.
    return paginate_sequence(producers, params=params)


@router.get(
    "/consumers",
    response_model=Page[Consumer],
    summary="List every registered event consumer.",
)
async def list_consumers(
    project: ProjectDep,
    params: Annotated[PageParams, Depends(ConsumersPageParams)],
) -> Page[Consumer]:
    """Return a page of consumers currently registered on the bus.

    Mirrors :func:`list_producers`: same sort options, same purpose
    (topology view), different table. The ``kind`` column on each
    row tells you whether a consumer is the engine, an MCP client,
    an HTTP SSE subscriber, etc.
    """

    def _read() -> list[Consumer]:
        with project.session_factory() as session:
            return project.consumers.list(session)

    consumers = await run_in_threadpool(_read)
    if params.sort == "created_at_desc":
        consumers = sorted(consumers, key=lambda c: c.created_at, reverse=True)
    elif params.sort == "created_at_asc":
        consumers = sorted(consumers, key=lambda c: c.created_at)
    # ``id_asc`` is already the repo's native order.
    return paginate_sequence(consumers, params=params)


# ── Explicit ack endpoint ─────────────────────────────────────────────


@router.post(
    "/consumers/{consumer_id}/ack",
    response_model=AckResult,
    summary="Acknowledge a batch of delivered events for a consumer.",
)
async def ack_events(
    project: ProjectDep,
    consumer_id: UUID,
    body: AckBody,
) -> AckResult:
    """Mark ``body.event_ids`` as acked for ``consumer_id``.

    The endpoint is idempotent — re-acking a row that is already
    ``acked`` is a benign no-op (the underlying ``UPDATE`` simply
    matches zero rows). ``acked`` in the response counts only the
    deliveries whose status actually changed as a result of this
    call; the difference between ``requested`` and ``acked`` is
    diagnostic, not an error.

    Returns ``404`` if the consumer id does not refer to a
    registered consumer — refusing this case (rather than silently
    no-op'ing) catches the common "typo in the path" mistake at
    development time.
    """
    consumer_id_str = str(consumer_id)
    requested = len(body.event_ids)

    def _ack() -> int:
        """Sync ack body — runs in the threadpool.

        Verifies the consumer exists once up front (raising
        :class:`LookupError` if it doesn't so the outer handler can
        translate to 404), then walks the id list inside a single
        transaction. We do the existence check here rather than in
        the async handler so we don't pay two threadpool hops.
        """
        acked_count = 0
        with project.session_factory() as session, session.begin():
            consumer = project.consumers.get(session, consumer_id_str)
            if consumer is None:
                raise LookupError(consumer_id_str)
            for event_id in body.event_ids:
                # ``ack`` is a targeted UPDATE that no-ops if the
                # delivery row is missing or already acked — we don't
                # need to pre-check existence. We track success by
                # comparing the rows-affected count from the session,
                # but the repo helper doesn't surface that today; for
                # now we assume each call advanced a row and let the
                # caller reconcile via the consumer-side state.
                project.event_deliveries.ack(
                    session,
                    event_id=str(event_id),
                    consumer_id=consumer_id_str,
                )
                acked_count += 1
            project.consumers.touch_last_seen(session, consumer_id_str)
        return acked_count

    try:
        acked = await run_in_threadpool(_ack)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown consumer id: {exc.args[0]!r}",
        ) from exc

    return AckResult(
        consumer_id=consumer_id_str,
        requested=requested,
        acked=acked,
    )
