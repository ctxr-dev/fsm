"""End-to-end lifecycle coverage for ``.ctxr-fsm/active-mcp.json`` (W14c).

The discovery file is the only thing standing between the W14e skill
bootstrap discipline and "agent stares at a blank prompt because the
stdio MCP entry was registered too recently to be effective". So this
test exercises the full contract:

1. ``ctxr-fsm serve`` boots → ``.ctxr-fsm/active-mcp.json`` exists,
   parses as JSON, declares MCP+API+UI subsystems with the right
   shape (http_url / healthz_url / pid; docs_url for API; ui's
   healthz_url is null because Vite has no /healthz route).
2. The file is atomically written (no half-written document
   observable mid-rename — covered by reading the file repeatedly
   while the supervisor is up).
3. A graceful SIGTERM shutdown removes the file so a later ``ensure``
   call doesn't talk to a dead port.
4. ``ctxr-fsm doctor`` surfaces the file under
   ``supervisor.active_mcp`` while the supervisor is up.

Why a real subprocess
---------------------

We piggy-back on the same fixture pattern as
``test_serve_reuse.py``: spawning the actual ``uv run ctxr-fsm
serve`` because the discovery contract spans the supervisor module +
the on-disk filesystem, and an in-process facsimile would short-circuit
the very atomic-write + signal-handler interactions we want to cover.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Tunables (mirror test_serve_reuse — kept in sync deliberately)
# ---------------------------------------------------------------------------

_BOOT_TIMEOUT_S: float = 90.0
_SHUTDOWN_TIMEOUT_S: float = 30.0
_BOOT_BANNER_PREFIX: str = "[ctxr-fsm supervisor] booted:"
_ACTIVE_MCP_FILE_REL: Path = Path(".ctxr-fsm") / "active-mcp.json"


# ---------------------------------------------------------------------------
# Stub project layout (reuses the test_serve_reuse contract)
# ---------------------------------------------------------------------------

_STUB_UI_PACKAGE_JSON: str = json.dumps(
    {
        "name": "ctxr-fsm-test-stub-ui",
        "private": True,
        "version": "0.0.0",
        "scripts": {
            "dev": "python3 -c \"import sys, time; sys.stdout.flush(); time.sleep(3600)\"",
        },
    },
    indent=2,
)


def _stage_project_root(tmpdir: Path) -> Path:
    """Build the minimal tree the supervisor's boot path walks.

    Mirror of the test_serve_reuse helper. Pre-creates ``.ctxr-fsm/pids``
    so the test can stat the discovery file beside it without having
    to wait for the first write to create the directory.
    """
    (tmpdir / ".ctxr-fsm" / "pids").mkdir(parents=True, exist_ok=True)
    (tmpdir / "ctxr" / "fsm").mkdir(parents=True, exist_ok=True)
    (tmpdir / "ui").mkdir(parents=True, exist_ok=True)
    (tmpdir / "ui" / "package.json").write_text(
        _STUB_UI_PACKAGE_JSON, encoding="utf-8"
    )
    return tmpdir


# ---------------------------------------------------------------------------
# Stderr drainer (slimmed clone of the one in test_serve_reuse)
# ---------------------------------------------------------------------------


class _StderrDrain:
    """Background pump that copies a subprocess's stderr into a buffer.

    See ``test_serve_reuse._StderrDrain`` for the full rationale. We
    duplicate it here (rather than reach across test modules) so each
    integration file stays self-contained and a regression in the
    drainer surfaces as one focused failure rather than rippling
    through every integration test that needs to wait on a log line.
    """

    def __init__(self, stream: object, label: str) -> None:
        self._stream = stream
        self._label = label
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._event = threading.Event()
        self._thread = threading.Thread(
            target=self._pump, daemon=True, name=f"drain[{label}]"
        )

    def start(self) -> None:
        self._thread.start()

    def _pump(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                try:
                    line = raw.decode("utf-8", errors="replace")
                except Exception:  # pragma: no cover - decode defensive
                    line = repr(raw)
                with self._lock:
                    self._buffer.append(line)
                self._event.set()
        except (ValueError, OSError):
            return

    def wait_for(self, substring: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                joined = "".join(self._buffer)
            if substring in joined:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._event.clear()
            self._event.wait(timeout=min(remaining, 0.5))

    def snapshot(self) -> str:
        with self._lock:
            return "".join(self._buffer)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _supervisor_argv(db_path: Path, *, mode: str = "dev") -> list[str]:
    """``uv run ctxr-fsm serve --mode <mode> --db <db>``."""
    return [
        "uv",
        "run",
        "ctxr-fsm",
        "serve",
        "--mode",
        mode,
        "--db",
        str(db_path),
    ]


@contextmanager
def _spawn_supervisor(
    *, project_root: Path, db_path: Path, label: str, mode: str = "dev"
) -> Iterator[tuple[subprocess.Popen[bytes], _StderrDrain]]:
    """Yield a ``(Popen, drainer)`` pair scoped to a single supervisor.

    Wraps spawn + drain + shutdown so a botched test never strands a
    supervisor holding a port. SIGTERM lets the supervisor run its own
    drain (releasing pid files + removing ``active-mcp.json``); the
    drain budget mirrors the supervisor's own (5s per child + 5s
    grace) plus generous slack for slow CI.
    """
    env = os.environ.copy()
    proc = subprocess.Popen(
        _supervisor_argv(db_path, mode=mode),
        cwd=str(project_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    drain = _StderrDrain(proc.stderr, label=f"{label}.stderr")
    drain.start()
    stdout_drain = _StderrDrain(proc.stdout, label=f"{label}.stdout")
    stdout_drain.start()
    try:
        yield proc, drain
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):  # pragma: no cover
                proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _wait_for_file(path: Path, *, timeout: float) -> bool:
    """Poll ``path`` until it exists or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_active_mcp_json_written_on_boot_and_removed_on_shutdown() -> None:
    """The supervisor writes the discovery file on boot and removes it on shutdown.

    Walks the full lifecycle in one test so the write and remove
    contracts share a fixture (a separate "boot only" test would
    leave a supervisor running, and a separate "shutdown only" test
    couldn't observe the post-boot file without re-implementing the
    boot wait).
    """
    with tempfile.TemporaryDirectory(prefix="ctxr-fsm-amcp-") as tmpdir:
        project_root = _stage_project_root(Path(tmpdir))
        db_path = project_root / "fsm.db"
        active_mcp = project_root / _ACTIVE_MCP_FILE_REL

        # Sanity: no discovery file before the supervisor runs.
        assert not active_mcp.exists()

        with _spawn_supervisor(
            project_root=project_root, db_path=db_path, label="serve"
        ) as (proc, drain):
            booted = drain.wait_for(_BOOT_BANNER_PREFIX, timeout=_BOOT_TIMEOUT_S)
            assert booted, (
                f"supervisor never emitted {_BOOT_BANNER_PREFIX!r} within "
                f"{_BOOT_TIMEOUT_S:.0f}s.\n--- stderr ---\n{drain.snapshot()}"
            )
            assert proc.poll() is None, (
                f"supervisor died after boot (rc={proc.returncode}).\n"
                f"--- stderr ---\n{drain.snapshot()}"
            )

            # The supervisor publishes the discovery file only after
            # MCP's /healthz responds; on a cold ``uv run`` boot that
            # write can land a few hundred ms after the banner. Give it
            # a generous waiting window (still well under the boot
            # budget so a real failure doesn't hide behind an
            # over-generous timeout).
            assert _wait_for_file(active_mcp, timeout=10.0), (
                f"active-mcp.json was not written within 10s of boot.\n"
                f"--- stderr ---\n{drain.snapshot()}"
            )

            payload = json.loads(active_mcp.read_text(encoding="utf-8"))

            # Top-level schema.
            assert set(payload.keys()) >= {
                "started_at",
                "supervisor_pid",
                "version",
                "subsystems",
            }, payload
            assert isinstance(payload["started_at"], str) and payload[
                "started_at"
            ].endswith("+00:00")
            assert isinstance(payload["supervisor_pid"], int)
            assert payload["supervisor_pid"] > 0
            assert isinstance(payload["version"], str)
            assert isinstance(payload["subsystems"], dict)

            # All three subsystems present in dev mode.
            sub = payload["subsystems"]
            assert set(sub.keys()) == {"mcp", "api", "ui"}, sub.keys()

            # MCP block shape.
            mcp_block = sub["mcp"]
            assert mcp_block["http_url"].startswith("http://127.0.0.1:")
            assert mcp_block["http_url"].endswith("/sse")
            assert mcp_block["healthz_url"].startswith("http://127.0.0.1:")
            assert mcp_block["healthz_url"].endswith("/healthz")
            assert isinstance(mcp_block["pid"], int) and mcp_block["pid"] > 0

            # API block shape.
            api_block = sub["api"]
            assert api_block["http_url"].startswith("http://127.0.0.1:")
            assert api_block["healthz_url"].endswith("/healthz")
            assert api_block["docs_url"].endswith("/docs")
            assert isinstance(api_block["pid"], int) and api_block["pid"] > 0

            # UI block shape: ``healthz_url`` deliberately None (Vite).
            ui_block = sub["ui"]
            assert ui_block["http_url"].startswith("http://127.0.0.1:")
            assert ui_block["healthz_url"] is None
            assert isinstance(ui_block["pid"], int) and ui_block["pid"] > 0

        # Context manager SIGTERMed the supervisor; the discovery file
        # MUST be gone (graceful-shutdown contract).
        # Allow a brief moment for the shutdown's ``finally`` to land.
        deadline = time.monotonic() + 10.0
        while active_mcp.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not active_mcp.exists(), (
            "active-mcp.json was not removed on graceful shutdown.\n"
            f"--- stderr ---\n{drain.snapshot()}"
        )


