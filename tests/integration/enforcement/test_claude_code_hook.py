"""Integration tests for the W12 layer-4 Claude Code pre-tool-use hook.

The hook itself is the dependency-free Python script under
``.claude/hooks/pre-tool-use.fsm-guard.py`` — we invoke it via
:mod:`subprocess` so the tests mirror exactly how Claude Code would
exec the hook in production: a fresh process per call, stdin carries
the JSON payload, stderr carries the structured block reason, the exit
code carries the allow/block decision.

Scenarios covered
-----------------

* No marker present → exit ``0`` (allow everything; installing the
  hook never bricks a session that has no FSM run live).
* Marker with ``allowed_tools=["Read", ...]`` and ``tool_name="Read"``
  → exit ``0``.
* Marker with same allowlist and ``tool_name="Bash"`` → exit ``1``
  and a structured ``"blocked": true`` JSON line on stderr.
* Implicit ``fsm.*`` wildcard always allows ``fsm.commit_outputs``
  even when the state's own allowlist would not.
* Empty allowlist → allow (an empty list mirrors "no marker" — the
  state intentionally imposes no restriction).
* Malformed marker → allow (soft-fail).
* Marker walked upwards from a subdirectory of ``CLAUDE_PROJECT_DIR``
  (the hook walks up from the env var, then cwd) → block still fires.

Each test creates a temp project root, optionally writes a marker
under ``.ctxr-fsm/active-run.json``, then runs the hook with
``CLAUDE_PROJECT_DIR`` pointed at the temp root so the hook discovers
the right marker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Hook script + helpers
# ---------------------------------------------------------------------------


_HOOK_PY = (
    Path(__file__).resolve().parents[3]
    / ".claude"
    / "hooks"
    / "pre-tool-use.fsm-guard.py"
)


def _write_marker(
    project_root: Path,
    *,
    run_id: str = "11111111-2222-3333-4444-555555555555",
    allowed_tools: list[str] | None = None,
    current_state: str = "draft",
    overwrite_with_raw: str | None = None,
) -> Path:
    """Drop an ``active-run.json`` under ``project_root/.ctxr-fsm/``.

    ``overwrite_with_raw`` lets a test deliberately stamp a malformed
    file (e.g. trailing junk) without going through the JSON encoder.
    """
    state_dir = project_root / ".ctxr-fsm"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "active-run.json"
    if overwrite_with_raw is not None:
        marker.write_text(overwrite_with_raw, encoding="utf-8")
        return marker
    payload = {
        "run_id": run_id,
        "set_at": "2026-05-30T12:00:00+00:00",
        "allowed_tools": list(allowed_tools or []),
        "current_state": current_state,
    }
    marker.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return marker


def _invoke_hook(
    payload: dict[str, object],
    *,
    project_root: Path,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the hook in a fresh subprocess; return the completed process.

    ``CLAUDE_PROJECT_DIR`` is set to ``project_root`` so the hook walks
    up from there. ``cwd`` defaults to ``project_root`` but a test can
    override it to exercise the "started in a subdirectory" branch.
    """
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)

    return subprocess.run(
        [sys.executable, str(_HOOK_PY)],
        input=json.dumps(payload),
        env=env,
        cwd=str(cwd or project_root),
        capture_output=True,
        text=True,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A fresh per-test project root (an empty temp dir)."""
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hook_script_exists_and_is_executable() -> None:
    """The reference hook ships under ``.claude/hooks/``.

    A missing or non-executable hook would silently degrade the rest of
    the suite (Claude Code wouldn't invoke it), so we assert presence
    + exec bit up front to fail loudly when the file is moved.
    """
    assert _HOOK_PY.is_file(), f"hook script missing at {_HOOK_PY}"
    # ``os.access`` checks the actual permission bits — we set ``+x``
    # at creation time and want to catch a regression that drops it.
    assert os.access(_HOOK_PY, os.X_OK), f"{_HOOK_PY} is not executable"


def test_no_marker_allows_any_tool(project_root: Path) -> None:
    """With no marker the hook MUST exit 0 regardless of tool name."""
    result = _invoke_hook(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        project_root=project_root,
    )
    assert result.returncode == 0, (
        f"expected allow, got exit {result.returncode}; stderr={result.stderr!r}"
    )


def test_marker_with_matching_tool_allows(project_root: Path) -> None:
    """``Read`` is in the allowlist → hook allows."""
    _write_marker(project_root, allowed_tools=["Read", "Grep"])
    result = _invoke_hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
        project_root=project_root,
    )
    assert result.returncode == 0
    assert result.stderr.strip() == ""


def test_marker_with_off_allowlist_tool_blocks(project_root: Path) -> None:
    """``Bash`` is NOT in ``[Read, Grep]`` → hook blocks (exit 1)."""
    _write_marker(project_root, allowed_tools=["Read", "Grep"])
    result = _invoke_hook(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        project_root=project_root,
    )
    assert result.returncode == 1, (
        f"expected block, got exit {result.returncode}; stderr={result.stderr!r}"
    )
    # Stderr must carry a structured ``{"blocked": true, ...}`` JSON
    # blob — Claude Code surfaces stderr verbatim to the agent.
    err_payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert err_payload["blocked"] is True
    assert err_payload["tool"] == "Bash"
    assert "Read" in err_payload["allowed"]
    assert "fsm.*" in err_payload["allowed"]
    assert "Bash" in err_payload["reason"]


def test_fsm_wildcard_is_always_allowed(project_root: Path) -> None:
    """``fsm.*`` is implicit even when the state's allowlist doesn't list it."""
    _write_marker(project_root, allowed_tools=["Read"])
    for tool in ("fsm.commit_outputs", "fsm.confirm_commit", "fsm.get_brief"):
        result = _invoke_hook(
            {"tool_name": tool, "tool_input": {}},
            project_root=project_root,
        )
        assert result.returncode == 0, (
            f"fsm.* must always pass; {tool} got exit {result.returncode} "
            f"stderr={result.stderr!r}"
        )


def test_empty_allowlist_means_no_restriction(project_root: Path) -> None:
    """Empty allow-list mirrors "no marker" — the state imposed no restriction."""
    _write_marker(project_root, allowed_tools=[])
    result = _invoke_hook(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        project_root=project_root,
    )
    assert result.returncode == 0


def test_malformed_marker_soft_fails_to_allow(project_root: Path) -> None:
    """A corrupt marker must not brick the agent — exit 0 with a stderr note."""
    _write_marker(project_root, overwrite_with_raw="{not json")
    result = _invoke_hook(
        {"tool_name": "Bash", "tool_input": {}},
        project_root=project_root,
    )
    assert result.returncode == 0
    # We don't assert exact stderr text — only that the hook stayed out
    # of the agent's way. The stderr line is for operator diagnostics.


def test_marker_found_when_cwd_is_subdirectory(project_root: Path) -> None:
    """Hook walks upwards from cwd so a sub-cwd still sees the marker.

    The Claude Code env var ``CLAUDE_PROJECT_DIR`` is pointed at the
    project root, but the test also sets cwd to a deep subdirectory to
    exercise the upward-walk branch. The block still fires because the
    marker is discovered at the project root.
    """
    deep = project_root / "src" / "pkg" / "subdir"
    deep.mkdir(parents=True, exist_ok=True)
    _write_marker(project_root, allowed_tools=["Read"])
    result = _invoke_hook(
        {"tool_name": "Bash", "tool_input": {}},
        project_root=project_root,
        cwd=deep,
    )
    assert result.returncode == 1
    err_payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert err_payload["blocked"] is True


def test_missing_tool_name_payload_allows(project_root: Path) -> None:
    """A payload without ``tool_name`` is treated as "unknown" → allow."""
    _write_marker(project_root, allowed_tools=["Read"])
    result = _invoke_hook({"tool_input": {}}, project_root=project_root)
    assert result.returncode == 0


def test_bash_shim_forwards_to_python(project_root: Path, tmp_path: Path) -> None:
    """The ``.sh`` shim exec's the python script with stdin / exit-code intact.

    Wired as a smoke test rather than a duplicate of every branch above
    — the shim is a thin ``exec`` wrapper and the substantive logic
    lives in the Python file. If the shim is broken (wrong path, lost
    stdin, swallowed exit code) this single block test catches it.
    """
    sh_hook = _HOOK_PY.with_suffix(".sh")
    assert sh_hook.is_file(), f"bash shim missing at {sh_hook}"
    assert os.access(sh_hook, os.X_OK), f"{sh_hook} is not executable"

    _write_marker(project_root, allowed_tools=["Read"])
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    result = subprocess.run(
        [str(sh_hook)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {}}),
        env=env,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    err_payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert err_payload["blocked"] is True
    assert err_payload["tool"] == "Bash"
