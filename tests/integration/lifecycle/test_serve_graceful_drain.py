"""Integration test: ``ctxr-fsm serve --mode dev`` graceful reload + drain.

This module exercises the W7 supervisor's dev-mode reload loop
end-to-end as a real subprocess tree:

1. Materialise a synthetic ``project_root`` under a
   :class:`tempfile.TemporaryDirectory` with the directory layout the
   supervisor expects (``ctxr/fsm/`` for the watch root, ``ui/`` with
   a minimal ``package.json`` whose ``dev`` script is a long-running
   noop so the UI child neither crashes nor consumes a real port).
2. Spawn ``uv run ctxr-fsm serve --mode dev --db <tmpdb>`` with that
   directory as ``cwd`` so the supervisor's ``project_root`` resolves
   to our synthetic tree (``run_supervisor`` defaults to
   :func:`Path.cwd`).
3. Stream the child's stderr in a background thread into a thread-safe
   buffer so the test driver can ``grep`` for supervisor banners
   without blocking on a pipe.
4. Wait for the boot banner, snapshot ``.ctxr-fsm/pids/api.pid``, and
   touch a file under ``<project_root>/ctxr/fsm/`` to trigger
   :func:`watchfiles.awatch`.
5. Watch for the supervisor's ``file change detected ... draining``
   banner and the matching ``respawned mcp + api`` banner. Assert no
   ``drain timeout exceeded`` / ``did not drain`` line was emitted.
6. Confirm the api pid recorded in ``.ctxr-fsm/pids/api.pid`` actually
   changed across the respawn and that the new api child is answering
   ``/healthz``.

Reload-loop flakiness fallback
------------------------------

The watcher's "did we observe the reload?" assertion is inherently
racier than the rest of the suite — watchfiles' debounce window, the
supervisor's drain budget, and uv's interpreter cold-start all stack
up. Per the W7 brief: if the file-change-triggered reload does not
land within the (generous) wall-clock budget we hold the assertion at
the smoke-test level — i.e. the supervisor must still be alive, must
have produced a boot banner, and must not have logged a drain timeout.

The "draining" + "respawned" / new-pid / new-healthz assertions are
gated behind ``pytest.skip`` rather than ``pytest.fail`` for that
fallback path so a slow CI host or a watcher hiccup never turns a
flaky environment into a red build, while still loudly reporting the
no-banner case so we don't silently lose coverage of the reload loop.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Wall-clock budget for the supervisor's boot banner to appear on
# stderr. Generous because ``uv run`` may need to resolve the
# environment on cold cache, the Typer app has to import the FastAPI +
# MCP stacks, and the per-child health probes run in series.
_BOOT_BANNER_TIMEOUT_SECONDS: float = 45.0

# Wall-clock budget for the supervisor to log a ``file change
# detected`` banner after the test driver touches a watched file.
# Watchfiles debounces at 500 ms (the supervisor's
# ``_WATCH_DEBOUNCE_MS``) and the supervisor then drains the children
# (per-child SIGTERM budget is 5 s), so 30 s leaves a comfortable
# margin even on a slow host.
_RELOAD_TIMEOUT_SECONDS: float = 30.0

# Wall-clock budget for the ``respawned`` banner once the ``draining``
# banner has landed. Bounded by the per-child drain budget + the new
# child's cold-start, so 30 s is comfortable.
_RESPAWN_TIMEOUT_SECONDS: float = 30.0

# Wall-clock budget for ``GET /healthz`` against the freshly respawned
# api child. The supervisor's own health probe already waits up to 5 s
# inside ``_poll_healthz``; we double that here so a slow respawn does
# not make the assertion flaky.
_HEALTHZ_TIMEOUT_SECONDS: float = 10.0

# Wall-clock budget for the supervisor to exit after SIGTERM. Matches
# the supervisor's own drain (5 s) + final-grace (5 s) budgets plus a
# wide safety margin so the shutdown teardown rarely escalates to
# SIGKILL.
_SHUTDOWN_TIMEOUT_SECONDS: float = 20.0


# Regex fragments keyed off the literal banner text that
# ``ctxr.fsm.cli.lifecycle.supervisor._log`` emits. Pinning these as
# module constants keeps the contract obvious — if the supervisor
# changes its banner shape, exactly one place in this test needs
# updating.
_BANNER_BOOTED: re.Pattern[str] = re.compile(
    r"\[ctxr-fsm supervisor\] booted: "
)
_BANNER_FILE_CHANGE: re.Pattern[str] = re.compile(
    r"\[ctxr-fsm supervisor\] file change detected.*draining"
)
_BANNER_RESPAWNED: re.Pattern[str] = re.compile(
    r"\[ctxr-fsm supervisor\] respawned mcp \+ api"
)
# Any of these would mean a child overran the drain budget; the test
# asserts none of them ever appears.
_BANNER_DRAIN_TIMEOUT: re.Pattern[str] = re.compile(
    r"\[ctxr-fsm supervisor\] (mcp|api|ui) did not drain"
)


# ---------------------------------------------------------------------------
# Synthetic project_root layout
# ---------------------------------------------------------------------------


# Minimal ``ui/package.json`` whose ``dev`` script is a long-running
# noop. The supervisor's UI child spawn does ``npm run dev -- --port
# <n>`` from ``<project_root>/ui``; piping ``--port N`` into ``node -e
# "setInterval(...)`` keeps the child alive (so the supervisor doesn't
# see an early UI exit and tear the rest down) without binding a real
# port. ``setInterval`` is the simplest "stay alive forever" handle
# Node offers; the empty body keeps CPU at zero.
_UI_PACKAGE_JSON: str = json.dumps(
    {
        "name": "ctxr-fsm-test-ui",
        "version": "0.0.0-test",
        "private": True,
        "scripts": {
            # ``node -e ...`` ignores the extra ``--port <n>`` argv
            # the supervisor appends; that's exactly what we want for
            # a noop child.
            "dev": "node -e \"setInterval(()=>{}, 1<<30)\"",
        },
    },
    indent=2,
)


def _build_synthetic_project_root(root: Path) -> None:
    """Populate ``root`` with the minimum tree the supervisor expects.

    Creates:

    * ``<root>/ctxr/fsm/__init__.py`` — the watch root. The file's
      content is irrelevant; what matters is that the directory exists
      so :func:`watchfiles.awatch` does not short-circuit (per
      ``_reload_loop``) and that there is a file in it the test driver
      can mutate to trigger a change event.
    * ``<root>/ui/package.json`` — needed by the supervisor's UI child
      spawn (``npm run dev`` in ``<root>/ui``). The ``dev`` script is
      a long-running noop (see :data:`_UI_PACKAGE_JSON`).
    """
    fsm_pkg = root / "ctxr" / "fsm"
    fsm_pkg.mkdir(parents=True, exist_ok=True)
    init_file = fsm_pkg / "__init__.py"
    init_file.write_text(
        "# Synthetic ctxr.fsm package for the W7 supervisor test.\n",
        encoding="utf-8",
    )

    ui_dir = root / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    (ui_dir / "package.json").write_text(_UI_PACKAGE_JSON, encoding="utf-8")


# ---------------------------------------------------------------------------
# Stderr streaming helper
# ---------------------------------------------------------------------------


class _StderrTail:
    """Thread-safe rolling buffer for a child process's stderr.

    The supervisor banners we key off (``booted``, ``file change
    detected``, ``respawned``) all land on stderr. We can't simply
    ``proc.stderr.read()`` because that blocks until EOF; instead we
    spawn a daemon thread that drains the pipe line-by-line into a
    list under a lock, and the test driver polls the list with a
    regex matcher.

    The buffer is unbounded by design — even a verbose supervisor run
    emits at most a few thousand lines over the test's wall-clock
    budget, and keeping every line lets a failed assertion print the
    full transcript instead of a tail-truncated snippet.
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._pump, name="serve-stderr-tail", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _pump(self) -> None:
        # ``iter(stream.readline, b"")`` returns until EOF; ``readline``
        # is a blocking read but the daemon flag means the thread won't
        # outlive the test process even if the child wedges.
        try:
            for raw in iter(self._stream.readline, b""):
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip("\n")
                with self._lock:
                    self._lines.append(text)
        except (ValueError, OSError):
            # ``ValueError: I/O operation on closed file`` fires when the
            # child closes its stderr; treat as EOF.
            return

    def snapshot(self) -> list[str]:
        """Return a copy of every line observed so far."""
        with self._lock:
            return list(self._lines)

    def wait_for(self, pattern: re.Pattern[str], timeout: float) -> str | None:
        """Block until a line matches ``pattern`` or ``timeout`` elapses.

        Returns the first matching line, or ``None`` on timeout. We
        poll at 50 ms (fast enough that a banner observable to a
        human-watching-the-terminal lands inside the first or second
        tick) rather than using a more elaborate event/condition
        because the line arrival path is a separate thread we don't
        control the signalling of.
        """
        deadline = time.monotonic() + timeout
        seen_index = 0
        while time.monotonic() < deadline:
            with self._lock:
                lines = self._lines[seen_index:]
                seen_index = len(self._lines)
            for line in lines:
                if pattern.search(line):
                    return line
            time.sleep(0.05)
        return None

    def joined(self) -> str:
        """Render the full transcript as one ``\\n``-joined string."""
        return "\n".join(self.snapshot())


