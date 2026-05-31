"""The UI ships INSIDE the ctxr-fsm package; consumer projects always get it.

Originally this test asserted the OPPOSITE: when a consumer project
had no ``ui/`` subtree, the supervisor's discovery file should omit
the UI subsystem. That assertion was WRONG and reflected a wrong
architectural model — the UI is part of the ctxr-fsm package (W6),
not of the consumer. Consumer projects don't carry their own ``ui/``
because there's nothing to carry; the package's Vite source serves
the dashboard regardless of which consumer project the supervisor
is pointed at. The W14k BLOCKER-1 "skip when ui/ missing" path
(which this test was supposedly locking) is now reserved for the
extreme case where the PACKAGE's own ``ui/`` was excluded from the
install (a malformed wheel) — and emits a clear advisory instead.

This rewritten test pins the correct architecture: a consumer
project with no local ``ui/`` STILL gets a ``ui`` subsystem entry
in ``active-mcp.json``, because the UI is the package's, not the
project's.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_BOOT_WAIT_SECONDS: float = 35.0
_POLL_INTERVAL_SECONDS: float = 0.2


def _wait_for_file(path: Path, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


def _kill_pid_quietly(pid: int) -> None:
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
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def test_consumer_project_gets_ui_subsystem_from_package_source(
    tmp_path: Path,
) -> None:
    """A consumer project with NO local ``ui/`` STILL gets a ``ui``
    subsystem entry in ``active-mcp.json``, because the UI ships from
    the ctxr-fsm package itself (W6) — the consumer never carries one
    because there's nothing for it to carry. Without this contract,
    consumer-project dashboards are silently absent.
    """
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    # No ``ui/`` directory anywhere under project_root.
    assert not (project_root / "ui").exists()

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

        # mcp + api + UI all present — the UI ships from the package.
        assert "mcp" in subsystems, f"mcp missing; got {sorted(subsystems)}"
        assert "api" in subsystems, f"api missing; got {sorted(subsystems)}"
        assert "ui" in subsystems, (
            f"ui must appear in subsystems because the UI ships from "
            f"the ctxr-fsm package, not from the consumer project. "
            f"Got subsystems={sorted(subsystems)} payload={payload!r}"
        )
        ui_block = subsystems["ui"]
        # The UI was actually spawned (pid set + URL set).
        assert isinstance(ui_block.get("pid"), int), (
            f"ui pid should be set; got ui_block={ui_block!r}"
        )
        ui_url = ui_block.get("http_url")
        assert isinstance(ui_url, str) and ui_url.startswith("http://"), (
            f"ui http_url should be a real URL; got {ui_url!r}"
        )
    finally:
        _kill_pid_quietly(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_locate_package_ui_dir_resolves_for_this_install() -> None:
    """The supervisor's UI-locator must resolve to a real directory with
    a ``package.json`` in THIS installation (editable / sibling-linked /
    sdist). If this assertion ever fails, the supervisor will silently
    skip UI on every consumer project — exactly the bug the user just
    flagged. Lock the contract here.
    """
    from ctxr.fsm.cli.lifecycle.supervisor import _locate_package_ui_dir

    located = _locate_package_ui_dir()
    assert located is not None, (
        "could not locate the package-owned ui/ directory; the "
        "supervisor would skip UI for every consumer project + the "
        "operator would see no dashboard"
    )
    assert (located / "package.json").is_file(), (
        f"located {located} but it has no package.json"
    )
