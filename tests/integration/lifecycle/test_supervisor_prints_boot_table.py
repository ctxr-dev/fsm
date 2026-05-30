"""End-to-end coverage for W14j: supervisor prints the boot table ONCE.

W14j wired the shared Rich subsystem table into ``run_supervisor`` so
an operator running ``ctxr-fsm serve`` sees the FastAPI / Swagger /
UI / MCP URLs immediately after every required healthz passes. The
contract is **exactly one** table per boot — NOT on every reload,
NOT on every healthz probe.

This test:

1. Spawns a real ``uv run ctxr-fsm serve`` against a stub project
   (mirrors the test_active_mcp_json.py pattern so the integration
   contract is exercised end-to-end).
2. Waits for the boot banner on stderr (the supervisor's own log
   line).
3. Captures stdout (where Rich prints the table).
4. Asserts the table headers appear exactly once.
5. SIGTERMs the supervisor; asserts no second table was printed
   between boot and shutdown.

We pipe the supervisor's stdout (not stderr) because
:func:`print_subsystem_table` writes to a fresh
:class:`rich.console.Console` which targets stdout by default. The
``_log`` helper used by the supervisor's banner writes to stderr —
two independent streams, which is what we want operators to be able
to pipe separately for downstream tooling.
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

# ---------------------------------------------------------------------------
# Tunables (mirror test_active_mcp_json.py — kept in sync deliberately)
# ---------------------------------------------------------------------------

_BOOT_TIMEOUT_S: float = 90.0
_SHUTDOWN_TIMEOUT_S: float = 30.0
_BOOT_BANNER_PREFIX: str = "[ctxr-fsm supervisor] booted:"

# After the banner lands the supervisor still has to (a) publish the
# discovery file and (b) call ``print_subsystem_table``. Give it
# plenty of slack on a cold-CI ``uv run``.
_TABLE_PRINT_WAIT_S: float = 10.0


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
    """Build the minimal tree the supervisor's boot path walks."""
    (tmpdir / ".ctxr-fsm" / "pids").mkdir(parents=True, exist_ok=True)
    (tmpdir / "ctxr" / "fsm").mkdir(parents=True, exist_ok=True)
    (tmpdir / "ui").mkdir(parents=True, exist_ok=True)
    (tmpdir / "ui" / "package.json").write_text(
        _STUB_UI_PACKAGE_JSON, encoding="utf-8"
    )
    return tmpdir