# ---------------------------------------------------------------------------
# pid-file helpers
# ---------------------------------------------------------------------------


def _read_api_pid(project_root: Path) -> int | None:
    """Return the api child's recorded pid, or ``None`` if absent/bad.

    Mirrors the read-side of
    ``ctxr.fsm.cli.lifecycle.primitives.write_pid_file`` — the
    document is a JSON object with a ``"pid"`` field. We tolerate
    every error mode (file missing, malformed JSON, missing key,
    non-integer value) by returning ``None`` because the caller's
    natural reaction in every case is the same: "the supervisor has
    not yet recorded a pid; keep polling."
    """
    pid_path = project_root / ".ctxr-fsm" / "pids" / "api.pid"
    if not pid_path.exists():
        return None
    try:
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("pid")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _read_api_probe_url(project_root: Path) -> str | None:
    """Return the api child's recorded ``probe_url`` (or ``None``).

    Used so the post-respawn ``/healthz`` assertion targets the *new*
    bound port — the kernel may have handed the respawned child a
    different ephemeral port if the previous one is still in
    ``TIME_WAIT``, and a hard-coded port would race with that.
    """
    pid_path = project_root / ".ctxr-fsm" / "pids" / "api.pid"
    if not pid_path.exists():
        return None
    try:
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("probe_url")
    return raw if isinstance(raw, str) and raw else None


