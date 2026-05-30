"""Low-level lifecycle primitives — ports, pid files, singleton locks.

The module is deliberately small and dependency-light: only the
standard library plus :mod:`httpx` (already a transitive dep of the
``api`` extras) for the singleton health probe. Higher-level wiring
(supervisor task group, signal handlers, watcher integration) lives in
the W7 supervisor module that imports from here.

Design notes:

* **Atomic writes.** ``ports.json``, ``pids/<name>.pid``, and
  ``active-run.json`` are all written via the ``tmp + rename`` idiom
  so a crash mid-write can never leave a half-written file that a
  later read would parse as truncated/invalid JSON. The temp file
  lives in the same directory as the target so :func:`os.replace`
  stays atomic on every supported filesystem.

* **Best-effort reads.** :func:`read_pid_file` returns ``None`` for
  both "file missing" and "file present but malformed" — the caller's
  natural reaction in either case is the same (treat the slot as
  free), so a richer error type would force needless branching.

* **No locking.** A pid file is a *hint*, not a mutex. Two processes
  racing for the same slot can both observe "no live owner" and both
  write their pid; the loser then notices on the next read that the
  file no longer belongs to them and exits. That's acceptable for a
  dev-machine subsystem; the only invariant we protect is "release
  never deletes someone else's lock".

* **Probe URL convention.** The probe URL is the *base* URL of the
  service (e.g. ``http://127.0.0.1:8765``); we append ``/healthz``
  inside :func:`acquire_singleton` so callers don't have to remember
  the suffix. A probe URL of ``None`` means "I have no HTTP endpoint
  — treat the pid being alive as sufficient proof of life", which the
  supervisor uses for non-HTTP subsystems like the watcher.
"""

from __future__ import annotations

import errno
import json
import os
import socket
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

