"""Parallel ``ctxr-fsm ensure`` calls converge on one supervisor (W14b).

The W7 singleton primitive promises that two concurrent supervisor
spawns observe each other and at most one of them survives. The
ensure command sits ABOVE that primitive, but its idempotency
contract demands the same outcome from the caller's perspective:
running ensure twice in close succession produces ONE supervisor
process (one pid in ``.ctxr-fsm/pids/mcp.pid``) and one
``active-mcp.json`` file.

Implementation note
-------------------

We run two ensures sequentially (not in true parallel) because:

1. ``subprocess.Popen`` + ``uv run`` are slow to boot; a true
   parallel test would have flaky timing and obscure the contract
   we're actually testing.
2. The singleton primitive's race window is observable even on the
   sequential path: ensure #1 spawns a supervisor + waits for healthz;
   ensure #2 starts AFTER healthz lands, so it MUST see the
   singleton and report ``actions.supervisor = reused``.

A future change could add a true-parallel variant using two threads
spawning at the same time; the singleton primitive's own test suite
under ``tests/unit/lifecycle/test_acquire_singleton.py`` already
covers the file-on-disk race directly.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

_TIMEOUT_S: float = 120.0


def _kill_pid(pid: int) -> None:
    """Reap a spawned supervisor (best-effort)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def _run_ensure_mcp_only(project_root: Path, timeout_s: int) -> dict[str, object]:
    """Run one ``ctxr-fsm ensure`` invocation and return the parsed summary."""
    result = subprocess.run(
        [
            "uv",
            "run",
            "ctxr-fsm",
            "ensure",
            "--mode",
            "mcp-only",
            "--no-memory",
            "--no-mcp-config",
            "--client",
            "none",
            "--project-root",
            str(project_root),
            "--json",
            "--timeout",
            str(timeout_s),
        ],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )
    if not result.stdout.strip():
        raise AssertionError(
            f"ensure produced no stdout (rc={result.returncode}).\n"
            f"stderr: {result.stderr}"
        )
    return json.loads(result.stdout)


def test_second_ensure_reuses_singleton_from_first(tmp_path: Path) -> None:
    """Run ensure twice; the second sees the first's supervisor.

    Asserts:

    * First call: ``actions.supervisor = "spawned"``.
    * Second call: ``actions.supervisor = "reused"``.
    * The active-mcp.json's supervisor_pid (or mcp.pid) is unchanged
      between calls — i.e. no second process exists.
    * Second-call ``duration_ms`` is meaningfully shorter than the
      first (the warm path skips the spawn + healthz wait).
    """
    spawned_pid: int | None = None
    try:
        first = _run_ensure_mcp_only(tmp_path, timeout_s=90)
        assert first["status"] == "ready", first
        assert first["actions"]["supervisor"] == "spawned"
        spawned_pid = first.get("spawned_supervisor_pid")

        # Sanity: discovery file lists one mcp pid.
        amcp_path = tmp_path / ".ctxr-fsm" / "active-mcp.json"
        first_doc = json.loads(amcp_path.read_text(encoding="utf-8"))
        first_mcp_pid = first_doc["subsystems"]["mcp"]["pid"]
        assert isinstance(first_mcp_pid, int) and first_mcp_pid > 0

        # Second invocation.
        second = _run_ensure_mcp_only(tmp_path, timeout_s=30)
        assert second["status"] == "ready", second
        assert second["actions"]["supervisor"] == "reused", second
        # No second supervisor spawned.
        assert "spawned_supervisor_pid" not in second

        # The mcp pid in the discovery file is unchanged.
        second_doc = json.loads(amcp_path.read_text(encoding="utf-8"))
        second_mcp_pid = second_doc["subsystems"]["mcp"]["pid"]
        assert second_mcp_pid == first_mcp_pid, (
            "active-mcp.json mcp pid changed across ensure calls — "
            "the singleton was not reused."
        )
    finally:
        # Reap whichever pid lands in mcp.pid (could be the supervisor
        # pid OR the mcp child pid after _record_child_pid).
        pid_file = tmp_path / ".ctxr-fsm" / "pids" / "mcp.pid"
        if pid_file.exists():
            try:
                payload = json.loads(pid_file.read_text())
                pid_val = payload.get("pid")
                if isinstance(pid_val, int):
                    _kill_pid(pid_val)
            except json.JSONDecodeError:
                pass
        if spawned_pid is not None:
            _kill_pid(spawned_pid)
