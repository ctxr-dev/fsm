"""--dry-run, --check, and reapply modes for ``ctxr-fsm install-mcp`` (W14d).

These cross-cutting tests target the contract every per-client merger
honours:

* ``--dry-run`` never writes to the filesystem.
* ``--check`` reports per-client status without applying.
* A second invocation after a fresh install is a no-op (no rewrite,
  ``action=unchanged``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctxr.fsm.cli.install_mcp_cmd import run_install_mcp


def _seed_workspace(tmp_path: Path) -> Path:
    """Make a tmp dir look like a Claude workspace root for auto detection."""
    (tmp_path / "CLAUDE.md").write_text("# project memory\n", encoding="utf-8")
    return tmp_path


def test_dry_run_does_not_mutate_filesystem(tmp_path: Path) -> None:
    """``dry_run=True`` returns a preview but never writes any file.

    Verified by:

    * The would-be ``.mcp.json`` does not exist after the call.
    * The result includes a ``preview`` field with the patch content.
    """
    proj = _seed_workspace(tmp_path)
    mcp_json = proj / ".mcp.json"

    result = run_install_mcp(proj, client="claude", dry_run=True)
    claude_row = result["results"][0]
    assert claude_row["action"] in {"would-create", "would-apply"}, claude_row
    assert "preview" in claude_row
    # File was never created.
    assert not mcp_json.exists()


def test_check_reports_missing_for_fresh_project(tmp_path: Path) -> None:
    """``check=True`` on an uninitialised project returns 'missing'.

    No filesystem mutation; the file should still not exist after
    the probe.
    """
    proj = _seed_workspace(tmp_path)
    mcp_json = proj / ".mcp.json"

    result = run_install_mcp(proj, client="claude", check=True)
    claude_row = result["results"][0]
    assert claude_row["action"] == "check:missing"
    assert claude_row["status"] == "missing"
    assert not mcp_json.exists()


def test_check_reports_installed_after_apply(tmp_path: Path) -> None:
    """Run apply, then check, then expect ``installed``."""
    proj = _seed_workspace(tmp_path)
    run_install_mcp(proj, client="claude")
    result = run_install_mcp(proj, client="claude", check=True)
    claude_row = result["results"][0]
    assert claude_row["action"] == "check:installed", claude_row
    assert claude_row["status"] == "installed"


def test_check_reports_out_of_date_when_entry_differs(
    tmp_path: Path,
) -> None:
    """A pre-existing ctxr-fsm entry with stale args reports out-of-date.

    Models the case where a future release changes the stdio invocation
    shape; the current installer's contract is to detect it via
    check mode without rewriting.
    """
    proj = _seed_workspace(tmp_path)
    mcp_json = proj / ".mcp.json"
    mcp_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ctxr-fsm": {
                        "command": "ctxr-fsm",
                        "args": ["mcp"],  # missing --transport stdio
                        "env": {},
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = run_install_mcp(proj, client="claude", check=True)
    claude_row = result["results"][0]
    assert claude_row["action"] == "check:out_of_date"
    assert claude_row["status"] == "out_of_date"


def test_client_none_is_noop(tmp_path: Path) -> None:
    """``client='none'`` short-circuits before any detection or writes.

    Useful for ``ctxr-fsm ensure --no-mcp-config`` so the ensure
    machinery can call through a single helper without branching.
    """
    proj = _seed_workspace(tmp_path)
    result = run_install_mcp(proj, client="none")
    assert result["results"] == []


def test_rejects_unknown_client(tmp_path: Path) -> None:
    """An unknown ``client`` value raises a clear ValueError."""
    proj = _seed_workspace(tmp_path)
    with pytest.raises(ValueError, match="client must be one of"):
        run_install_mcp(proj, client="garbage")  # type: ignore[arg-type]