__all__ = [
    "PidLock",
    "ReusedSubsystem",
    "acquire_singleton",
    "active_mcp_file_path",
    "now_iso_ms",
    "pick_port",
    "pid_file_for",
    "pid_is_alive",
    "read_active_mcp_file",
    "read_pid_file",
    "recall_port",
    "release_singleton",
    "remember_active_mcp_file",
    "remember_port",
    "remove_active_mcp_file",
    "write_active_run_marker",
    "write_pid_file",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# The single root subdirectory every lifecycle artifact lives under.
# Kept as a module constant so the test suite can monkeypatch one
# location if it ever needs to and so a future relocation (e.g. to
# ``$XDG_RUNTIME_DIR``) only touches one line.
_STATE_DIR_NAME: str = ".ctxr-fsm"

# File names for the three persistent JSON documents the lifecycle
# module owns. ``ports.json`` is a flat ``{name: port}`` map;
# ``pids/<name>.pid`` is one JSON document per subsystem;
# ``active-run.json`` is a single ``{run_id, set_at}`` document.
_PORTS_FILE_NAME: str = "ports.json"
_PIDS_DIR_NAME: str = "pids"
_ACTIVE_RUN_FILE_NAME: str = "active-run.json"
# W14c: discovery file the supervisor writes once its MCP child is
# answering /healthz. Skills + ``ctxr-fsm ensure`` read it to find the
# HTTP-SSE MCP URL when the stdio entry is not registered with the
# active client yet. Removed on graceful supervisor shutdown so a stale
# document never points a skill at a dead port.
_ACTIVE_MCP_FILE_NAME: str = "active-mcp.json"


def _state_dir(project_root: Path) -> Path:
    """Return ``<project_root>/.ctxr-fsm``, creating it if missing.

    The directory is created with default permissions (the parent
    project owns it) and ``parents=True`` so a fresh checkout that
    has never run any subsystem still gets a working tree.
    """
    target = project_root / _STATE_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def _pids_dir(project_root: Path) -> Path:
    """Return ``<project_root>/.ctxr-fsm/pids``, creating it if missing."""
    target = _state_dir(project_root) / _PIDS_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def pid_file_for(name: str, *, project_root: Path) -> Path:
    """Return the canonical pid-file path for subsystem ``name``.

    Public surface so callers (e.g. the supervisor's post-spawn rewrite
    that swaps the supervisor's own pid for the child's pid) can locate
    the same file :func:`acquire_singleton` would create, without
    reaching into the private ``_pids_dir`` helper or hand-rolling the
    ``.ctxr-fsm/pids/<name>.pid`` join themselves.
    """
    return _pids_dir(project_root) / f"{name}.pid"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via the tmp + rename idiom.

    The temp file is created next to ``path`` (same directory) so the
    final :func:`os.replace` stays atomic on POSIX and on Windows. A
    trailing newline is appended so the file ends cleanly when read
    by line-oriented tools (``cat``, ``less``, editors that warn on
    missing final newlines).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with offset.

    Centralised so every persisted timestamp uses the same format,
    which keeps ``ports.json`` / ``pids/*.pid`` / ``active-run.json``
    diff-friendly when a human inspects them.
    """
    return datetime.now(UTC).isoformat()


def now_iso_ms() -> str:
    """Return the current UTC time as ISO-8601 with millisecond precision.

    Project convention (per the plan): every timestamp the FSM
    substrate persists is ISO-8601 in UTC with millisecond precision so
    cross-row sort is total and the printed form fits in a fixed
    column. Python's :meth:`datetime.isoformat` defaults to microsecond
    precision when a microsecond component is non-zero, so we truncate
    explicitly to keep the surface stable.

    Returned shape: ``"2026-05-29T12:34:56.789+00:00"``.
    """
    now = datetime.now(UTC)
    # Truncate microseconds → milliseconds (Python's stdlib has no
    # nicer hook). We multiply-then-int to round-down rather than risk
    # banker's rounding on the boundary.
    truncated_us = (now.microsecond // 1000) * 1000
    return now.replace(microsecond=truncated_us).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Port allocation + memory
# ---------------------------------------------------------------------------


def pick_port(preferred: int | None = None) -> int:
    """Return a free TCP port on ``127.0.0.1``.

    If ``preferred`` is given we first try to bind to it with
    ``SO_REUSEADDR`` set (so a recently-released port doesn't trip on
    ``TIME_WAIT``). On ``EADDRINUSE`` we fall back to letting the
    kernel choose an ephemeral port via ``bind(('127.0.0.1', 0))``.
    Either way we close the probe socket before returning — the caller
    is expected to bind for real, and holding a socket open across the
    return value would race with whatever framework they're handing
    the port to.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if preferred is not None:
            try:
                sock.bind(("127.0.0.1", preferred))
                return preferred
            except OSError as exc:
                # ``EADDRINUSE`` is the only error we transparently
                # recover from — every other ``OSError`` (permission
                # denied on a privileged port, no IPv4 stack, ...)
                # surfaces unchanged so the caller can react to it
                # rather than silently get a random port.
                if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                    raise
                # Fall through to the ephemeral-port branch below.
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port
    finally:
        sock.close()


def remember_port(name: str, port: int, *, project_root: Path) -> None:
    """Persist ``{name: port}`` into ``<project_root>/.ctxr-fsm/ports.json``.

    The file is updated in place: an existing mapping for ``name`` is
    overwritten, every other key is preserved. The write is atomic
    (tmp + rename) so a concurrent reader either sees the old map or
    the new one, never a half-written document.
    """
    path = _state_dir(project_root) / _PORTS_FILE_NAME
    data: dict[str, int] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                # Coerce to ``{str: int}``; silently drop entries that
                # don't match (the file is developer-facing, not a
                # contract — a hand-edit that introduces a string
                # value shouldn't poison subsequent writes).
                data = {
                    str(k): int(v)
                    for k, v in loaded.items()
                    if isinstance(v, int) or (isinstance(v, str) and v.isdigit())
                }
        except (OSError, json.JSONDecodeError, ValueError):
            # Treat a malformed file as empty — the next write will
            # heal it. We never crash startup over a corrupt cache.
            data = {}
    data[name] = int(port)
    _atomic_write_text(path, json.dumps(data, sort_keys=True, indent=2))


def recall_port(name: str, *, project_root: Path) -> int | None:
    """Return the previously-remembered port for ``name``, or ``None``.

    A missing file, a malformed file, or an unknown ``name`` all yield
    ``None`` — the caller's natural reaction in every case is to fall
    back to :func:`pick_port`, so a finer-grained signal would just
    force boilerplate at every call site.
    """
    path = _state_dir(project_root) / _PORTS_FILE_NAME
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    raw = loaded.get(name)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


# ---------------------------------------------------------------------------
# Pid-file primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PidLock:
    """Proof that *we* currently own a singleton slot.

    Returned by :func:`acquire_singleton` when no live owner was
    found. The fields mirror the on-disk JSON document, so a caller
    that wants to inspect ``pids/<name>.pid`` can reconstruct what
    they expect to see without re-reading the file.

    Fields:

    * ``name`` — the singleton's logical name (matches the pid-file
      stem). Kept on the dataclass so :func:`release_singleton`
      doesn't need it passed separately.
    * ``pid`` — our own pid, captured at acquisition time so a
      release after a (theoretical) re-exec still targets the right
      record.
    * ``probe_url`` — the base URL we registered for health probing,
      or ``None`` if this subsystem doesn't expose HTTP.
    * ``acquired_at`` — ISO-8601 timestamp, useful for diagnostics
      when a stale lock is observed.
    """

    name: str
    pid: int
    probe_url: str | None
    acquired_at: str


@dataclass(frozen=True)
class ReusedSubsystem:
    """Proof that an existing, healthy owner already holds the slot.

    Returned by :func:`acquire_singleton` when the pid file pointed to
    a live process *and* the health probe succeeded. The caller's
    correct behaviour is to skip its own startup work and treat the
    existing instance as authoritative — typically by surfacing the
    ``probe_url`` to the user so they can keep using whatever URL
    they already had.

    The ``health_status`` field is the body of the ``/healthz``
    response (e.g. ``"ok"``); we keep it verbatim rather than collapsing
    to a bool so a future ``"degraded"`` status can be threaded through
    without changing the dataclass shape.
    """

    name: str
    existing_pid: int
    probe_url: str
    health_status: str


def write_pid_file(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a pid-file JSON document to ``path``.

    ``payload`` is serialised with sorted keys + two-space indent so
    diffs are stable. The write goes through :func:`_atomic_write_text`
    so a crash between create-and-rename can't leave a half-written
    file that a later reader would treat as malformed.
    """
    _atomic_write_text(path, json.dumps(payload, sort_keys=True, indent=2))


def read_pid_file(path: Path) -> dict[str, Any] | None:
    """Return the parsed pid-file document, or ``None`` if missing/bad.

    "Missing" and "malformed" collapse to the same return value
    because the caller's reaction in both cases is identical: treat
    the slot as free.
    """
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def pid_is_alive(pid: int) -> bool:
    """Return ``True`` iff ``pid`` names a live process we can signal.

    Uses the POSIX ``kill(pid, 0)`` trick: signal 0 performs all of
    the lookups + permission checks of a real signal delivery without
    actually sending one, so it's the cheapest available liveness
    probe. A ``ProcessLookupError`` (``ESRCH``) is the unambiguous
    "no such pid" signal; a ``PermissionError`` (``EPERM``) means the
    pid *does* exist (we'd otherwise have gotten ``ESRCH`` first), we
    just can't signal it — which still counts as "alive" for our
    purposes. Any other ``OSError`` is treated as "not alive" so a
    surprising failure mode never leaves us blocked forever.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _probe_healthz(probe_url: str, *, timeout: float = 1.0) -> str | None:
    """Return the body of ``GET <probe_url>/healthz`` on 200, else ``None``.

    Centralised so :func:`acquire_singleton` doesn't grow an inline
    ``try/except httpx.HTTPError`` block; also keeps the timeout in
    one place. A short timeout (1s) is correct because both probe and
    target are on ``127.0.0.1`` — anything slower than that is almost
    certainly a hung process we'd want to replace anyway.
    """
    # Tolerate callers that pass the URL with or without a trailing
    # slash; ``urljoin`` would also work but a manual ``rstrip`` keeps
    # the dependency surface visibly tiny.
    url = probe_url.rstrip("/") + "/healthz"
    try:
        resp = httpx.get(url, timeout=timeout)
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code != 200:
        return None
    # ``/healthz`` is conventionally tiny ("ok" or a small JSON blob);
    # we hand the raw text back so a future structured health payload
    # is observable without changing this layer.
    return resp.text


# ---------------------------------------------------------------------------
# Singleton acquisition + release
# ---------------------------------------------------------------------------


def acquire_singleton(
    name: str,
    *,
    project_root: Path,
    probe_url: str | None = None,
) -> PidLock | ReusedSubsystem:
    """Try to claim the ``name`` singleton slot for the current process.

    Resolution order:

    1. Read ``<project_root>/.ctxr-fsm/pids/<name>.pid``.
    2. If the file names a *live* pid (per :func:`pid_is_alive`) AND
       a ``probe_url`` is recorded AND ``GET <probe_url>/healthz``
       returns 200 → return :class:`ReusedSubsystem` so the caller
       can adopt the existing instance.
    3. Otherwise (file missing, stale pid, or health probe failed)
       → write our own pid + ``probe_url`` + ``acquired_at`` into the
       file and return :class:`PidLock`.

    The ``probe_url`` argument is the *base* URL of the subsystem
    we're about to start (e.g. ``http://127.0.0.1:8765``); the
    ``/healthz`` suffix is appended internally so callers don't have
    to remember it. Passing ``probe_url=None`` disables the HTTP probe
    branch entirely — a live pid alone is then enough to short-circuit
    to :class:`ReusedSubsystem`, which is the right behaviour for
    non-HTTP subsystems like the watcher.
    """
    pid_path = _pids_dir(project_root) / f"{name}.pid"
    existing = read_pid_file(pid_path)

    if existing is not None:
        raw_pid = existing.get("pid")
        existing_pid = int(raw_pid) if isinstance(raw_pid, int) else 0
        existing_probe_raw = existing.get("probe_url")
        existing_probe = (
            existing_probe_raw if isinstance(existing_probe_raw, str) else None
        )

        if existing_pid > 0 and pid_is_alive(existing_pid):
            # We have a live owner. If they advertise a probe URL we
            # must confirm they're answering — a hung process that
            # never released the port is the failure mode this guard
            # exists to catch.
            if existing_probe is not None:
                health = _probe_healthz(existing_probe)
                if health is not None:
                    return ReusedSubsystem(
                        name=name,
                        existing_pid=existing_pid,
                        probe_url=existing_probe,
                        health_status=health.strip() or "ok",
                    )
                # Live pid, dead probe: treat as stale and take over.
            elif probe_url is None:
                # Both sides opted out of HTTP probing; a live pid is
                # all the proof of life we have, and it's enough.
                return ReusedSubsystem(
                    name=name,
                    existing_pid=existing_pid,
                    probe_url="",
                    health_status="alive",
                )
            # Else: existing record has no probe but we *do* — treat
            # as stale (the previous owner didn't advertise the same
            # contract we're about to register) and take over.

    # No live owner (or stale lock): claim the slot for ourselves.
    lock = PidLock(
        name=name,
        pid=os.getpid(),
        probe_url=probe_url,
        acquired_at=_now_iso(),
    )
    write_pid_file(pid_path, asdict(lock))
    return lock


def release_singleton(lock: PidLock, *, project_root: Path) -> None:
    """Delete the pid file for ``lock`` iff we still own it.

    The "iff we still own it" check is critical: if another instance
    *took over* the slot (because we were killed without releasing,
    then the user restarted us in a new process), the on-disk pid
    would no longer be ours and deleting the file would silently
    strand the new owner's record. Comparing pids before deleting
    keeps that hand-off safe.
    """
    pid_path = _pids_dir(project_root) / f"{lock.name}.pid"
    current = read_pid_file(pid_path)
    if current is None:
        return
    raw_pid = current.get("pid")
    current_pid = int(raw_pid) if isinstance(raw_pid, int) else 0
    if current_pid != lock.pid:
        # Someone else owns the slot now; leave their record intact.
        return
    try:
        pid_path.unlink()
    except FileNotFoundError:
        # Raced with another cleanup — the post-condition (file gone)
        # already holds, so we're done.
        return


# ---------------------------------------------------------------------------
# Active-run marker
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Active-MCP discovery file (W14c)
# ---------------------------------------------------------------------------


def active_mcp_file_path(project_root: Path) -> Path:
    """Return ``<project_root>/.ctxr-fsm/active-mcp.json`` (no I/O).

    Pure path helper exposed so callers that want to ``stat()`` the
    file or remove it manually (tests, ``ensure --check``) reach the
    same location the writer uses without duplicating the constant.
    We deliberately do NOT go through :func:`_state_dir` — that
    helper has a side effect (``mkdir(exist_ok=True)``) which would
    violate the "read-only probe" contract :func:`ensure --check`
    relies on. The bare ``project_root / .ctxr-fsm / ...`` join is
    semantically identical for reads.
    """
    return project_root / _STATE_DIR_NAME / _ACTIVE_MCP_FILE_NAME


def remember_active_mcp_file(
    payload: dict[str, Any], *, project_root: Path
) -> None:
    """Atomically write the supervisor's active-MCP discovery document.

    Sole writer is the W7 supervisor, once its MCP child's ``/healthz``
    is answering. The skill bootstrap discipline (W14e) tells agents to
    parse this file when the stdio MCP entry is not registered with
    the active client yet, so the contents MUST match the
    documented schema:

    ``{started_at, supervisor_pid, version, subsystems: {<name>:
    {http_url, healthz_url, pid, docs_url?}}}``.

    The write goes through :func:`_atomic_write_text` (tmp + rename)
    so a concurrent reader either sees the previous document or the
    new one in full — never a half-written file that JSON would
    reject. ``_state_dir`` is called explicitly here (rather than via
    :func:`active_mcp_file_path`) so the ``.ctxr-fsm/`` directory is
    created on the write path even though the read-only path-helper
    is now side-effect-free.
    """
    path = _state_dir(project_root) / _ACTIVE_MCP_FILE_NAME
    _atomic_write_text(path, json.dumps(payload, sort_keys=True, indent=2))


def read_active_mcp_file(project_root: Path) -> dict[str, Any] | None:
    """Return the parsed active-MCP document, or ``None`` if absent / bad.

    Mirror of :func:`read_pid_file`: missing file and malformed file
    collapse to the same return value because every caller's natural
    reaction in both cases is the same — "treat the supervisor as not
    ready, fall through to a spawn-or-wait branch".
    """
    path = active_mcp_file_path(project_root)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def remove_active_mcp_file(project_root: Path) -> None:
    """Best-effort removal of the active-MCP discovery document.

    Called by the supervisor's graceful shutdown path so a stopped
    project never leaves a stale document pointing at a now-dead
    port. ``FileNotFoundError`` is the post-condition we want
    (file gone) so we swallow it; any other OSError surfaces as a
    log line elsewhere — the supervisor is already shutting down
    and a noisy crash would just confuse the operator.
    """
    path = active_mcp_file_path(project_root)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def write_active_run_marker(
    run_id: str | None,
    *,
    project_root: Path,
    allowed_tools: list[str] | None = None,
    current_state: str | None = None,
) -> None:
    """Publish (or clear) the active-run marker the W12 hook reads.

    The marker lives at ``<project_root>/.ctxr-fsm/active-run.json``
    rather than ``~/.ctxr-fsm/active-run.json`` so two checkouts of
    the same repo on one machine never overwrite each other's marker.
    The W12 Claude Code hook walks up from ``$CLAUDE_PROJECT_DIR`` to
    find the nearest ``.ctxr-fsm`` directory, which makes the
    per-project layout transparent on the consumer side.

    Parameters
    ----------
    run_id:
        UUID-as-string for the in-flight FSM run, or ``None`` to clear
        the marker entirely (used by ``fsm.abort_run`` / terminal
        commits / supervisor shutdown so a later hook invocation
        doesn't attach an event to a run that's no longer current).
    project_root:
        Directory whose ``.ctxr-fsm/`` subtree owns the marker. The
        directory is created on demand.
    allowed_tools:
        The current FSM state's allowed_tools list — the hook adds
        ``fsm.*`` implicitly and then blocks any tool call that
        matches neither. ``None`` (or empty list) means the worker is
        unrestricted within the FSM run, in which case the hook will
        not block any tool call.
    current_state:
        The current state id, recorded purely for diagnostics so
        ``doctor`` / hook stderr can name the state that imposed the
        allowlist. Optional; omitted from the document if ``None``.
    """
    path = _state_dir(project_root) / _ACTIVE_RUN_FILE_NAME
    if run_id is None:
        try:
            path.unlink()
        except FileNotFoundError:
            # Already absent — nothing to clear, no need to surface
            # the race as an error.
            return
        return
    payload: dict[str, Any] = {
        "run_id": run_id,
        "set_at": _now_iso(),
        "allowed_tools": list(allowed_tools or []),
    }
    if current_state is not None:
        payload["current_state"] = current_state
    _atomic_write_text(path, json.dumps(payload, sort_keys=True, indent=2))