class _StreamDrain:
    """Background pump that copies a subprocess stream into a buffer.

    Slimmed clone of ``test_active_mcp_json._StderrDrain``; we keep a
    private copy so this integration file stays self-contained and
    its assertions only depend on its own fixtures.
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


def _supervisor_argv(db_path: Path, *, mode: str = "dev") -> list[str]:
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
) -> Iterator[tuple[subprocess.Popen[bytes], _StreamDrain, _StreamDrain]]:
    """Yield ``(proc, stderr_drain, stdout_drain)`` scoped to a single supervisor."""
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
    err = _StreamDrain(proc.stderr, label=f"{label}.stderr")
    out = _StreamDrain(proc.stdout, label=f"{label}.stdout")
    err.start()
    out.start()
    try:
        yield proc, err, out
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)


def _count_table_renderings(stdout_text: str) -> int:
    """Count how many subsystem-table renderings appear in ``stdout_text``.

    Rich draws the table title on its own line followed by the header
    row; the ``ctxr-fsm subsystems`` title is the most reliable
    once-per-table sentinel (the column headers also appear once per
    table but their tokens could in principle show up in other log
    surfaces).
    """
    return stdout_text.count("ctxr-fsm subsystems")


def test_supervisor_prints_boot_table_exactly_once() -> None:
    """``ctxr-fsm serve`` prints the W14j subsystem table once after boot.

    Walks the full lifecycle: boot → wait for banner → wait for table
    to land on stdout → confirm exactly one rendering → SIGTERM →
    re-check no second rendering arrived during the steady-state
    window.
    """
    with tempfile.TemporaryDirectory(prefix="ctxr-fsm-boot-table-") as tmpdir:
        project_root = _stage_project_root(Path(tmpdir))
        db_path = project_root / "fsm.db"

        with _spawn_supervisor(
            project_root=project_root, db_path=db_path, label="serve"
        ) as (proc, err, out):
            # Wait for the supervisor's own banner on stderr — that
            # tells us boot is done and the publish gate has been
            # evaluated.
            booted = err.wait_for(_BOOT_BANNER_PREFIX, timeout=_BOOT_TIMEOUT_S)
            assert booted, (
                f"supervisor never emitted {_BOOT_BANNER_PREFIX!r} within "
                f"{_BOOT_TIMEOUT_S:.0f}s.\n--- stderr ---\n{err.snapshot()}"
            )
            assert proc.poll() is None, (
                f"supervisor died after boot (rc={proc.returncode}).\n"
                f"--- stderr ---\n{err.snapshot()}"
            )

            # Wait for the table to land on stdout. Rich flushes per
            # ``console.print`` call so the title appears immediately.
            assert out.wait_for(
                "ctxr-fsm subsystems", timeout=_TABLE_PRINT_WAIT_S
            ), (
                f"subsystem table never printed within "
                f"{_TABLE_PRINT_WAIT_S:.0f}s of boot.\n"
                f"--- stdout ---\n{out.snapshot()}\n"
                f"--- stderr ---\n{err.snapshot()}"
            )

            # Exactly one table at this point.
            first_count = _count_table_renderings(out.snapshot())
            assert first_count == 1, (
                f"expected exactly 1 boot table, saw {first_count}.\n"
                f"--- stdout ---\n{out.snapshot()}"
            )

            # Give the supervisor a steady-state window. The reload
            # loop watches ``ctxr/fsm`` — we don't touch any source
            # files, so no reload should fire, so no second table
            # should appear.
            time.sleep(2.0)

            second_count = _count_table_renderings(out.snapshot())
            assert second_count == 1, (
                f"table re-printed during steady state "
                f"(count went {first_count}→{second_count}); "
                f"the boot-table contract is once-per-boot.\n"
                f"--- stdout ---\n{out.snapshot()}"
            )


def test_supervisor_boot_table_headers_visible_on_stdout() -> None:
    """The boot table on stdout carries the locked column headers + Project row.

    A focused header-presence check separated from the once-per-boot
    test so a regression that mangled column ordering surfaces in its
    own failure (rather than getting confused with the count check).
    """
    with tempfile.TemporaryDirectory(prefix="ctxr-fsm-boot-table-hdr-") as tmpdir:
        project_root = _stage_project_root(Path(tmpdir))
        db_path = project_root / "fsm.db"

        with _spawn_supervisor(
            project_root=project_root, db_path=db_path, label="serve-hdr"
        ) as (_proc, err, out):
            assert err.wait_for(_BOOT_BANNER_PREFIX, timeout=_BOOT_TIMEOUT_S), (
                f"supervisor never booted.\n--- stderr ---\n{err.snapshot()}"
            )
            # The Rich table prints in several pipe-buffered chunks
            # (title, then header row, then data rows, then footer).
            # We wait for the LAST data row's sentinel ("Project" is
            # the leading row but the table footer line lands after
            # every row has flushed) before snapshotting. Picking
            # "Project" + the trailing newline guarantees the whole
            # body reached our buffer.
            assert out.wait_for(
                "ctxr-fsm subsystems", timeout=_TABLE_PRINT_WAIT_S
            ), (
                f"table title never printed.\n--- stdout ---\n{out.snapshot()}"
            )
            assert out.wait_for("Project", timeout=_TABLE_PRINT_WAIT_S), (
                f"table body never printed.\n--- stdout ---\n{out.snapshot()}"
            )
            # The header row arrives between the title and the Project
            # row but rich may chunk pipes so we explicitly wait for
            # the rightmost column header too — once "PID" landed,
            # every header to its left has also landed by definition.
            assert out.wait_for("PID", timeout=_TABLE_PRINT_WAIT_S), (
                f"table header row never fully printed.\n"
                f"--- stdout ---\n{out.snapshot()}"
            )

            captured = out.snapshot()
            for header in ("Subsystem", "URL", "Swagger", "Health", "PID"):
                assert header in captured, (
                    f"missing header {header!r} in:\n{captured}"
                )
            assert "Project" in captured
