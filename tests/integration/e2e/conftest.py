"""Shared E2E fixtures: tmp-project materialisation + supervisor lifecycle.

The ``live_project`` fixture is the workhorse: it materialises the
bundled fixture project into a session-scoped tmpdir, runs
``ctxr-fsm ensure --json`` against it to bring up the MCP + API + UI
subsystems, parses the JSON envelope, and yields a ``LiveProject``
DTO carrying the project root and subsystem URLs. Teardown wipes the
tmpdir and SIGKILLs any supervisor PID files that survive shutdown.

Skips are surfaced with actionable hints rather than swallowed: a
missing Playwright install reads as "install the e2e group + run
playwright install chromium" instead of as a silent green.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

# Option registration + the ``@pytest.mark.e2e`` auto-skip live in the
# root conftest (``tests/conftest.py``) so pytest discovers them
# regardless of which path the user invoked the runner from. This file
# only defines the e2e-specific fixtures.


# ---------------------------------------------------------------------------
# DTO + helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveProject:
    """A materialised + supervised tmp project ready for E2E driving.

    ``ui_url`` is the Vite dev-server URL the supervisor brought up
    (e.g. ``http://127.0.0.1:5173``). ``api_url`` is the FastAPI base
    URL (``http://127.0.0.1:<port>``); pair it with ``/api/v1/runs``
    etc. for direct REST hits in tests that want to assert on
    server-side state before exercising the UI.
    """

    project_root: Path
    api_url: str
    ui_url: str
    mcp_http_url: str | None


def _run_ensure(project_root: Path, timeout_s: float = 60.0) -> dict:
    """Spawn ``ctxr-fsm ensure --no-table --json`` and parse the envelope."""
    # ``--client none`` disables MCP-client config writes (we don't
    # want the test fixture mutating the developer's CLAUDE/Codex/
    # Cursor configs), and ``--no-memory`` skips the install-memory
    # step (which is otherwise incompatible with ``--client none``).
    # The combination keeps the test hermetic.
    result = subprocess.run(
        [
            "uv",
            "run",
            "ctxr-fsm",
            "ensure",
            "--no-table",
            "--json",
            "--client",
            "none",
            "--no-memory",
        ],
        cwd=project_root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ctxr-fsm ensure failed (rc={result.returncode}) in {project_root}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    # ensure prints a single JSON document on stdout when --json is set.
    return json.loads(result.stdout)


def _wait_for_url(url: str, *, timeout_s: float, accept_status_below: int = 500) -> None:
    """Poll ``url`` until it returns < ``accept_status_below`` or timeout.

    Vite's dev server and uvicorn typically need 1-3s after the
    supervisor reports them as "spawned" before they actually accept
    sockets. The Vite ready signal in particular is asynchronous to
    the child-process PID being live. We poll on a 100ms cadence.
    """
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status < accept_status_below:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise TimeoutError(
        f"timed out after {timeout_s}s waiting for {url} "
        f"(last error: {last_error!r})"
    )


def _kill_supervisor(project_root: Path) -> None:
    """Tear down any supervisor PIDs the ensure call left behind.

    Best-effort: missing pid files / already-dead pids are non-errors.
    """
    pids_dir = project_root / ".ctxr-fsm" / "pids"
    if not pids_dir.is_dir():
        return
    for pid_file in pids_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().split("\n", 1)[0].strip())
        except (ValueError, OSError):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        with pid_file.open("a"):
            pass
        try:
            pid_file.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_project(tmp_path_factory: pytest.TempPathFactory) -> Generator[LiveProject]:
    """Session-scoped supervised tmp project.

    Materialises the bundled ``fixture_project/`` template, spins up
    MCP + API + UI via ``ctxr-fsm ensure``, and yields URLs for the
    test session. Tears the supervisor down + wipes the tmpdir at
    session end.

    Single fixture per session because supervisor spin-up takes
    several seconds; sharing the live UI across tests is fine
    because each test seeds its own runs (different run_ids, no
    cross-test data dependencies in the queries the UI makes).
    """
    # Import lazily so this module is importable on machines without
    # the e2e deps (the collection-time skip above relies on that).
    from ctxr.fsm.testing import materialise_fixture_project

    root_dir = tmp_path_factory.mktemp("ctxr_fsm_e2e_")
    project_root = root_dir / "project"
    materialise_fixture_project(project_root)

    try:
        envelope = _run_ensure(project_root)
        subsystems = envelope.get("subsystems") or {}
        api_block = subsystems.get("api") or {}
        ui_block = subsystems.get("ui") or {}
        mcp_block = subsystems.get("mcp") or {}
        api_url = api_block.get("http_url")
        ui_url = ui_block.get("http_url")
        if not api_url or not ui_url:
            raise RuntimeError(
                f"ensure did not surface api/ui URLs; envelope: {envelope!r}"
            )
        # ensure reports subsystems as "spawned" once the child
        # process is alive, but Vite + uvicorn need a moment more
        # before they accept connections. Poll healthz before
        # handing off to tests.
        _wait_for_url(f"{api_url}/healthz", timeout_s=20.0)
        _wait_for_url(ui_url, timeout_s=20.0)
        yield LiveProject(
            project_root=project_root,
            api_url=api_url,
            ui_url=ui_url,
            mcp_http_url=mcp_block.get("http_url"),
        )
    finally:
        _kill_supervisor(project_root)
        shutil.rmtree(root_dir, ignore_errors=True)
