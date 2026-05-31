"""W16: active-mcp.json must NOT carry a UI subsystem entry when ``ui/`` is missing.

Regression test for the operator-visible bug that surfaced when
``ctxr-fsm urls`` displayed the supervisor's UI row with a port that
no process actually bound — ⌘-clicking opened a browser tab that
ECONNREFUSED. Root cause was twofold:

* ``_boot_subsystem`` skip-path (W14k BLOCKER-1) returned the picked
  port even though it had not spawned anything, so the caller still
  recorded a port in ``ports["ui"]``.
* The supervisor's ``include_ui`` decision keyed on the ``--mode``
  flag rather than on "did this boot actually produce a UI?".

The fix flips both: skip returns ``port=None`` and the supervisor's
``include_ui`` predicate checks ``ports["ui"] is not None``. This test
locks the contract end-to-end so the next regression surfaces in CI,
not in an operator's browser.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


_BOOT_WAIT_SECONDS: float = 25.0
_POLL_INTERVAL_SECONDS: float = 0.2


def _wait_for_file(path: Path, *, timeout: float) -> bool:
    """Block up to ``timeout`` for ``path`` to appear on disk."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


def _kill_pid_quietly(pid: int) -> None:
    """Best-effort SIGTERM then SIGKILL the supervisor."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    with pytest.MonkeyPatch.context():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_active_mcp_omits_ui_when_no_ui_subtree_present(tmp_path: Path) -> None:
    """A project root with NO ``ui/`` directory must NOT get a ``ui`` key
    in ``.ctxr-fsm/active-mcp.json``. Operators clicking the table URLs
    must never see a Vite port that nobody bound.
    """
    project_root = tmp_path / "consumer"
    project_root.mkdir()

    # Boot the supervisor in dev mode (the mode that would normally
    # include UI) and let it run long enough to publish active-mcp.json,
    # then kill it.
    # Run the supervisor in a fresh subprocess so its signal handlers
    # + event loop don't bleed into pytest's loop. Call ``serve()``
    # via Python directly to dodge any entry-point / venv resolution
    # flake — the supervisor produces active-mcp.json regardless of
    # how it was launched.
    runner_script = (
        "from ctxr.fsm.cli.serve_cmd import serve; "
        "serve(db=None, mode='dev', mcp_only=False)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", runner_script],
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        # Wait for the discovery file to land — its presence means the
        # MCP child answered healthz and the supervisor published.
        active_path = project_root / ".ctxr-fsm" / "active-mcp.json"
        assert _wait_for_file(active_path, timeout=_BOOT_WAIT_SECONDS), (
            f"supervisor never published active-mcp.json within "
            f"{_BOOT_WAIT_SECONDS}s.\n--- stdout ---\n"
            f"{proc.stdout.read().decode() if proc.stdout else ''}"
            "\n--- stderr ---\n"
            f"{proc.stderr.read().decode() if proc.stderr else ''}"
        )
        payload = json.loads(active_path.read_text(encoding="utf-8"))
        subsystems = payload.get("subsystems") or {}
        # mcp + api MUST be present (this is dev mode); ui MUST NOT.
        assert "mcp" in subsystems, f"expected mcp in subsystems; got {sorted(subsystems)}"
        assert "api" in subsystems, f"expected api in subsystems; got {sorted(subsystems)}"
        assert "ui" not in subsystems, (
            f"ui key must NOT appear when no ui/ subtree exists; "
            f"got subsystems={sorted(subsystems)} payload={payload!r}"
        )
    finally:
        _kill_pid_quietly(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
