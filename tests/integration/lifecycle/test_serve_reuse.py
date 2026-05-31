"""End-to-end singleton-reuse coverage for ``ctxr-fsm serve``.

The W7 supervisor's most important operator-facing invariant is
"two ``ctxr-fsm serve`` invocations in the same checkout never fight
over the same ports." The unit suite under ``tests/unit/lifecycle/``
proves :func:`ctxr.fsm.cli.lifecycle.primitives.acquire_singleton`
gets the reuse/replace decision right in isolation; this module
proves the supervisor actually drives that primitive correctly when
it boots a real process tree.

What this test does
-------------------

1. Stages a throwaway project root under :class:`tempfile.TemporaryDirectory`
   that satisfies every directory the supervisor reaches for:

   * ``ctxr/fsm/`` — the watchfiles target. We create it empty so the
     reload loop attaches without crashing; a missing watch root would
     log a warning ("watch root missing; reload loop disabled") and
     the test would still pass, but staging an empty dir keeps the
     supervisor on its happy path.
   * ``ui/package.json`` — the Vite dev server's working directory.
     We can't ship a real Vite app in a throwaway tree, so the stub
     ``dev`` script just sleeps forever. The supervisor inherits
     stdio for the UI child (Vite's ANSI banner contract), so the
     stub's silence is invisible to the test.

2. Spawns ``uv run ctxr-fsm serve --mode dev --db <tmp>`` with that
   throwaway directory as ``cwd``. The supervisor resolves
   ``project_root = Path.cwd().resolve()`` so ``cwd`` is the only
   knob we need to redirect every pid file and ports cache into the
   tmpdir.

3. Drains stderr in a background thread into a shared buffer + an
   :class:`threading.Event` that fires the moment the boot banner
   appears. The test body blocks on that event (with a generous
   timeout) so we have a deterministic "supervisor is up" signal
   without polling the pid file.

4. Reads ``.ctxr-fsm/pids/api.pid``, confirms the recorded pid is
   alive (the ``api`` child process the supervisor spawned), then
   spawns a *second* supervisor against the same project root. The
   second supervisor must:

   * log ``api already running (pid=<X>, url=<probe>); skipping spawn.``
     on its own stderr (the canonical "reuse" line emitted by
     :func:`ctxr.fsm.cli.lifecycle.supervisor._boot_subsystem`).
   * leave the on-disk ``api.pid`` payload byte-for-byte unchanged
     (the singleton primitive promises the existing record is the
     authoritative one and reuse must NOT rewrite it with the
     reusing process's pid).

5. SIGTERMs both supervisors and waits for clean exit so the next
   test in the suite (or the developer's editor) doesn't inherit a
   hanging child.

Why ``uv run`` instead of importing the module
----------------------------------------------

The MCP integration suite already established the convention: spawn
the operator-facing command, not an in-process facsimile. The whole
point of this test is the cross-process singleton contract — a Python
import of the supervisor would share the parent's pid and trivially
"reuse" itself, missing the file-on-disk handshake we actually need
to cover.

Why we drain stderr eagerly
---------------------------

Python's :class:`subprocess.Popen` will block a child once its
stderr pipe buffer fills (typically ~64 KiB). The supervisor's
verbose-by-default ``dev`` mode bridges every child's stdio through
its own stderr, so a passive test that only reads after the banner
would deadlock the first time ``uv run`` finishes resolving the lock
file (which is itself logged). The :func:`_StderrDrain` helper runs
in a daemon thread so the pipe is continuously emptied for the
lifetime of the subprocess.
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
# Tunables
# ---------------------------------------------------------------------------

# Maximum wall-clock seconds to wait for the boot banner. ``uv run``
# is cold here (the test process is not the project's editor venv) so
# the budget has to absorb the uv lock check + the Python interpreter
# start + every import in the FastMCP / FastAPI / SQLAlchemy graph.
# 90s is generous on a slow CI runner while still failing within a
# coffee break if the supervisor is genuinely hung.
_BOOT_TIMEOUT_S: float = 90.0

# Maximum wall-clock seconds to wait for the reuse log line from the
# second supervisor. Once the first is up the second's path is much
# shorter (it short-circuits at the singleton probe, before any HTTP
# bind), but we keep a similar budget for ``uv run`` cold-start.
_REUSE_TIMEOUT_S: float = 90.0

# Maximum wall-clock seconds to wait for each supervisor to exit after
# we send SIGTERM during teardown. The supervisor's own drain budget
# is 5s per child + 5s grace, so 30s covers both with slack.
_SHUTDOWN_TIMEOUT_S: float = 30.0

# Literal banner prefix the supervisor writes on a successful boot.
# Centralised so a rename of the prefix has one place to update.
_BOOT_BANNER_PREFIX: str = "[ctxr-fsm supervisor] booted:"

# Literal log fragment emitted by ``_boot_subsystem`` when an existing
# healthy singleton is found. We grep for the substring rather than
# the full line so a future addition of extra metadata (e.g. an
# ``acquired_at`` field) does not break the assertion.
_REUSE_LOG_FRAGMENT_TEMPLATE: str = "api already running (pid={pid},"


# ---------------------------------------------------------------------------
# Stub project layout
# ---------------------------------------------------------------------------


# Minimal ``package.json`` planted under ``<tmpdir>/ui/`` so the Vite
# child can ``cd`` into a real directory and ``npm run dev`` resolves
# a script. The script body is a Python sleep so the child stays
# alive without binding any port, network, or filesystem the test
# would have to clean up. ``--port 0`` passed by the supervisor lands
# as a positional after ``python3`` and is harmlessly ignored.
_STUB_UI_PACKAGE_JSON: str = json.dumps(
    {
        "name": "ctxr-fsm-test-stub-ui",
        "private": True,
        "version": "0.0.0",
        "scripts": {
            # A long sleep keeps the child running until the
            # supervisor's SIGTERM reaches it. Sleeping ~1h is more
            # than long enough for the test body to assert + tear down.
            "dev": "python3 -c \"import sys, time; sys.stdout.flush(); time.sleep(3600)\"",
        },
    },
    indent=2,
)


def _stage_project_root(tmpdir: Path) -> Path:
    """Build the minimal directory tree the supervisor's boot path walks.

    The supervisor reaches for:

    * ``<root>/.ctxr-fsm/`` — created lazily by the primitives; we
      pre-create it so the test can assert on its contents without
      having to wait for the first write.
    * ``<root>/ctxr/fsm/`` — the watchfiles target in dev mode.
    * ``<root>/ui/package.json`` — the Vite child's working dir +
      its ``npm run dev`` entrypoint.

    Returns the project root path so the caller can hand it straight
    to :class:`subprocess.Popen` as ``cwd``.
    """
    (tmpdir / ".ctxr-fsm" / "pids").mkdir(parents=True, exist_ok=True)
    (tmpdir / "ctxr" / "fsm").mkdir(parents=True, exist_ok=True)
    (tmpdir / "ui").mkdir(parents=True, exist_ok=True)
    (tmpdir / "ui" / "package.json").write_text(_STUB_UI_PACKAGE_JSON, encoding="utf-8")
    return tmpdir


# ---------------------------------------------------------------------------
# Stderr drainer
# ---------------------------------------------------------------------------


class _StderrDrain:
    """Background pump that copies a subprocess's stderr into a buffer.

    Why a dedicated class rather than :func:`Popen.communicate`?
    ``communicate`` is one-shot — it blocks until the child exits.
    The test body needs to *peek* at stderr to wait for specific log
    lines while the child stays alive, so we need a streaming reader.
    Wrapping it in a class also gives us:

    * ``wait_for(substring, timeout)`` — block until ``substring``
      appears anywhere in the accumulated buffer, with a wall-clock
      deadline that surfaces a useful error message on miss.
    * ``snapshot()`` — return the full buffer for assertion error
      messages so a failing test prints the supervisor's actual log
      output instead of an opaque "did not find X" sentence.

    The drainer thread is a daemon so a botched test (one that crashes
    before calling :meth:`stop`) cannot leak a thread into pytest's
    process; the underlying pipe is closed on subprocess exit, which
    breaks the thread's ``readline`` loop naturally.
    """

    def __init__(self, stream: object, label: str) -> None:
        # ``stream`` is intentionally typed as ``object`` because
        # :class:`subprocess.Popen.stderr` is ``IO[bytes] | None`` and
        # the narrower type would force a cast at every call site for
        # the type-checker without buying real safety.
        self._stream = stream
        self._label = label
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._event = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True, name=f"drain[{label}]")

    def start(self) -> None:
        """Spawn the daemon reader thread."""
        self._thread.start()

    def _pump(self) -> None:
        """Read one line at a time and append it to the shared buffer.

        ``readline`` blocks until either a newline arrives or the
        underlying pipe is closed (which happens when the subprocess
        exits). We swallow every exception inside the loop because the
        drainer's only job is to keep the pipe empty — propagating an
        IO error to the daemon thread's default uncaught-exception
        handler would print noise to the test runner without changing
        the test outcome.
        """
        stream = self._stream
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                try:
                    line = raw.decode("utf-8", errors="replace")
                except Exception:  # pragma: no cover - decode is defensive
                    line = repr(raw)
                with self._lock:
                    self._buffer.append(line)
                self._event.set()
        except (ValueError, OSError):
            # ValueError: I/O operation on closed file (we closed it).
            # OSError: pipe closed mid-read. Both are normal teardown.
            return

    def wait_for(self, substring: str, timeout: float) -> bool:
        """Block until ``substring`` appears in the buffer or ``timeout`` elapses.

        Returns ``True`` on hit, ``False`` on timeout. We loop on the
        :class:`threading.Event` (which the pump sets after every
        ``readline``) so the wait wakes promptly on each new line
        without busy-spinning at the test layer.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                joined = "".join(self._buffer)
            if substring in joined:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # ``Event.wait`` returns True if the event fires, False on
            # timeout. We clear it before each wait so we re-arm for
            # the *next* line rather than spin on the prior set.
            self._event.clear()
            self._event.wait(timeout=min(remaining, 0.5))

    def snapshot(self) -> str:
        """Return the entire buffer joined into a single string."""
        with self._lock:
            return "".join(self._buffer)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _supervisor_argv(db_path: Path) -> list[str]:
    """Build the ``uv run ctxr-fsm serve --mode dev --db <db>`` command.

    Centralised so both the first and the second supervisor share the
    exact same argv — the test's "reuse" assertion depends on the
    second invocation reaching the same singleton slot, which the
    primitives key off ``name`` (always ``"api"``) plus
    ``project_root`` (the supervisor's ``cwd``).
    """
    return [
        "uv",
        "run",
        "ctxr-fsm",
        "serve",
        "--mode",
        "dev",
        "--db",
        str(db_path),
    ]