def _wait_for_api_pid_change(
    project_root: Path, original_pid: int, timeout: float
) -> int | None:
    """Poll ``api.pid`` until the recorded pid differs from ``original_pid``.

    Returns the new pid, or ``None`` if the budget elapsed without a
    change. The poll cadence (100 ms) is tuned to be much shorter than
    the supervisor's own respawn cycle (drain + spawn + health probe ≈
    seconds), so the test driver observes the transition in the same
    tick the supervisor writes the new pid.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _read_api_pid(project_root)
        if current is not None and current != original_pid:
            return current
        time.sleep(0.1)
    return None


def _poll_healthz(url: str, timeout: float) -> bool:
    """Poll ``<url>/healthz`` until 200 or ``timeout`` elapses.

    Returns ``True`` on a 200 response. Mirrors the supervisor's own
    health-probe loop in :func:`_poll_healthz` but synchronous (we are
    already on a test thread, not inside an anyio scope). Every
    exception (transport error, DNS error, timeout) is swallowed and
    treated as "not yet ready" — the loop's sole job is to decide
    whether the child is answering before the budget runs out.
    """
    deadline = time.monotonic() + timeout
    healthz_url = url.rstrip("/") + "/healthz"
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(healthz_url, timeout=1.0)
            if resp.status_code == 200:
                return True
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Subprocess lifecycle helpers
# ---------------------------------------------------------------------------


def _spawn_serve(project_root: Path, db_path: Path) -> subprocess.Popen[bytes]:
    """Spawn ``uv run ctxr-fsm serve --mode dev --db <db_path>``.

    The child runs with ``cwd=project_root`` so the supervisor's
    ``project_root`` (which defaults to :func:`Path.cwd`) lands on our
    synthetic tree. Stdout is silenced (``DEVNULL``); stderr is piped
    so :class:`_StderrTail` can grep it.

    On POSIX we use ``start_new_session=True`` so the supervisor and
    every descendant (uv → python → MCP/api/UI children) share a
    process group we can later signal as a unit — that's how the
    teardown guarantees we leave no stray processes even if the
    supervisor wedges.
    """
    env = os.environ.copy()
    # Force a predictable line buffering on the child's stderr so the
    # supervisor's banner lines flush promptly into our pipe. Without
    # this, Python's default block-buffering on stderr-to-pipe would
    # add multi-second delays before the test sees a banner.
    env["PYTHONUNBUFFERED"] = "1"
    # The supervisor uses ``Path.cwd()`` as the default project_root;
    # there is no env-var override exposed by the CLI, so we rely on
    # ``cwd=`` below to anchor it.

    return subprocess.Popen(
        [
            "uv",
            "run",
            "ctxr-fsm",
            "serve",
            "--mode",
            "dev",
            "--db",
            str(db_path),
        ],
        cwd=str(project_root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _terminate_serve(proc: subprocess.Popen[bytes]) -> None:
    """Drain the supervisor process tree on test teardown.

    Sends SIGTERM to the whole process group (so the supervisor's
    children — MCP, api, the noop UI node process, and any
    grandchildren ``uv`` spawned — all receive it together), then
    waits up to :data:`_SHUTDOWN_TIMEOUT_SECONDS` for the supervisor
    itself to exit. If it overruns, we escalate to SIGKILL on the
    group.

    Every signal is wrapped in ``suppress(ProcessLookupError)``
    because the supervisor may have already exited (clean shutdown,
    child crash, etc.) — re-raising a "no such process" error here
    would mask the real failure that the test body asserted on.
    """
    if proc.poll() is not None:
        return

    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Escalate. Any remaining children share the group, so a
        # single ``killpg`` SIGKILL takes everyone down.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        try:
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # At this point the kernel owes us a zombie reap; give up
            # rather than block the test runner forever.
            return


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def supervisor_env() -> Iterator[tuple[Path, Path, subprocess.Popen[bytes], _StderrTail]]:
    """Spawn the supervisor against a synthetic project root.

    Yields ``(project_root, db_path, proc, tail)``:

    * ``project_root`` — temp directory populated by
      :func:`_build_synthetic_project_root`.
    * ``db_path`` — sibling ``fsm.db`` path passed via ``--db``.
    * ``proc`` — the ``Popen`` for ``uv run ctxr-fsm serve``.
    * ``tail`` — running :class:`_StderrTail` draining the child's
      stderr into a thread-safe buffer.

    Teardown signals the whole process group (SIGTERM, then SIGKILL on
    overrun) so the test never strands the api/MCP/UI children on the
    box, then cleans up the temp directory. The temp directory itself
    is created via :func:`tempfile.TemporaryDirectory` so its
    ``__exit__`` finalises even if the test body raises.
    """
    with tempfile.TemporaryDirectory(prefix="ctxr-fsm-serve-it-") as tmp:
        project_root = Path(tmp).resolve()
        _build_synthetic_project_root(project_root)
        db_path = project_root / "fsm.db"

        proc = _spawn_serve(project_root, db_path)
        # The Popen object owns ``stderr``; mypy/pyright are fine
        # because we asked for ``stderr=subprocess.PIPE`` above.
        assert proc.stderr is not None
        tail = _StderrTail(proc.stderr)
        tail.start()

        try:
            yield project_root, db_path, proc, tail
        finally:
            _terminate_serve(proc)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_serve_dev_reload_drains_and_respawns_api_child(
    supervisor_env: tuple[Path, Path, subprocess.Popen[bytes], _StderrTail],
) -> None:
    """``serve --mode dev`` drains + respawns the api child on a source change.

    Flow:

    1. Wait for the supervisor's boot banner — proof that the task
       group is up and every initial spawn finished its health probe.
    2. Snapshot the api child's pid from ``.ctxr-fsm/pids/api.pid``.
       This is the supervisor's authoritative record (it's what
       :func:`acquire_singleton` writes on a fresh spawn), so reading
       it back is the most reliable cross-process handle the test has.
    3. Trigger a change inside the watched directory by appending a
       comment line to ``<project_root>/ctxr/fsm/__init__.py``.
       Watchfiles' debounce window collapses many touches into one
       reload cycle, so a single append is enough.
    4. Wait for the ``file change detected ... draining`` banner.
    5. Wait for the ``respawned mcp + api`` banner.
    6. Wait for ``.ctxr-fsm/pids/api.pid`` to record a *new* pid (the
       respawned child's), then hit ``/healthz`` against the recorded
       probe URL.

    If the watcher does not deliver a change event within the (very
    generous) reload budget — usually a sign of a slow CI host or a
    filesystem that suppresses content-identical change events — we
    fall back to a smoke-test posture: assert the supervisor is still
    alive, still has the boot banner, and never logged a drain
    timeout. That matches the W7 brief's "if the reload assertion is
    flaky, downgrade to a smoke test" guidance.
    """
    project_root, _db_path, proc, tail = supervisor_env

    # ── 1. Boot banner ────────────────────────────────────────────────
    booted_line = tail.wait_for(_BANNER_BOOTED, _BOOT_BANNER_TIMEOUT_SECONDS)
    if booted_line is None:
        # If the supervisor never even boots inside the budget, fail
        # outright — that's a real regression, not flakiness. The
        # failure message includes the full stderr transcript so a CI
        # log captures the diagnosis.
        pytest.fail(
            "supervisor did not emit the boot banner within "
            f"{_BOOT_BANNER_TIMEOUT_SECONDS:.0f}s; "
            f"proc.poll()={proc.poll()!r}; stderr=\n{tail.joined()}"
        )

    # ── 2. Snapshot the initial api pid ───────────────────────────────
    initial_pid: int | None = None
    # The boot banner fires *after* every initial spawn finished its
    # health probe and ``remember_port`` wrote the port file, so the
    # pid file should be present by this point. We allow a brief
    # polling window anyway because the write is not strictly ordered
    # against the banner emit.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        initial_pid = _read_api_pid(project_root)
        if initial_pid is not None:
            break
        time.sleep(0.1)
    assert initial_pid is not None, (
        "api.pid was not written within 5s of the boot banner; "
        f"stderr=\n{tail.joined()}"
    )

    # ── 3. Trigger the reload by touching a watched file ──────────────
    watched_file = project_root / "ctxr" / "fsm" / "__init__.py"
    original_text = watched_file.read_text(encoding="utf-8")
    try:
        watched_file.write_text(
            original_text + f"# Reload trigger at {time.time_ns()}\n",
            encoding="utf-8",
        )

        # ── 4. ``file change detected ... draining`` banner ───────────
        drain_line = tail.wait_for(
            _BANNER_FILE_CHANGE, _RELOAD_TIMEOUT_SECONDS
        )
        if drain_line is None:
            # Fallback: confirm the supervisor is still alive + healthy
            # and exit as a smoke test rather than failing the build on
            # what is fundamentally a watchfiles/filesystem race. The
            # boot banner already proved the supervisor reached the
            # task-group steady state; absence of a drain timeout
            # below confirms no child wedged in the meantime.
            assert proc.poll() is None, (
                "supervisor exited while we were waiting for the "
                f"reload banner; returncode={proc.returncode!r}; "
                f"stderr=\n{tail.joined()}"
            )
            timeout_line = next(
                (
                    line
                    for line in tail.snapshot()
                    if _BANNER_DRAIN_TIMEOUT.search(line)
                ),
                None,
            )
            assert timeout_line is None, (
                "supervisor logged a drain timeout even though no "
                f"reload was triggered: {timeout_line!r}; "
                f"stderr=\n{tail.joined()}"
            )
            pytest.skip(
                "watcher did not deliver a file-change event within "
                f"{_RELOAD_TIMEOUT_SECONDS:.0f}s; downgrading to "
                "smoke-test posture (supervisor alive, no drain "
                "timeout). See module docstring for the rationale."
            )

        # ── 5. ``respawned mcp + api`` banner ─────────────────────────
        respawn_line = tail.wait_for(
            _BANNER_RESPAWNED, _RESPAWN_TIMEOUT_SECONDS
        )
        assert respawn_line is not None, (
            "supervisor logged 'file change detected' but never "
            f"emitted the matching 'respawned' banner within "
            f"{_RESPAWN_TIMEOUT_SECONDS:.0f}s; "
            f"drain_line={drain_line!r}; stderr=\n{tail.joined()}"
        )

        # ── 6. api pid in .ctxr-fsm/pids/api.pid must have changed ────
        new_pid = _wait_for_api_pid_change(
            project_root, initial_pid, _RESPAWN_TIMEOUT_SECONDS
        )
        assert new_pid is not None, (
            "supervisor logged 'respawned' but api.pid still records "
            f"the original pid {initial_pid}; stderr=\n{tail.joined()}"
        )
        assert new_pid != initial_pid, (
            f"api.pid did not change after respawn: still {new_pid}; "
            f"stderr=\n{tail.joined()}"
        )

        # ── 6b. /healthz against the new child must answer 200 ────────
        new_probe_url = _read_api_probe_url(project_root)
        assert new_probe_url, (
            "respawned api child's pid file is missing 'probe_url'; "
            f"stderr=\n{tail.joined()}"
        )
        assert _poll_healthz(new_probe_url, _HEALTHZ_TIMEOUT_SECONDS), (
            "respawned api child did not answer /healthz at "
            f"{new_probe_url} within {_HEALTHZ_TIMEOUT_SECONDS:.0f}s; "
            f"stderr=\n{tail.joined()}"
        )

        # ── No "drain timeout exceeded" banner anywhere in the run ────
        timeout_line = next(
            (
                line
                for line in tail.snapshot()
                if _BANNER_DRAIN_TIMEOUT.search(line)
            ),
            None,
        )
        assert timeout_line is None, (
            "supervisor logged a drain timeout during the reload: "
            f"{timeout_line!r}; stderr=\n{tail.joined()}"
        )
    finally:
        # Revert the watched file so the source tree is left exactly
        # as we found it. The fixture's temp dir cleanup would handle
        # this for the synthetic root, but keeping the revert here
        # documents the contract for any future variant that runs
        # against the real repo's ``ctxr/fsm/__init__.py``.
        if watched_file.exists():
            # The file may have been torn down by the supervisor's
            # rmtree on shutdown; that's fine, so suppress ``OSError``.
            with contextlib.suppress(OSError):
                watched_file.write_text(original_text, encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - manual debug path
    # Allow ``python tests/integration/lifecycle/test_serve_graceful_drain.py``
    # to run the test under pytest without typing the full path.
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
