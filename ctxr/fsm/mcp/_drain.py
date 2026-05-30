"""Graceful-drain primitives for the ``ctxr-fsm mcp`` server.

When the supervisor wants to restart the MCP child (the source-change
watcher saw a Python file move, the operator triggered a hot reload,
the container runtime sent a graceful shutdown signal) we want the
server to *finish what it has in flight* before it dies — not to drop
half-completed tool calls on the floor.

The contract this module implements is small and three-part:

1. **Reject new work** — once :func:`start_drain` runs, every tool that
   wraps itself with :func:`track_in_flight` (the :func:`drain_aware`
   decorator does this for free) gets to ask :func:`is_draining` up
   front and bail out with the structured ``server_draining`` error
   envelope. Clients see a typed "retry in a moment" response instead
   of a torn-down stdio pipe.

2. **Wait for the in-flight tail** — :func:`wait_for_drain` blocks the
   caller until either (a) every in-flight call has decremented its
   counter or (b) the configured drain timeout elapses. This is the
   pivot the SIGTERM handler hangs on: it flips the drain flag, then
   waits, then exits.

3. **Surface the lifecycle on stderr** — the supervisor tails the
   child's stderr to attribute reload progress in operator-facing
   logs, so we emit one banner when the drain starts (with the
   timeout the operator should expect to wait at most) and one when
   the drain ends (whether by quiescence or by timeout). The banners
   are stable strings so a downstream log-grep on
   ``[ctxr-fsm mcp]`` continues to work.

Why module-level state rather than a class? The MCP server is a
single-process, single-tenant runtime — there is exactly one drain
state machine per server lifetime. A module-global pairs naturally
with the existing :mod:`ctxr.fsm.mcp._state` Project handle pattern
and keeps the call sites trivial (``with track_in_flight(): ...``).

Thread / async safety
---------------------

FastMCP's stdio loop is single-threaded asyncio — every tool body
runs on the same event loop, so a plain ``int`` counter plus a
``threading.Lock`` is more than enough. The lock is held only across
counter mutations (microseconds), never across awaits, so there is no
contention path that could starve the loop. We deliberately do NOT
reach for ``asyncio.Lock`` because the SIGTERM handler that calls
:func:`wait_for_drain` runs *outside* the event loop (signal handlers
in Python execute on the main thread between bytecode instructions)
and asyncio primitives are not safe to touch from there.

The :func:`wait_for_drain` implementation polls the counter in a
short sleep loop rather than blocking on a condition variable — this
keeps the implementation independent of whatever event loop is (or
isn't) running, matches the existing
:func:`ctxr.fsm.mcp.tools_events.subscribe_events` polling cadence,
and is easy to reason about under signal-handler reentrancy.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

__all__ = [
    "DrainState",
    "drain_state",
    "in_flight_count",
    "in_flight_decrement",
    "in_flight_increment",
    "is_draining",
    "reset_drain_state",
    "start_drain",
    "track_in_flight",
    "wait_for_drain",
]


# Logger pinned to this module so an operator can grep
# ``ctxr.fsm.mcp._drain`` in the server's stderr to isolate drain
# events from the rest of the per-tool chatter.
_LOG = logging.getLogger("ctxr.fsm.mcp._drain")


# Default drain budget. Mirrors the value the supervisor passes when it
# triggers a reload — keeping the default here means a direct call to
# :func:`start_drain()` (e.g. from a test or an embedder) gets the same
# generous-but-bounded grace period the real lifecycle uses.
DEFAULT_DRAIN_TIMEOUT_SECONDS: float = 30.0


# Poll cadence for :func:`wait_for_drain`. 50 ms is well below the
# round-trip time of any realistic tool call (the cheapest call —
# ``fsm.healthcheck`` — is ~1 ms when the SQLite cache is warm, so
# polling at 50 ms adds at most a single poll's worth of latency to a
# clean drain) and well above any reasonable scheduler tick so we are
# not pegging a core while idle.
_WAIT_POLL_INTERVAL_SECONDS: float = 0.05


# Settle period after the in-flight counter reaches zero. The
# ``drain_aware`` decorator decrements the counter inside the tool's
# ``finally`` block — *before* the FastMCP transport has had a chance
# to serialise the return value and write the JSON-RPC response back
# down stdout. If we exited the process the instant the counter went
# to zero we would drop the just-completed call's response on the
# floor, and the client would see ``Connection closed`` instead of
# the structured result the worker actually produced.
#
# A small settle window lets the asyncio loop run for one more tick,
# the FastMCP serialiser flush the response, and the stdio writer
# drain its buffer before the supervisor kills the process. 250 ms is
# generous (the typical response is sub-millisecond) but cheap enough
# that it does not measurably extend a hot-reload cycle.
_POST_DRAIN_SETTLE_SECONDS: float = 0.25


# Stable banner text. Centralised so the supervisor's log-tail regex
# (and any future test that asserts the lifecycle) sees one canonical
# string per event. The ``[ctxr-fsm mcp]`` prefix matches the existing
# convention used by other lifecycle subsystems (CLI, supervisor) so a
# grep over the operator's terminal lines up neatly.
_BANNER_START: str = (
    "[ctxr-fsm mcp] draining for reload "
    "(waiting up to {timeout}s for in-flight tool calls)"
)
_BANNER_END: str = "[ctxr-fsm mcp] drain complete; goodbye"


# ---------------------------------------------------------------------------
# State container
# ---------------------------------------------------------------------------


@dataclass
class DrainState:
    """Mutable bookkeeping for the in-flight + drain-start state.

    Attributes
    ----------
    drain_started_at:
        Wall-clock moment :func:`start_drain` was called, or ``None``
        while the server is still accepting traffic. The wall-clock
        timestamp is the canonical "are we draining?" predicate (a
        non-``None`` value means yes); :func:`is_draining` is the
        public-facing accessor that does the predicate test so call
        sites never have to know the field exists.
    in_flight_count:
        How many tool bodies are currently inside a
        :func:`track_in_flight` block. The decorator increments on
        entry, decrements on exit (success or exception). Mutated only
        under :attr:`_lock` so the increment / decrement pair is
        atomic across threads even though the FastMCP loop itself is
        single-threaded — the safety belt is cheap and means a test
        helper that pokes the state from a fixture thread cannot
        observe a torn read.
    drain_timeout_seconds:
        The bound :func:`wait_for_drain` will wait for the counter to
        reach zero before giving up and letting the server exit anyway.
        Stored on the state object (not just passed to
        :func:`start_drain`) so a debugger / test inspector can read
        the configured budget without re-deriving it.
    """

    drain_started_at: datetime | None = None
    in_flight_count: int = 0
    drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS

    # The lock is intentionally a private attribute — call sites should
    # never need to grab it directly; the helpers in this module are
    # the only legitimate way to mutate the counter. ``field(repr=
    # False)`` keeps the ``repr`` of a ``DrainState`` readable when a
    # test logs it on failure.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


# The single module-level state machine. Mirrors the
# :mod:`ctxr.fsm.mcp._state` pattern: one canonical instance per
# process, mutated through small helpers so the binding stays auditable.
_state: DrainState = DrainState()


# ---------------------------------------------------------------------------
# Read-only predicates
# ---------------------------------------------------------------------------


def is_draining() -> bool:
    """Return ``True`` once :func:`start_drain` has flipped the drain flag.

    The check is intentionally cheap (one attribute read) so the
    :func:`drain_aware` decorator can call it on every tool entry
    without measurable overhead. The wall-clock timestamp doubles as
    the truth — a non-``None`` value means a drain is in progress.
    """
    return _state.drain_started_at is not None


def in_flight_count() -> int:
    """Return the current in-flight tool-call count.

    Exposed so tests (and a future ``ctxr-fsm doctor`` surface) can
    observe the counter without reaching into the private
    :data:`_state` attribute. Acquires the lock so the read is
    consistent under concurrent mutation — the cost is a single
    uncontended lock acquisition, well under a microsecond.
    """
    with _state._lock:
        return _state.in_flight_count


def drain_state() -> DrainState:
    """Return the live :class:`DrainState` instance (for tests / inspection).

    Returns the *same* object the module mutates, not a copy — callers
    that want a snapshot should ``copy.copy`` it themselves. The
    helper exists so external code never has to import the private
    :data:`_state` name (which is documented as an implementation
    detail).
    """
    return _state


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------


def in_flight_increment() -> int:
    """Atomically bump the in-flight counter and return the new value.

    The increment runs under :attr:`DrainState._lock` so a concurrent
    decrement cannot interleave between the read and the write. The
    return value is the post-increment count — useful for tests that
    want to assert "the call moved the counter from N to N+1" without
    a separate read.
    """
    with _state._lock:
        _state.in_flight_count += 1
        return _state.in_flight_count


def in_flight_decrement() -> int:
    """Atomically decrement the in-flight counter and return the new value.

    Guards against a programming error that would let the counter go
    negative (which would silently break the drain wait predicate) —
    if a buggy caller decrements one too many times we clamp at zero
    and log a warning rather than poisoning the state. The clamp is
    defensive; the :func:`track_in_flight` context manager pairs
    increments and decrements automatically so the bug should never
    fire in practice.
    """
    with _state._lock:
        if _state.in_flight_count <= 0:
            _LOG.warning(
                "in_flight_decrement called with counter already at 0; "
                "clamping (this indicates an unbalanced track_in_flight)"
            )
            _state.in_flight_count = 0
            return 0
        _state.in_flight_count -= 1
        return _state.in_flight_count


@contextmanager
def track_in_flight() -> Iterator[None]:
    """Context manager that bumps + decrements the in-flight counter.

    Use this around any code path that the supervisor's drain wait
    needs to observe — typically the body of every ``fsm.*`` tool.
    The decrement runs in a ``finally`` so an exception in the tool
    body does not strand the counter above zero (which would make
    :func:`wait_for_drain` hang for the full timeout on the next
    reload).

    Example:

        with track_in_flight():
            return _do_the_work()

    The :func:`drain_aware` decorator wraps every tool with this
    automatically; direct calls to the context manager exist for the
    rare hand-rolled tool body that does something the decorator
    cannot express.
    """
    in_flight_increment()
    try:
        yield
    finally:
        in_flight_decrement()


def start_drain(timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS) -> None:
    """Flip the drain flag and announce the lifecycle on stderr.

    Idempotent: calling :func:`start_drain` twice keeps the *first*
    timestamp and emits the banner only once. That matters because a
    badly-behaved supervisor might re-deliver SIGTERM (operators
    occasionally Ctrl-\\ a stuck process which delivers SIGQUIT but
    Linux's signal coalescing can elide a queued SIGTERM and resend
    it); we want the audit trail to show the first instant the drain
    began, not a confusing later re-flip.

    The ``timeout`` argument is stored on the state object so
    :func:`wait_for_drain` can pull the configured budget without the
    caller having to thread it through a second function call. We
    coerce non-positive values to a tiny epsilon so an operator who
    passes ``0`` (a "drain instantly" intent) still gets one poll
    cycle to flush a near-complete call instead of an immediate exit.
    """
    timeout = max(float(timeout), 0.001)
    if _state.drain_started_at is not None:
        # Already draining — preserve the original timestamp; refresh
        # the timeout in case a second SIGTERM bumps it (operators do
        # occasionally retry with a longer grace period when the
        # first attempt looks stuck).
        _state.drain_timeout_seconds = timeout
        _LOG.debug(
            "start_drain called while already draining; preserving "
            "drain_started_at=%s, refreshing timeout=%s",
            _state.drain_started_at,
            timeout,
        )
        return

    _state.drain_started_at = datetime.now(UTC)
    _state.drain_timeout_seconds = timeout

    # Banner is emitted to stderr explicitly (not through the logging
    # framework) so it appears verbatim regardless of how the server's
    # root logger is configured. The integer cast on the timeout is
    # cosmetic — operators read the banner as "wait up to N seconds"
    # and a clean integer is friendlier than ``30.0``.
    banner = _BANNER_START.format(timeout=int(timeout))
    print(banner, file=sys.stderr, flush=True)
    _LOG.info("drain started; budget %.3fs", timeout)


def wait_for_drain() -> None:
    """Block until the in-flight counter is zero or the budget elapses.

    Polls the counter at :data:`_WAIT_POLL_INTERVAL_SECONDS` cadence so
    the function can be called from a synchronous signal handler
    without needing an event loop. Returns as soon as either condition
    fires; the end-of-drain banner is emitted in both cases so a
    timeout-induced exit and a clean exit both produce a stable line
    on stderr.

    The poll loop is intentionally simple — no condition variable, no
    asyncio primitive — because :mod:`signal` handlers in Python run
    on the main thread between bytecode instructions and cannot touch
    most synchronisation primitives. A short ``time.sleep`` plus a
    counter read is the smallest thing that satisfies every caller.
    """
    deadline = time.monotonic() + _state.drain_timeout_seconds
    drained_cleanly = False
    while True:
        if in_flight_count() == 0:
            drained_cleanly = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _LOG.warning(
                "wait_for_drain timed out with %d call(s) still in flight",
                in_flight_count(),
            )
            break
        # Never sleep past the deadline so a slow shutdown is bounded
        # by the configured timeout, not by the poll interval. The
        # ``min`` keeps the very last cycle short when the budget is
        # nearly exhausted.
        time.sleep(min(_WAIT_POLL_INTERVAL_SECONDS, remaining))

    # Settle window — give the FastMCP transport on the asyncio loop
    # one more tick to serialise + flush the just-completed call's
    # response before the supervisor terminates the process. Only
    # applied on a *clean* drain (counter hit zero) because in the
    # timeout case the in-flight call is still running and there is
    # no response to wait for. See ``_POST_DRAIN_SETTLE_SECONDS`` for
    # the rationale.
    if drained_cleanly:
        time.sleep(_POST_DRAIN_SETTLE_SECONDS)

    print(_BANNER_END, file=sys.stderr, flush=True)
    _LOG.info(
        "drain complete; remaining in-flight count=%d",
        in_flight_count(),
    )


def reset_drain_state() -> None:
    """Reset the module-global state (intended for tests).

    Clears the drain flag, zeroes the in-flight counter, and restores
    the default timeout. Tests call this between cases so a previous
    test's draining server does not bleed into the next case. Not
    intended for production use — the live server's drain is a
    one-shot lifecycle event.
    """
    global _state
    _state = DrainState()
