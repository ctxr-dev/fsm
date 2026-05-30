"""Unit tests for the ``active-mcp.json`` primitives (W14c).

Covers the read / write / remove + path helpers in isolation:

* Atomic write goes through tmp + rename (we observe the
  ``.tmp`` sibling is gone after the call).
* Re-reads round-trip the payload exactly.
* Missing file collapses to ``None`` (no exception).
* Malformed JSON collapses to ``None``.
* ``remove`` is idempotent when the file is already gone.
* ``now_iso_ms`` returns a string with millisecond precision and the
  ``+00:00`` UTC offset (project convention).

The supervisor end-to-end behaviour (file written on boot, removed on
shutdown) lives in ``tests/integration/lifecycle/test_active_mcp_json.py``
because it needs a real ``ctxr-fsm serve`` subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path

from ctxr.fsm.cli.lifecycle.primitives import (
    active_mcp_file_path,
    now_iso_ms,
    read_active_mcp_file,
    remember_active_mcp_file,
    remove_active_mcp_file,
)


def test_active_mcp_file_path_under_state_dir(tmp_path: Path) -> None:
    """Path helper resolves under ``<project_root>/.ctxr-fsm/`` exactly.

    The skill bootstrap discipline (W14e) tells agents to look for the
    file at this exact location; an accidental relocation (e.g. into
    ``pids/``) would silently break every skill that depends on the
    fallback path.
    """
    path = active_mcp_file_path(tmp_path)
    assert path == tmp_path / ".ctxr-fsm" / "active-mcp.json"


def test_remember_round_trips(tmp_path: Path) -> None:
    """Write + read returns the same document byte-for-equivalent.

    ``json.dumps(sort_keys=True)`` makes the on-disk bytes
    deterministic, and ``json.loads`` mirrors it back into the same
    dict.
    """
    payload = {
        "started_at": now_iso_ms(),
        "supervisor_pid": 1234,
        "version": "0.2.0",
        "subsystems": {
            "mcp": {
                "http_url": "http://127.0.0.1:8770/sse",
                "healthz_url": "http://127.0.0.1:8770/healthz",
                "pid": 5,
            },
        },
    }
    remember_active_mcp_file(payload, project_root=tmp_path)
    loaded = read_active_mcp_file(tmp_path)
    assert loaded == payload


def test_remember_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    """The tmp-then-rename idiom does not leave a ``.tmp`` sibling."""
    remember_active_mcp_file(
        {"started_at": now_iso_ms(), "supervisor_pid": 0,
         "version": "0.0.0", "subsystems": {}},
        project_root=tmp_path,
    )
    state_dir = tmp_path / ".ctxr-fsm"
    siblings = sorted(p.name for p in state_dir.iterdir())
    assert "active-mcp.json" in siblings
    # Strictly: NO ``.tmp`` artefact.
    assert not any(name.endswith(".tmp") for name in siblings), siblings


def test_read_returns_none_when_missing(tmp_path: Path) -> None:
    """Missing file collapses to ``None`` — caller treats supervisor as down."""
    assert read_active_mcp_file(tmp_path) is None


def test_read_returns_none_on_malformed_json(tmp_path: Path) -> None:
    """A hand-edit that breaks JSON does not crash the reader.

    Real-world failure: an operator opens the file in an editor and
    saves a half-edit. The skill bootstrap retries via fall-through;
    a crash here would block the retry on a non-bug.
    """
    (tmp_path / ".ctxr-fsm").mkdir(parents=True, exist_ok=True)
    active_mcp_file_path(tmp_path).write_text("{not valid json", encoding="utf-8")
    assert read_active_mcp_file(tmp_path) is None


def test_remove_is_idempotent(tmp_path: Path) -> None:
    """Removing a non-existent file is a no-op, not an exception.

    Shutdown paths fire ``remove_active_mcp_file`` unconditionally; a
    second supervisor crash that never wrote the file in the first
    place would otherwise crash the cleanup.
    """
    # First call: file is absent → no-op.
    remove_active_mcp_file(project_root=tmp_path)
    # Now create and remove.
    remember_active_mcp_file(
        {"started_at": now_iso_ms(), "supervisor_pid": 0,
         "version": "0.0.0", "subsystems": {}},
        project_root=tmp_path,
    )
    assert active_mcp_file_path(tmp_path).exists()
    remove_active_mcp_file(project_root=tmp_path)
    assert not active_mcp_file_path(tmp_path).exists()
    # Third call after removal: still a no-op.
    remove_active_mcp_file(project_root=tmp_path)


def test_now_iso_ms_has_millisecond_precision() -> None:
    """``now_iso_ms`` truncates to milliseconds and includes a UTC offset.

    Project convention (per the plan): every timestamp the FSM
    substrate persists is ISO-8601 in UTC with millisecond precision.
    Microsecond precision would make CLI output noisier than humans
    need and microsecond-vs-millisecond drift breaks the row-comparison
    contracts in the dummy-fsm-test (W14h).
    """
    s = now_iso_ms()
    # ``2026-05-29T12:34:56.789+00:00`` — 29 chars exactly.
    assert s.endswith("+00:00"), s
    # The dot must precede a 3-digit ms run + the offset.
    assert "." in s
    ms_part = s.split(".", 1)[1].split("+", 1)[0]
    assert ms_part.isdigit() and len(ms_part) == 3, s


def test_remember_overwrites_existing_document(tmp_path: Path) -> None:
    """A second write replaces the first verbatim (no append, no merge).

    The supervisor calls ``remember_active_mcp_file`` on every reload;
    this contract means a previous document's stale subsystem block
    cannot leak into a fresh one.
    """
    first = {
        "started_at": now_iso_ms(),
        "supervisor_pid": 1,
        "version": "0.0.1",
        "subsystems": {"mcp": {"http_url": "http://127.0.0.1:1/sse"}},
    }
    second = {
        "started_at": now_iso_ms(),
        "supervisor_pid": 2,
        "version": "0.0.2",
        "subsystems": {"mcp": {"http_url": "http://127.0.0.1:2/sse"}},
    }
    remember_active_mcp_file(first, project_root=tmp_path)
    remember_active_mcp_file(second, project_root=tmp_path)
    bytes_on_disk = active_mcp_file_path(tmp_path).read_text(encoding="utf-8")
    loaded = json.loads(bytes_on_disk)
    assert loaded == second
