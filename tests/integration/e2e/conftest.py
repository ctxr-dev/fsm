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

import contextlib
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


def _collect_supervisor_log_tails(project_root: Path) -> str:
    """Return a multi-block string with the tail of every supervisor log.

    Used as diagnostics when a healthz / Vite readiness poll times out:
    if the supervisor's npm child crashed mid-prebundle, the relevant
    error lives in the date-sharded logs under
    ``.ctxr-fsm/logs/<category>/YYYY/MM/DD/...`` and is otherwise
    invisible to the test runner. Best-effort: returns an empty string
    if the logs tree is absent or unreadable.
    """
    logs_root = project_root / ".ctxr-fsm" / "logs"
    if not logs_root.is_dir():
        return ""
    blocks: list[str] = []
    # Cap at the 5 most recently modified .log files anywhere under
    # the logs tree; a healthy run typically has a handful, but a
    # supervisor crash loop can spew dozens.
    log_files = sorted(
        logs_root.rglob("*.log"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )[:5]
    for path in log_files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        tail = data[-4096:].decode("utf-8", errors="replace")
        blocks.append(f"--- tail of {path} ---\n{tail}")
    return "\n".join(blocks)


def _wait_for_url(
    url: str,
    *,
    timeout_s: float,
    accept_status_below: int = 500,
    diag_project_root: Path | None = None,
) -> None:
    """Poll ``url`` until it returns < ``accept_status_below`` or timeout.

    Vite's dev server and uvicorn typically need 1-3s after the
    supervisor reports them as "spawned" before they actually accept
    sockets. The Vite ready signal in particular is asynchronous to
    the child-process PID being live, and on CI cold-start (npm ci +
    esbuild prebundle) the dev server routinely needs 30-60s before
    it binds the port. We poll on a 100ms cadence and, on timeout,
    tail the last few KB of every supervisor log under
    ``<diag_project_root>/.ctxr-fsm/logs/`` into the raised error so
    a real Vite crash is distinguishable from a slow cold-start.
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
    diag = ""
    if diag_project_root is not None:
        tails = _collect_supervisor_log_tails(diag_project_root)
        if tails:
            diag = "\n" + tails
    raise TimeoutError(
        f"timed out after {timeout_s}s waiting for {url} "
        f"(last error: {last_error!r}){diag}"
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
        with contextlib.suppress(OSError):
            pid_file.unlink()


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
        # before they accept connections. On CI cold-start Vite's
        # npm child has to run ``npm ci`` + the esbuild dependency
        # prebundle before binding its port, which routinely takes
        # 30 to 60 seconds; bump the UI budget to 90s and surface
        # the supervisor log tails in the timeout error so a real
        # Vite crash is visible in the failure diagnostics (rather
        # than just a generic ConnectionRefused).
        _wait_for_url(
            f"{api_url}/healthz",
            timeout_s=60.0,
            diag_project_root=project_root,
        )
        _wait_for_url(
            ui_url,
            timeout_s=90.0,
            diag_project_root=project_root,
        )
        yield LiveProject(
            project_root=project_root,
            api_url=api_url,
            ui_url=ui_url,
            mcp_http_url=mcp_block.get("http_url"),
        )
    finally:
        _kill_supervisor(project_root)
        shutil.rmtree(root_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# W23a: Universal "no console errors" autouse fixture
# ---------------------------------------------------------------------------


# URL prefixes / message patterns that legitimately log to console in dev mode
# and don't indicate a real bug. Kept TIGHT to avoid masking actual API
# failures — a broad "Failed to load resource" wildcard would mask the
# /specs TypeError that prompted W23a.
_IGNORED_CONSOLE_PREFIXES: tuple[str, ...] = (
    # Vite HMR client noise when the dev server is reloading or briefly down.
    "[vite] failed to connect to websocket",
    # /favicon.ico 404 is browser-default and not the API's concern.
    "Failed to load resource: the server responded with a status of 404 (Not Found) @ http://",
)


def _is_ignored_console_message(text: str, url: str | None) -> bool:
    """Return True for known-acceptable console errors in dev mode.

    Any error not matched here fails the autouse audit. The list is
    intentionally narrow so a real regression cannot hide behind a
    permissive wildcard.
    """
    if any(text.startswith(p) for p in _IGNORED_CONSOLE_PREFIXES):
        return True
    # favicon misses in particular surface as ``Failed to load resource``
    # with the resource URL in a separate field on some Playwright versions.
    return url is not None and url.endswith("/favicon.ico")


@pytest.fixture(autouse=True)
def _console_audit(request: pytest.FixtureRequest) -> Generator[None]:
    """Fail any e2e test whose page logged a console.error or threw.

    W23a-deep mandate: the /specs TypeError that prompted this wave was
    a console error the existing tests didn't catch. From now on every
    e2e test that requests the ``page`` fixture also asserts that the
    page produced ZERO uncaught errors during the test body. Opt out
    per-test with ``@pytest.mark.allow_console_errors`` (reserved for
    tests deliberately exercising a UI error path).

    The fixture is autouse so collection-time inclusion is implicit;
    tests that DO NOT request ``page`` (no Playwright surface) are
    no-ops because the fixture only activates when ``page`` is in the
    request's resolved fixturenames.
    """
    if request.node.get_closest_marker("allow_console_errors") is not None:
        yield
        return
    # The ``page`` fixture is lazy — if a test never requests it we
    # skip the wiring entirely. Detection: look up the fixture name in
    # the test's resolved fixture set.
    if "page" not in request.fixturenames:
        yield
        return

    # Pull the Playwright page fixture by name (the Playwright pytest
    # integration registers it as a normal pytest fixture).
    page = request.getfixturevalue("page")

    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg) -> None:
        if msg.type != "error":
            return
        url = None
        try:
            url = msg.location.get("url") if msg.location else None
        except Exception:
            url = None
        if _is_ignored_console_message(msg.text, url):
            return
        console_errors.append(msg.text)

    def _on_pageerror(err) -> None:
        page_errors.append(repr(err))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)

    yield

    diag_lines: list[str] = []
    for e in console_errors:
        diag_lines.append(f"  console.error: {e}")
    for e in page_errors:
        diag_lines.append(f"  pageerror:     {e}")
    assert not console_errors and not page_errors, (
        "JS console / uncaught errors during e2e test:\n" + "\n".join(diag_lines)
    )
