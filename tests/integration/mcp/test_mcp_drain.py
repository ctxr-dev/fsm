"""Integration test: graceful SIGTERM drain for the MCP server.

The W7 service-lifecycle wave wires the supervisor: when watchfiles
sees a source change, the supervisor sends ``SIGTERM`` to the MCP
child. The child's contract is to:

1. Flip the drain flag (so subsequent tool calls return the structured
   ``server_draining`` error envelope rather than starting new work).
2. Let any in-flight tool call finish, bounded by a configurable
   timeout (default 30 s).
3. Close the :class:`Project` and exit with status 0.

This test exercises that lifecycle end-to-end:

* Spawn a custom Python wrapper that monkey-patches
  ``ctxr.fsm.mcp.tools_meta.fsm_healthcheck`` to sleep for
  :data:`_SLOW_BODY_SLEEP_SECONDS`, then calls the real MCP server
  ``main()``. The wrapper is launched via ``uv run python <wrapper>``
  so the project's virtualenv is honoured.
* Connect to the child via the official ``mcp`` SDK's stdio client.
* Dispatch a slow ``fsm.healthcheck`` call, wait briefly, send
  SIGTERM to the child, dispatch a *second* call (which must come
  back as the ``server_draining`` envelope), then await the slow
  call's clean completion and the child's exit code.

The patch happens *before* the FastMCP tool decoration runs, so the
slow body is what gets registered as ``fsm.healthcheck`` on the
FastMCP instance. That sidesteps the trickier "patch a function
after it has already been captured by a decorator" problem.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# How long each MCP round-trip is allowed to block before the test
# declares the server hung. Generous because the spawn + handshake on
# a cold cache can land near 10 s, and we layer a 2 s "slow" body on
# top of that for the in-flight call.
_ROUND_TRIP_TIMEOUT_SECONDS: float = 60.0


# How long the monkey-patched healthcheck body sleeps. Comfortable
# window for the test driver to send SIGTERM while the call is
# in-flight without making the suite slow.
_SLOW_BODY_SLEEP_SECONDS: float = 2.0


# Time we give the child to exit after sending SIGTERM. Must exceed
# the slow-body sleep + the drain banner emit time.
_CHILD_EXIT_TIMEOUT_SECONDS: float = 15.0


# ---------------------------------------------------------------------------
# Wrapper script that monkey-patches + launches the MCP server
# ---------------------------------------------------------------------------


def _wrapper_script(db_path: Path, sleep_seconds: float) -> str:
    """Generate the Python source for the MCP-server wrapper.

    The wrapper does three things in order:

    1. Import :mod:`ctxr.fsm.mcp.tools_meta` early, replace its
       ``fsm_healthcheck`` symbol with a wrapper that sleeps for
       ``sleep_seconds`` and then calls the original. This must
       happen *before* :mod:`ctxr.fsm.mcp` is imported (which is
       what kicks off the tool-registration import side effects on
       FastMCP) so the registered tool is the slow version.
    2. Patch the FastMCP-registered tool entry too — FastMCP stores
       its own reference to the decorated function in
       ``mcp._tool_manager._tools["fsm.healthcheck"].fn``, so we
       overwrite that attribute as well so the wrapped slow body
       runs on every dispatch.
    3. Call :func:`ctxr.fsm.mcp.server.main` with ``--db <db_path>``.

    The wrapper is written as a self-contained script (rather than a
    sibling test module) because it needs to run inside the spawned
    child's interpreter, which has its own import path.
    """
    return textwrap.dedent(
        f"""
        import sys
        import time
        from pathlib import Path

        # Slow-down value injected by the test harness.
        _SLEEP_S = {sleep_seconds!r}

        # Import the tools module so the @mcp.tool() / @drain_aware
        # decoration has produced the FastMCP entry. We then wrap the
        # FastMCP-registered handler (which IS the drain_aware
        # wrapper) with a thin sleep — keeping the drain_aware
        # semantics intact while making the body slow enough to
        # observe in a SIGTERM drain race.
        from ctxr.fsm.mcp import mcp as _mcp_inst
        from ctxr.fsm.mcp import tools_meta  # noqa: F401  (decorator side-effect)

        _tm = getattr(_mcp_inst, "_tool_manager", None)
        assert _tm is not None, "FastMCP _tool_manager missing"
        _tools = getattr(_tm, "_tools", None) or {{}}
        _entry = _tools.get("fsm.healthcheck")
        assert _entry is not None, "fsm.healthcheck not registered on FastMCP"

        # Replace the registered tool with an ASYNC slow body wrapped
        # by ``drain_aware``. Sync tool bodies in FastMCP execute on
        # the main thread inside the event loop, so a ``time.sleep``
        # would block the loop and prevent the SIGTERM-handler-driven
        # second call from being received by the server while the
        # slow call is in flight. ``asyncio.sleep`` yields the loop so
        # the signal handler, the drain check on the second call, and
        # the eventual decrement of the in-flight counter all
        # interleave correctly — which is exactly the race the test
        # wants to exercise.
        import asyncio as _asyncio

        from ctxr.fsm.mcp._drain_decorator import drain_aware
        from ctxr.fsm.mcp.tools_meta import fsm_healthcheck as _real_body

        # ``_real_body`` is the result of the decorator stack — the
        # outer ``@mcp.tool()`` returned the bare-body function, so
        # the module global points at the @drain_aware-wrapped
        # callable. Walk back one level via ``__wrapped__`` to reach
        # the bare body, splice in our async sleep, then re-decorate.
        _bare_body = getattr(_real_body, "__wrapped__", _real_body)

        async def _slow_bare(*args, **kwargs):
            await _asyncio.sleep(_SLEEP_S)
            return _bare_body(*args, **kwargs)

        _entry.fn = drain_aware(_slow_bare)
        # ``is_async`` is the flag FastMCP reads on dispatch to decide
        # whether to ``await`` the callable; we flip it because we
        # just installed an async wrapper in place of a sync one.
        _entry.is_async = True

        # Hand off to the real server entry point.
        from ctxr.fsm.mcp.server import main
        main(transport="stdio", db_path=Path({str(db_path)!r}))
        """
    ).strip()


def _write_wrapper(wrapper_dir: Path, db_path: Path, sleep_seconds: float) -> Path:
    """Drop the wrapper script into ``wrapper_dir`` and return its path."""
    wrapper_path = wrapper_dir / "_mcp_wrapper.py"
    wrapper_path.write_text(
        _wrapper_script(db_path, sleep_seconds), encoding="utf-8"
    )
    return wrapper_path


def _server_params(wrapper_path: Path) -> StdioServerParameters:
    """Build the launch parameters for the wrapper-launched MCP child."""
    return StdioServerParameters(
        command="uv",
        args=["run", "python", str(wrapper_path)],
        env=dict(os.environ),
    )


# ---------------------------------------------------------------------------
# Helpers for tool-call envelopes
# ---------------------------------------------------------------------------


def _extract_payload(envelope: Any) -> dict[str, Any]:
    """Pull the FastMCP-wrapped ``result`` dict out of a CallToolResult."""
    sc = envelope.structuredContent
    assert sc is not None, f"missing structuredContent; envelope={envelope!r}"
    assert "result" in sc, f"structuredContent missing 'result' wrapper; got={sc!r}"
    payload = sc["result"]
    assert isinstance(payload, dict), f"'result' should be dict; got={type(payload).__name__}"
    return payload


# ---------------------------------------------------------------------------
# Child-PID discovery + exit waiting
# ---------------------------------------------------------------------------


def _discover_child_pid() -> int | None:
    """Best-effort discovery of the MCP child PID spawned by stdio_client.

    The mcp SDK does not expose the PID directly on every version, so
    we scan our own process tree for the deepest descendant whose
    command line includes the wrapper script name. On macOS / Linux
    that walk is reliable because there is exactly one such child
    per test (uv -> python wrapper).
    """
    try:
        our_pid = os.getpid()

        def _children(pid: int) -> list[int]:
            res = subprocess.run(
                ["pgrep", "-P", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
            return [int(x) for x in res.stdout.split() if x.strip()]

        def _cmdline(pid: int) -> str:
            res = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
            return res.stdout.strip()

        # Walk the entire descendant tree.
        all_descendants: list[int] = []
        frontier = _children(our_pid)
        while frontier:
            pid = frontier.pop(0)
            all_descendants.append(pid)
            frontier.extend(_children(pid))

        # Prefer the deepest descendant matching the wrapper marker.
        for pid in reversed(all_descendants):
            cmd = _cmdline(pid)
            if "_mcp_wrapper.py" in cmd:
                return pid

        # Fall back to deepest descendant.
        return all_descendants[-1] if all_descendants else None
    except Exception:  # pragma: no cover - diagnostic path
        return None


def _wait_for_pid_gone(pid: int | None, timeout: float) -> bool:
    """Poll ``kill(pid, 0)`` until ``pid`` is gone or ``timeout`` elapses.

    Returns ``True`` if the process exited within the budget. We use
    a liveness poll (rather than ``waitpid``) because the wrapper is
    typically a grandchild of the test process — ``uv run python``
    spawns ``python``, and only ``uv`` is our direct child. The
    grandchild's exit status is reaped by ``uv``, not by us.
    """
    if pid is None:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Async driver
# ---------------------------------------------------------------------------


async def _drive_drain(wrapper_path: Path) -> dict[str, Any]:
    """Spawn the MCP child, drive the drain lifecycle, return observations.

    Flow:
      1. Spawn the child and complete the MCP handshake.
      2. Start an in-flight ``fsm.healthcheck`` call (it will sleep
         for :data:`_SLOW_BODY_SLEEP_SECONDS` inside the child).
      3. Sleep briefly to ensure the call is actually in flight.
      4. SIGTERM the child.
      5. Try a *second* ``fsm.healthcheck`` call — it must come back
         as the ``server_draining`` error envelope (and quickly).
      6. Await the in-flight call's result — it must be the
         happy-path healthcheck payload.
      7. Wait for the child process to disappear and record whether
         the exit happened within the drain budget.
    """
    params = _server_params(wrapper_path)

    observations: dict[str, Any] = {
        "slow_call_payload": None,
        "drain_call_payload": None,
        "drain_call_is_error": None,
        "child_exited_cleanly": False,
        "child_pid": None,
    }

    async with stdio_client(params) as (read, write):
        # Give the OS a moment to spawn the wrapper process — pgrep
        # needs the process to exist on disk before it can be found.
        await asyncio.sleep(0.1)
        child_pid = _discover_child_pid()
        observations["child_pid"] = child_pid

        async with ClientSession(read, write) as session:
            await session.initialize()

            # If we still don't have the PID, try one more time now
            # that the handshake has definitely spun the child up.
            if child_pid is None:
                child_pid = _discover_child_pid()
                observations["child_pid"] = child_pid

            assert child_pid is not None, (
                "could not discover MCP wrapper PID; cannot deliver SIGTERM"
            )

            # Kick off the in-flight (slow) call. Don't await yet.
            slow_task = asyncio.create_task(
                session.call_tool("fsm.healthcheck", {})
            )

            # Give the child a moment to start the slow body. 300 ms
            # is comfortably more than the FastMCP dispatch overhead
            # and well under the slow-body sleep.
            await asyncio.sleep(0.3)

            # Deliver SIGTERM. The child's handler flips the drain
            # flag and then begins waiting for the in-flight slot.
            os.kill(child_pid, signal.SIGTERM)

            # Give the signal handler a moment to actually flip the
            # drain flag inside the child. The signal is processed
            # between bytecode instructions on the main thread; 300
            # ms is plenty for that to happen even under load.
            await asyncio.sleep(0.3)

            # Try a second call — it should be refused with the
            # structured ``server_draining`` envelope.
            try:
                drain_envelope = await asyncio.wait_for(
                    session.call_tool("fsm.healthcheck", {}),
                    timeout=_ROUND_TRIP_TIMEOUT_SECONDS,
                )
                observations["drain_call_is_error"] = bool(drain_envelope.isError)
                if drain_envelope.structuredContent is not None:
                    observations["drain_call_payload"] = (
                        drain_envelope.structuredContent.get("result")
                    )
                else:
                    # FastMCP renders error envelopes as content too —
                    # capture the text so the assertion has something
                    # actionable to display.
                    observations["drain_call_payload"] = {
                        "raw_content": [
                            getattr(c, "text", None) for c in drain_envelope.content
                        ]
                    }
            except Exception as exc:  # pragma: no cover - diagnostic path
                observations["drain_call_payload"] = {"call_exception": repr(exc)}

            # Now await the slow call — it should have completed
            # successfully despite the drain.
            try:
                slow_envelope = await asyncio.wait_for(
                    slow_task, timeout=_ROUND_TRIP_TIMEOUT_SECONDS
                )
                if slow_envelope.isError is False and slow_envelope.structuredContent:
                    observations["slow_call_payload"] = _extract_payload(slow_envelope)
                else:
                    observations["slow_call_payload"] = {
                        "is_error": slow_envelope.isError,
                        "structuredContent": slow_envelope.structuredContent,
                    }
            except Exception as exc:  # pragma: no cover - diagnostic path
                observations["slow_call_payload"] = {"slow_call_exception": repr(exc)}

    # Outside the stdio_client context the child has been signalled
    # and is on its way out. Poll for the process to disappear so we
    # can confirm a clean exit happened within the drain budget.
    observations["child_exited_cleanly"] = _wait_for_pid_gone(
        observations["child_pid"], timeout=_CHILD_EXIT_TIMEOUT_SECONDS
    )
    return observations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mcp_sigterm_drains_in_flight_and_rejects_new_calls() -> None:
    """End-to-end: SIGTERM drains the in-flight call, rejects the new one.

    Asserts:
    * The in-flight ``fsm.healthcheck`` call completes successfully
      despite being caught by the SIGTERM.
    * A new tool call dispatched after SIGTERM is refused with the
      structured ``server_draining`` error envelope.
    * The child process exits cleanly within the drain budget.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "fsm.db"

        wrapper_dir = tmp_path / "wrapper"
        wrapper_dir.mkdir()
        wrapper_path = _write_wrapper(
            wrapper_dir, db_path, _SLOW_BODY_SLEEP_SECONDS
        )

        observations = asyncio.run(
            asyncio.wait_for(
                _drive_drain(wrapper_path),
                timeout=_ROUND_TRIP_TIMEOUT_SECONDS + _CHILD_EXIT_TIMEOUT_SECONDS + 30.0,
            )
        )

    # ── in-flight call must have completed cleanly ────────────────
    slow_payload = observations["slow_call_payload"]
    assert slow_payload is not None, (
        f"in-flight healthcheck call returned no payload; observations={observations!r}"
    )
    assert slow_payload.get("status") == "ok", (
        f"in-flight healthcheck did not return status=ok; payload={slow_payload!r}, "
        f"observations={observations!r}"
    )

    # ── drain-time call must have been rejected with server_draining ─
    drain_payload = observations["drain_call_payload"]
    assert drain_payload is not None, (
        f"drain-time call returned no payload; observations={observations!r}"
    )
    # FastMCP wraps an ``McpToolError`` return value as
    # ``structuredContent.result = {"error": ..., "detail": ..., ...}``.
    # We assert on the ``error`` field directly so the contract is
    # plain.
    assert drain_payload.get("error") == "server_draining", (
        f"drain-time call did not return server_draining envelope; "
        f"payload={drain_payload!r}, observations={observations!r}"
    )

    # ── child exited cleanly within the drain budget ──────────────
    assert observations["child_exited_cleanly"], (
        f"MCP child did not disappear within drain budget; "
        f"observations={observations!r}"
    )


if __name__ == "__main__":  # pragma: no cover - manual debug path
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
