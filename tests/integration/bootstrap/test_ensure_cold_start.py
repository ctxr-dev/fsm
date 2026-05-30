"""Cold-start integration: ``ctxr-fsm ensure`` drives the full pipeline (W14b).

The single end-to-end test in this module verifies the happy-path
contract:

1. Tmpdir project with no ``.ctxr-fsm/``, no client config.
2. Run ``ensure --mode mcp-only --no-memory --no-mcp-config --client none``
   (mcp-only narrows the spawn to JUST the MCP child; the API + UI
   children take much longer to boot under ``uv run`` and the test
   doesn't need them to verify the happy path).
3. Expect status="ready", actions show the init / supervisor steps
   applied, ``active-mcp.json`` lands on disk with the mcp block.
4. Cleanup: SIGTERM the spawned supervisor so the test process exits
   cleanly.

Why mcp-only here
-----------------

The full ``ensure`` spec demands the full trio when ``--mode full``.
Booting all three under ``uv run`` from a cold cache takes 60-90s on
fresh CI runners, which is acceptable for a single end-to-end test
but compounds badly when a future change adds a second integration
case. The mcp-only path exercises every part of the pipeline (init,
supervisor spawn, active-mcp.json publication, ensure summary
assembly) and the warm-path test elsewhere in this suite covers the
``mode=full`` reuse semantics.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

_ENSURE_TIMEOUT_S: float = 120.0
_SHUTDOWN_TIMEOUT_S: float = 30.0


def _kill_pid(pid: int) -> None:
    """Send SIGTERM then SIGKILL; absorb the standard race errors.

    The supervisor's own SIGTERM handler triggers a drain of its
    children, which we want; SIGKILL is the last resort.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    # Wait for graceful exit.
    deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def test_ensure_cold_start_mcp_only(tmp_path: Path) -> None:
    """End-to-end cold start: ensure boots the MCP supervisor and reports ready.

    Asserts the headline contract pieces:

    * ``status: "ready"`` in the JSON summary.
    * ``actions.init = "applied"`` and ``actions.supervisor = "spawned"``.
    * ``.ctxr-fsm/active-mcp.json`` lands on disk with an mcp block.
    * ``subsystems.mcp`` is in the summary with status spawned/ready.
    """
    spawned_pid: int | None = None
    try:
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
                str(tmp_path),
                "--json",
                "--timeout",
                "90",
            ],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=_ENSURE_TIMEOUT_S,
            check=False,
        )

        # Parse stdout. We tolerate a non-zero exit only when the
        # status is genuinely failed; the happy path should be 0.
        if not result.stdout.strip():
            raise AssertionError(
                f"ensure produced no stdout. stderr:\n{result.stderr}"
            )
        summary = json.loads(result.stdout)
        spawned_pid = summary.get("spawned_supervisor_pid")

        assert summary["status"] == "ready", (
            f"expected status=ready; got {summary}\nstderr: {result.stderr}"
        )
        assert result.returncode == 0, (
            f"ensure exit {result.returncode}\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

        # Per-action assertions.
        assert summary["actions"]["init"] == "applied", summary
        assert summary["actions"]["supervisor"] == "spawned", summary

        # Discovery file landed.
        amcp_path = tmp_path / ".ctxr-fsm" / "active-mcp.json"
        assert amcp_path.exists(), (
            f"active-mcp.json missing.\nstderr: {result.stderr}"
        )
        doc = json.loads(amcp_path.read_text(encoding="utf-8"))
        assert "mcp" in doc["subsystems"]

        # Summary has the mcp block.
        assert "mcp" in summary["subsystems"]
        mcp_block = summary["subsystems"]["mcp"]
        assert mcp_block["status"] in ("spawned", "ready")
        assert mcp_block["http_url"].endswith("/sse")
        assert isinstance(mcp_block["pid"], int) and mcp_block["pid"] > 0

    finally:
        # Reap the supervisor we spawned. Walk the pid file too in
        # case the JSON output was malformed.
        if spawned_pid is None:
            pid_file = tmp_path / ".ctxr-fsm" / "pids" / "mcp.pid"
            if pid_file.exists():
                try:
                    pid_payload = json.loads(pid_file.read_text())
                    if isinstance(pid_payload.get("pid"), int):
                        spawned_pid = pid_payload["pid"]
                except json.JSONDecodeError:
                    spawned_pid = None
        if spawned_pid is not None:
            _kill_pid(spawned_pid)


def test_ensure_timeout_failed_when_supervisor_wont_come_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the supervisor never reports healthy, ensure returns status=failed.

    We force this by monkeypatching the in-process ensure body
    (``_spawn_supervisor_detached``) to return immediately without
    starting any subprocess; the discovery file never lands, so the
    wait loop exhausts its budget and ensure reports the failure.
    """
    from ctxr.fsm.cli import ensure_cmd
    from ctxr.fsm.cli.ensure_cmd import run_ensure
    from ctxr.fsm.cli.init_cmd import run_init

    # Pre-init so the init step is "current" (not the failure point).
    run_init(
        db_path=tmp_path / ".ctxr-fsm" / "fsm.db",
        no_memory=True,
        cwd=tmp_path,
    )

    # Spawn = no-op: returns a sentinel pid but starts nothing.
    monkeypatch.setattr(
        ensure_cmd, "_spawn_supervisor_detached",
        lambda **kwargs: 999999,
    )

    summary = run_ensure(
        project_root=tmp_path,
        client="none",
        mode="mcp-only",
        no_memory=True,
        no_mcp_config=True,
        check=False,
        timeout=1.0,  # tight budget so the test stays fast
    )
    assert summary["status"] == "failed", summary
    assert summary["actions"]["supervisor"] == "failed"
    assert "failure_detail" in summary
