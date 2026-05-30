"""Process and port lifecycle primitives for ``ctxr-fsm`` subsystems.

This package centralises the low-level concerns every long-running
subsystem (API server, MCP server, UI dev server, watcher, etc.) needs
to coordinate on a single developer machine:

* **Port allocation** — :func:`pick_port` tries a preferred port first
  and falls back to an ephemeral free port; :func:`remember_port` /
  :func:`recall_port` persist the chosen number under
  ``.ctxr-fsm/ports.json`` so a restart can hand the same URL back to
  the user (and to any background tooling holding the previous URL).

* **Singleton acquisition** — :func:`acquire_singleton` uses a pid
  file plus an optional health-probe URL to decide between *reusing* a
  living instance (returning :class:`ReusedSubsystem`) and *claiming*
  the slot for ourselves (returning :class:`PidLock`).
  :func:`release_singleton` is the inverse: it only deletes the pid
  file if it still belongs to us, so a takeover by another instance
  never accidentally clobbers the new owner's record.

* **Atomic pid-file IO** — :func:`write_pid_file` / :func:`read_pid_file`
  / :func:`pid_is_alive` are small enough to inline, but pulling them
  into named functions makes the singleton dance trivial to unit-test
  without monkeypatching ``open`` / ``os.kill`` on every call site.

* **Active-run marker** — :func:`write_active_run_marker` writes the
  ``active-run.json`` file the W12 Claude Code hook reads to learn
  which run to record into. The marker lives under
  ``project_root/.ctxr-fsm/`` rather than ``~/.ctxr-fsm/`` so it
  follows the project (multiple checkouts of the same repo each get
  their own marker), which matches the per-project layout the rest of
  the lifecycle module assumes.

All files live under ``project_root/.ctxr-fsm/`` (gitignored), keeping
the runtime state perfectly co-located with the SQLite DB and the
``logs/`` tree so a developer can ``rm -rf .ctxr-fsm`` to fully reset
the project's runtime environment.
"""

from __future__ import annotations

from ctxr.fsm.cli.lifecycle.primitives import (
    PidLock,
    ReusedSubsystem,
    acquire_singleton,
    pick_port,
    pid_is_alive,
    read_pid_file,
    recall_port,
    release_singleton,
    remember_port,
    write_active_run_marker,
    write_pid_file,
)

__all__ = [
    "PidLock",
    "ReusedSubsystem",
    "acquire_singleton",
    "pick_port",
    "pid_is_alive",
    "read_pid_file",
    "recall_port",
    "release_singleton",
    "remember_port",
    "write_active_run_marker",
    "write_pid_file",
]
