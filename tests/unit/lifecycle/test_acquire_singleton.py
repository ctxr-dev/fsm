"""Unit tests for ``acquire_singleton`` / ``release_singleton``.

Covers the two outcomes the supervisor branches on:

* **Reuse.** A pid file pointing to a live process AND a probe URL
  whose ``/healthz`` answers 200 → :class:`ReusedSubsystem`. The
  caller's correct behaviour is to skip its own startup work.

* **Replace.** A pid file naming a dead pid (or pointing to a live
  pid whose ``/healthz`` is down) → :class:`PidLock`, and the pid
  file on disk now records our pid.

We monkeypatch :func:`pid_is_alive` and :func:`_probe_healthz` inside
the primitives module to keep the tests fully hermetic — spawning a
real subprocess or starting a real HTTP server would slow the suite
without exercising any logic that isn't already covered by direct
behavioural checks on the resolution rules.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ctxr.fsm.cli.lifecycle import primitives
from ctxr.fsm.cli.lifecycle.primitives import (
    PidLock,
    ReusedSubsystem,
    acquire_singleton,
    read_pid_file,
    release_singleton,
    write_pid_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pid_path(project_root: Path, name: str) -> Path:
    """Return the canonical pid-file path for ``name`` under ``project_root``."""
    return project_root / ".ctxr-fsm" / "pids" / f"{name}.pid"


def _seed_pid_file(
    project_root: Path,
    name: str,
    *,
    pid: int,
    probe_url: str | None,
    acquired_at: str = "2025-01-01T00:00:00+00:00",
) -> Path:
    """Write a hand-crafted pid file so the test fully controls the prior owner."""
    path = _pid_path(project_root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_pid_file(
        path,
        {
            "name": name,
            "pid": pid,
            "probe_url": probe_url,
            "acquired_at": acquired_at,
        },
    )
    return path


# ---------------------------------------------------------------------------
# Reuse paths
# ---------------------------------------------------------------------------


def test_acquire_reuses_when_pid_alive_and_probe_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live pid + healthy probe → :class:`ReusedSubsystem`."""
    _seed_pid_file(tmp_path, "api", pid=99999, probe_url="http://127.0.0.1:8765")

    monkeypatch.setattr(primitives, "pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(primitives, "_probe_healthz", lambda _url, timeout=1.0: "ok")

    result = acquire_singleton(
        "api",
        project_root=tmp_path,
        probe_url="http://127.0.0.1:8765",
    )

    assert isinstance(result, ReusedSubsystem)
    assert result.existing_pid == 99999
    assert result.probe_url == "http://127.0.0.1:8765"
    assert result.health_status == "ok"

    # Reuse must not rewrite the pid file with our pid.
    on_disk = read_pid_file(_pid_path(tmp_path, "api"))
    assert on_disk is not None
    assert on_disk["pid"] == 99999


def test_acquire_reuses_when_no_probe_on_either_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live pid with no probe contract on either side → reuse without HTTP probe."""
    _seed_pid_file(tmp_path, "watcher", pid=99999, probe_url=None)
    monkeypatch.setattr(primitives, "pid_is_alive", lambda _pid: True)

    result = acquire_singleton("watcher", project_root=tmp_path, probe_url=None)

    assert isinstance(result, ReusedSubsystem)
    assert result.existing_pid == 99999
    assert result.health_status == "alive"


# ---------------------------------------------------------------------------
# Replace paths
# ---------------------------------------------------------------------------


def test_acquire_replaces_when_pid_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dead pid → :class:`PidLock`, file rewritten with our pid."""
    _seed_pid_file(tmp_path, "api", pid=99999, probe_url="http://127.0.0.1:8765")
    monkeypatch.setattr(primitives, "pid_is_alive", lambda _pid: False)

    result = acquire_singleton(
        "api",
        project_root=tmp_path,
        probe_url="http://127.0.0.1:8765",
    )

    assert isinstance(result, PidLock)
    assert result.pid == os.getpid()
    assert result.probe_url == "http://127.0.0.1:8765"
    assert result.name == "api"

    on_disk = read_pid_file(_pid_path(tmp_path, "api"))
    assert on_disk is not None
    assert on_disk["pid"] == os.getpid()
    assert on_disk["probe_url"] == "http://127.0.0.1:8765"


def test_acquire_replaces_when_probe_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live pid but probe down → take over (stale-but-hung process)."""
    _seed_pid_file(tmp_path, "api", pid=99999, probe_url="http://127.0.0.1:8765")

    monkeypatch.setattr(primitives, "pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(primitives, "_probe_healthz", lambda _url, timeout=1.0: None)

    result = acquire_singleton(
        "api",
        project_root=tmp_path,
        probe_url="http://127.0.0.1:8765",
    )

    assert isinstance(result, PidLock)
    assert result.pid == os.getpid()


def test_acquire_with_no_existing_file_writes_lock(tmp_path: Path) -> None:
    """No prior owner → :class:`PidLock` is written from scratch."""
    result = acquire_singleton("api", project_root=tmp_path, probe_url=None)

    assert isinstance(result, PidLock)
    assert result.pid == os.getpid()
    assert result.probe_url is None

    parsed = json.loads(_pid_path(tmp_path, "api").read_text(encoding="utf-8"))
    assert parsed["pid"] == os.getpid()
    assert parsed["name"] == "api"


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_release_removes_file_when_we_own_it(tmp_path: Path) -> None:
    """A normal release deletes the pid file we wrote."""
    lock = acquire_singleton("api", project_root=tmp_path, probe_url=None)
    assert isinstance(lock, PidLock)
    assert _pid_path(tmp_path, "api").exists()

    release_singleton(lock, project_root=tmp_path)
    assert not _pid_path(tmp_path, "api").exists()


def test_release_preserves_takeover_lock(tmp_path: Path) -> None:
    """If another instance has overwritten the slot, release leaves it alone."""
    lock = acquire_singleton("api", project_root=tmp_path, probe_url=None)
    assert isinstance(lock, PidLock)

    # Simulate a takeover: someone else wrote their own pid into the
    # file while we weren't looking.
    foreign_pid = os.getpid() + 1
    write_pid_file(
        _pid_path(tmp_path, "api"),
        {
            "name": "api",
            "pid": foreign_pid,
            "probe_url": None,
            "acquired_at": "2025-01-02T00:00:00+00:00",
        },
    )

    release_singleton(lock, project_root=tmp_path)

    # The foreign record must survive — releasing our (stale) lock
    # must never strand the new owner.
    on_disk = read_pid_file(_pid_path(tmp_path, "api"))
    assert on_disk is not None
    assert on_disk["pid"] == foreign_pid


def test_release_is_idempotent_when_file_missing(tmp_path: Path) -> None:
    """Calling release after the file is already gone is a no-op."""
    lock = acquire_singleton("api", project_root=tmp_path, probe_url=None)
    assert isinstance(lock, PidLock)

    _pid_path(tmp_path, "api").unlink()
    # Should not raise.
    release_singleton(lock, project_root=tmp_path)
