"""Unit tests for ``ctxr.fsm.cli.lifecycle.primitives.pick_port`` + port memory.

Covers the two contracts the supervisor + ``serve`` / ``ui`` / ``mcp``
commands rely on:

1. :func:`pick_port` prefers the requested port when free, and falls
   back to an ephemeral free port when the preferred port is bound by
   somebody else (``EADDRINUSE``).
2. :func:`remember_port` is idempotent — calling it twice with the
   same ``(name, port)`` leaves ``ports.json`` byte-identical, and a
   subsequent update for a *different* subsystem preserves the prior
   entry.

The "preferred port busy" path is exercised by binding the preferred
port from the test itself; that's both deterministic (no reliance on
which ports happen to be free on CI) and faithful to the real failure
mode the supervisor handles in production.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from ctxr.fsm.cli.lifecycle.primitives import pick_port, recall_port, remember_port

# ---------------------------------------------------------------------------
# pick_port
# ---------------------------------------------------------------------------


def _bind_ephemeral_blocker() -> tuple[socket.socket, int]:
    """Bind ``127.0.0.1:0`` and return ``(sock, port)``.

    The returned socket must be kept alive by the caller (otherwise
    the kernel releases the port immediately and the "busy" branch
    we're trying to exercise no longer fires). We use an ephemeral
    port rather than a hard-coded one so the test never collides
    with a real service that happens to be listening on the dev
    machine.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    return sock, port


def test_pick_port_returns_preferred_when_free() -> None:
    """A free preferred port is returned verbatim (no fallback)."""
    blocker, port = _bind_ephemeral_blocker()
    # Release immediately so the port is genuinely free for the
    # subsequent ``pick_port`` call. Using a just-released ephemeral
    # port keeps the test from depending on any specific number.
    blocker.close()

    picked = pick_port(port)
    assert picked == port


def test_pick_port_falls_back_when_preferred_busy() -> None:
    """``EADDRINUSE`` triggers fallback to an ephemeral free port."""
    blocker, busy_port = _bind_ephemeral_blocker()
    try:
        picked = pick_port(busy_port)
    finally:
        blocker.close()

    # Fallback must produce a different, non-zero, valid TCP port.
    assert picked != busy_port
    assert 1024 <= picked <= 65535


def test_pick_port_no_preferred_returns_ephemeral() -> None:
    """``preferred=None`` short-circuits straight to ephemeral allocation."""
    picked = pick_port(None)
    assert 1024 <= picked <= 65535


# ---------------------------------------------------------------------------
# remember_port / recall_port
# ---------------------------------------------------------------------------


def test_remember_port_then_recall_round_trip(tmp_path: Path) -> None:
    """A remembered port survives a recall via a fresh function call."""
    remember_port("api", 8765, project_root=tmp_path)
    assert recall_port("api", project_root=tmp_path) == 8765


def test_remember_port_is_idempotent(tmp_path: Path) -> None:
    """Two identical writes produce byte-identical ``ports.json`` files.

    Idempotency is the property the supervisor relies on when it
    restarts a subsystem: re-registering the same port must not churn
    the file's mtime/contents in a way that confuses watchers or
    diff-friendly inspection.
    """
    remember_port("api", 8765, project_root=tmp_path)
    first = (tmp_path / ".ctxr-fsm" / "ports.json").read_bytes()

    remember_port("api", 8765, project_root=tmp_path)
    second = (tmp_path / ".ctxr-fsm" / "ports.json").read_bytes()

    assert first == second


def test_remember_port_preserves_other_entries(tmp_path: Path) -> None:
    """Updating one subsystem's port leaves the others untouched."""
    remember_port("api", 8001, project_root=tmp_path)
    remember_port("ui", 5173, project_root=tmp_path)
    remember_port("mcp", 8910, project_root=tmp_path)

    # Overwrite a single key.
    remember_port("api", 9001, project_root=tmp_path)

    assert recall_port("api", project_root=tmp_path) == 9001
    assert recall_port("ui", project_root=tmp_path) == 5173
    assert recall_port("mcp", project_root=tmp_path) == 8910


def test_recall_port_unknown_returns_none(tmp_path: Path) -> None:
    """An unknown subsystem name yields ``None``, not a KeyError."""
    remember_port("api", 8001, project_root=tmp_path)
    assert recall_port("ui", project_root=tmp_path) is None


def test_recall_port_missing_file_returns_none(tmp_path: Path) -> None:
    """A project that has never remembered any port yields ``None``."""
    assert recall_port("api", project_root=tmp_path) is None


def test_remember_port_recovers_from_corrupt_json(tmp_path: Path) -> None:
    """A garbled ``ports.json`` is rewritten cleanly on next remember.

    The lifecycle module treats the file as a cache, not a contract;
    a hand-edit that introduces invalid JSON must not crash the next
    subsystem startup.
    """
    state_dir = tmp_path / ".ctxr-fsm"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "ports.json").write_text("not json at all", encoding="utf-8")

    remember_port("api", 8765, project_root=tmp_path)
    parsed = json.loads((state_dir / "ports.json").read_text(encoding="utf-8"))
    assert parsed == {"api": 8765}


# ---------------------------------------------------------------------------
# Combined: pick + remember + recall
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subsystem", ["api", "ui", "mcp"])
def test_pick_then_remember_then_recall(subsystem: str, tmp_path: Path) -> None:
    """End-to-end: ``pick_port → remember_port → recall_port`` round-trips."""
    picked = pick_port(None)
    remember_port(subsystem, picked, project_root=tmp_path)
    assert recall_port(subsystem, project_root=tmp_path) == picked