@contextmanager
def _spawn_supervisor(
    *, project_root: Path, db_path: Path, label: str
) -> Iterator[tuple[subprocess.Popen[bytes], _StderrDrain]]:
    """Yield a ``(Popen, drainer)`` pair scoped to a single supervisor invocation.

    The context manager guarantees the child is signalled and reaped
    on every exit path (assertion failure, exception, normal return)
    so a flaky test never leaves a supervisor process running in the
    user's tmpdir holding a port.

    ``stderr`` is captured via :data:`subprocess.PIPE` and drained on
    a daemon thread (see :class:`_StderrDrain`) so the supervisor's
    chatty ``dev`` mode never deadlocks on a full pipe.

    ``stdout`` is also captured — the supervisor itself logs only to
    stderr, but the API and MCP children may write banner lines to
    their stdout that the supervisor's stdio bridge picks up; capturing
    both keeps the pipe drained.

    We deliberately do NOT pass ``start_new_session=True``. The
    supervisor's SIGTERM handler is what coordinates the drain; if we
    detached it into its own session group, a SIGTERM to the leader
    would not reach the API + MCP grandchildren and they'd survive as
    orphans even after the supervisor exited.
    """
    # ``env`` is forwarded verbatim so ``uv`` can find its config /
    # Python interpreter through whatever the dev environment uses
    # (PATH, UV_CACHE_DIR, ...). We do NOT inject ``CTXR_FSM_DB`` —
    # the explicit ``--db`` flag is the supported precedence path the
    # supervisor advertises in its docstring.
    env = os.environ.copy()

    proc = subprocess.Popen(
        _supervisor_argv(db_path),
        cwd=str(project_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    stderr_drain = _StderrDrain(proc.stderr, label=f"{label}.stderr")
    stderr_drain.start()
    # Also drain stdout — see docstring. We don't expose the stdout
    # buffer because the assertions key on stderr; the drainer just
    # keeps the pipe from filling.
    stdout_drain = _StderrDrain(proc.stdout, label=f"{label}.stdout")
    stdout_drain.start()

    try:
        yield proc, stderr_drain
    finally:
        # Best-effort drain. SIGTERM first so the supervisor runs its
        # own shutdown path (it owns the API + MCP + UI children, and
        # only its signal handler knows how to drain them gracefully).
        if proc.poll() is None:
            # Race: process exited between poll() and send_signal;
            # suppress ``ProcessLookupError`` in that window.
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # Last-resort kill so a stuck supervisor never strands the
            # next test or wedges CI. The drainers will see the pipes
            # close and exit on their own.
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):  # pragma: no cover - defensive
                proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)