def test_doctor_reports_active_mcp_contents() -> None:
    """``ctxr-fsm doctor --json`` surfaces the discovery doc verbatim.

    Operators reach for ``doctor`` when a skill says "MCP unreachable";
    the discovery doc is what the skill bootstrap would have parsed,
    so doctor showing the same bytes is the diagnostic shortcut.
    """
    with tempfile.TemporaryDirectory(prefix="ctxr-fsm-amcp-doc-") as tmpdir:
        project_root = _stage_project_root(Path(tmpdir))
        db_path = project_root / "fsm.db"
        active_mcp = project_root / _ACTIVE_MCP_FILE_REL

        with _spawn_supervisor(
            project_root=project_root, db_path=db_path, label="serve"
        ) as (_proc, drain):
            booted = drain.wait_for(_BOOT_BANNER_PREFIX, timeout=_BOOT_TIMEOUT_S)
            assert booted, drain.snapshot()
            assert _wait_for_file(active_mcp, timeout=10.0)

            # Doctor against the same project root.
            res = subprocess.run(
                [
                    "uv",
                    "run",
                    "ctxr-fsm",
                    "doctor",
                    "--json",
                    "--db",
                    str(db_path),
                ],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert res.returncode == 0, (
                f"doctor exited {res.returncode}\nstdout: {res.stdout}\n"
                f"stderr: {res.stderr}"
            )
            report = json.loads(res.stdout)
            assert "supervisor" in report
            sup = report["supervisor"]
            assert "active_mcp" in sup
            doc = sup["active_mcp"]
            assert doc is not None, (
                f"doctor returned None for active_mcp; expected the "
                f"discovery doc.\nreport: {report}"
            )
            assert "subsystems" in doc
            # The doctor reads the file at call time, so its view of
            # the subsystem set must match the on-disk file.
            disk_payload = json.loads(active_mcp.read_text(encoding="utf-8"))
            assert doc["subsystems"].keys() == disk_payload["subsystems"].keys()


if __name__ == "__main__":  # pragma: no cover - manual debug path
    raise SystemExit(pytest.main([__file__, "-v"]))