def _read_api_pid_payload(project_root: Path) -> dict[str, object]:
    """Load and validate ``<project_root>/.ctxr-fsm/pids/api.pid``.

    The payload shape is fixed by
    :class:`ctxr.fsm.cli.lifecycle.primitives.PidLock`'s
    :func:`dataclasses.asdict` round-trip: ``{name, pid, probe_url,
    acquired_at}``. We assert on the shape minimally (it's an integer
    pid keyed under ``"pid"``) so an unrelated schema change in the
    primitives surfaces here as a focused failure rather than a
    cryptic ``KeyError``.
    """
    path = project_root / ".ctxr-fsm" / "pids" / "api.pid"
    assert path.exists(), f"expected api.pid at {path}, but it is missing"
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert isinstance(payload, dict), (
        f"api.pid payload must be a JSON object, got {type(payload).__name__}: {raw!r}"
    )
    pid_value = payload.get("pid")
    assert isinstance(pid_value, int) and pid_value > 0, (
        f"api.pid must record a positive integer pid, got {pid_value!r} "
        f"(full payload: {payload!r})"
    )
    return payload


def _pid_is_alive(pid: int) -> bool:
    """Local copy of the ``kill(pid, 0)`` liveness probe.

    We duplicate the helper rather than importing
    :func:`ctxr.fsm.cli.lifecycle.primitives.pid_is_alive` so the test
    is independent of the very module under test — a regression that
    breaks the primitive's liveness check would otherwise mask itself
    by also breaking the test's "is the api child up?" assertion.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it; still "alive" for
        # our purposes (same semantics as the primitive).
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_second_serve_reuses_existing_api_singleton() -> None:
    """A second ``ctxr-fsm serve`` adopts the first's API child instead of respawning.

    Walks the full W7 reuse contract end-to-end:

    1. Stage a throwaway project root that satisfies every dir the
       supervisor reaches for.
    2. Boot supervisor #1, wait for the ``booted:`` banner, capture the
       pid recorded under ``.ctxr-fsm/pids/api.pid``.
    3. Confirm that pid is alive (the supervisor's API child).
    4. Boot supervisor #2 with the same ``cwd`` and assert it logs the
       ``api already running (pid=<X>, ...)`` line on its own stderr.
    5. Assert the on-disk ``api.pid`` payload is unchanged (no rewrite
       by the reusing process; the singleton primitive guarantees
       the original record is authoritative).
    6. SIGTERM both supervisors and let the context managers reap them.

    The assertions on the actual log fragment + pid-file byte-identity
    are mutually reinforcing: even if the log line were forged
    somehow, an unchanged pid file proves the second supervisor never
    spawned its own ``uvicorn`` (which would have overwritten the
    file with a different pid).
    """
    with tempfile.TemporaryDirectory(prefix="ctxr-fsm-serve-reuse-") as tmpdir:
        project_root = _stage_project_root(Path(tmpdir))
        db_path = project_root / "fsm.db"

        # ---- Supervisor #1 -------------------------------------------------
        with _spawn_supervisor(
            project_root=project_root, db_path=db_path, label="serve1"
        ) as (proc1, drain1):
            booted = drain1.wait_for(_BOOT_BANNER_PREFIX, timeout=_BOOT_TIMEOUT_S)
            assert booted, (
                f"supervisor #1 did not emit boot banner "
                f"({_BOOT_BANNER_PREFIX!r}) within {_BOOT_TIMEOUT_S:.0f}s.\n"
                f"--- stderr so far ---\n{drain1.snapshot()}"
            )
            # Crash-detect: if the supervisor printed the banner but
            # then immediately died, we want to fail with a useful
            # message rather than racing the second invocation against
            # a corpse. ``poll()`` is non-blocking.
            assert proc1.poll() is None, (
                f"supervisor #1 exited unexpectedly after boot "
                f"(returncode={proc1.returncode}).\n"
                f"--- stderr ---\n{drain1.snapshot()}"
            )

            payload_before = _read_api_pid_payload(project_root)
            api_pid_before = int(payload_before["pid"])  # type: ignore[arg-type]
            assert _pid_is_alive(api_pid_before), (
                f"api.pid records pid {api_pid_before} but the process is not alive "
                f"(full payload: {payload_before!r}).\n"
                f"--- supervisor #1 stderr ---\n{drain1.snapshot()}"
            )

            # ---- Supervisor #2 ---------------------------------------------
            # Same project_root, same db. Must observe the singleton
            # from #1 and skip its own spawn for the api subsystem.
            with _spawn_supervisor(
                project_root=project_root, db_path=db_path, label="serve2"
            ) as (proc2, drain2):
                reuse_fragment = _REUSE_LOG_FRAGMENT_TEMPLATE.format(pid=api_pid_before)
                reused = drain2.wait_for(reuse_fragment, timeout=_REUSE_TIMEOUT_S)
                assert reused, (
                    f"supervisor #2 did not log the api-reuse line "
                    f"({reuse_fragment!r}) within {_REUSE_TIMEOUT_S:.0f}s.\n"
                    f"--- supervisor #2 stderr ---\n{drain2.snapshot()}\n"
                    f"--- supervisor #1 stderr ---\n{drain1.snapshot()}"
                )

                # The reuse path MUST NOT rewrite the pid file. We
                # compare the full payload dict (not just the pid) so
                # a regression that silently updates ``acquired_at``
                # or ``probe_url`` also fails this assertion.
                payload_after = _read_api_pid_payload(project_root)
                assert payload_after == payload_before, (
                    "api.pid was rewritten by the second supervisor — reuse must "
                    f"leave the existing record intact.\n"
                    f"before: {payload_before!r}\n"
                    f"after:  {payload_after!r}\n"
                    f"--- supervisor #2 stderr ---\n{drain2.snapshot()}"
                )

                # The first supervisor's api child must still be the
                # process named in the pid file — i.e. the second
                # supervisor did NOT race in, kill it, and respawn.
                assert _pid_is_alive(api_pid_before), (
                    f"api child pid {api_pid_before} stopped being alive while "
                    f"supervisor #2 was running — the reuse contract requires "
                    f"the existing api child to keep serving.\n"
                    f"--- supervisor #2 stderr ---\n{drain2.snapshot()}"
                )

            # Supervisor #2 has been SIGTERM'd by the context manager.
            # Confirm it actually exited (the inner ``finally`` waits
            # up to ``_SHUTDOWN_TIMEOUT_S``; a still-running process
            # here means the kill fallback fired and we want to know).
            assert proc2.returncode is not None, (
                "supervisor #2 did not exit after SIGTERM + kill — likely a "
                "hang in the supervisor's shutdown path.\n"
                f"--- supervisor #2 stderr ---\n{drain2.snapshot()}"
            )

        # Supervisor #1 also reaped by its own context manager.
        assert proc1.returncode is not None, (
            "supervisor #1 did not exit after SIGTERM + kill — likely a hang "
            "in the supervisor's shutdown path.\n"
            f"--- supervisor #1 stderr ---\n{drain1.snapshot()}"
        )


if __name__ == "__main__":  # pragma: no cover - manual debug path
    # Allow ``python tests/integration/lifecycle/test_serve_reuse.py``
    # to drive the suite under pytest from the command line, mirroring
    # the convention every other integration test in this repo uses.
    raise SystemExit(pytest.main([__file__, "-v"]))
