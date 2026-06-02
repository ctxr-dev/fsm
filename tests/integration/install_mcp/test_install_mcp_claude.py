"""Coverage for the Claude side of ``ctxr-fsm install-mcp`` (W14d).

Three scenarios:

1. **Preserves unrelated entries.** A pre-existing ``.mcp.json`` that
   already lists three other MCP servers in ``mcpServers`` must come
   out the other side of an ``install-mcp`` with all three unchanged
   and a fresh ``ctxr-fsm`` entry merged alongside.
2. **Creates from scratch.** Running ``install-mcp`` against a project
   where no ``.mcp.json`` exists creates the file with just the
   ``mcpServers.ctxr-fsm`` block.
3. **Aborts on bad shape.** If the file's ``mcpServers`` value is a
   list (not an object), the merger errors out rather than clobbering
   user data.

We exercise the pure ``run_install_mcp`` function (not the Typer
command) so the tests stay in-process and fast.
"""

from __future__ import annotations

import json
from pathlib import Path

from ctxr.fsm.cli.install_mcp_cmd import _resolve_stdio_entry, run_install_mcp

# Resolved AT IMPORT TIME from the same helper the install path uses;
# stays valid for both the uv-run shape and the absolute-path shape
# Fix 4 introduced. Hardcoding ``"ctxr-fsm"`` would silently re-bake
# the bare-literal regression the fix prevents.
EXPECTED_ENTRY = _resolve_stdio_entry()


def _seed_workspace_marker(tmp_path: Path) -> None:
    """Make ``tmp_path`` look like a Claude Code workspace root.

    The detector treats either ``.git/`` or ``CLAUDE.md`` as the
    workspace marker — we plant the cheaper one (a single file) so
    the .mcp.json path is the canonical target.
    """
    (tmp_path / "CLAUDE.md").write_text("# project memory\n", encoding="utf-8")


def test_preserves_unrelated_mcp_servers_when_merging(tmp_path: Path) -> None:
    """Existing entries in mcpServers and other top-level keys pass through.

    The brief is explicit: only ``mcpServers.ctxr-fsm`` is OWNED;
    every other key (top-level OR inside mcpServers) is sacred.
    """
    _seed_workspace_marker(tmp_path)
    mcp_json = tmp_path / ".mcp.json"
    original = {
        "$schema": "https://example.com/mcp-schema.json",
        "mcpServers": {
            "github": {
                "command": "uvx",
                "args": ["mcp-github"],
                "env": {"GITHUB_TOKEN": "***"},
            },
            "notion": {
                "command": "npx",
                "args": ["@notionhq/mcp-server"],
                "env": {},
            },
        },
        "comment": "hand-edited by the user",
    }
    mcp_json.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    result = run_install_mcp(tmp_path, client="claude")
    assert {r["client"] for r in result["results"]} == {"claude"}, result
    claude_row = result["results"][0]
    assert claude_row["action"] == "applied", claude_row

    patched = json.loads(mcp_json.read_text(encoding="utf-8"))
    # Top-level keys preserved.
    assert patched["$schema"] == original["$schema"]
    assert patched["comment"] == original["comment"]
    # Existing mcpServers entries preserved.
    assert patched["mcpServers"]["github"] == original["mcpServers"]["github"]
    assert patched["mcpServers"]["notion"] == original["mcpServers"]["notion"]
    # New entry merged with the exact desired shape.
    assert patched["mcpServers"]["ctxr-fsm"] == EXPECTED_ENTRY


def test_creates_mcp_json_when_absent(tmp_path: Path) -> None:
    """A workspace without ``.mcp.json`` gets one created on install."""
    _seed_workspace_marker(tmp_path)
    mcp_json = tmp_path / ".mcp.json"
    assert not mcp_json.exists()

    result = run_install_mcp(tmp_path, client="claude")
    claude_row = result["results"][0]
    assert claude_row["action"] == "applied", claude_row

    assert mcp_json.exists()
    loaded = json.loads(mcp_json.read_text(encoding="utf-8"))
    assert loaded == {"mcpServers": {"ctxr-fsm": EXPECTED_ENTRY}}


def test_aborts_when_mcp_servers_is_wrong_type(tmp_path: Path) -> None:
    """A list-valued ``mcpServers`` is treated as malformed and reported.

    Silently coercing the value would lose the user's data; the
    merger surfaces the error as a per-result outcome instead.
    """
    _seed_workspace_marker(tmp_path)
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps({"mcpServers": ["broken"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    result = run_install_mcp(tmp_path, client="claude")
    claude_row = result["results"][0]
    assert claude_row["action"] == "failed", claude_row
    assert "mcpServers" in claude_row["detail"]
    # File untouched on failure.
    after = json.loads(mcp_json.read_text(encoding="utf-8"))
    assert after == {"mcpServers": ["broken"]}


def test_reapply_is_unchanged(tmp_path: Path) -> None:
    """A second install on a project where the entry already matches is a no-op.

    Idempotency contract: when the existing entry deep-equals the
    desired one, we return ``action: unchanged`` and do not rewrite
    the file. We assert this by comparing mtime + bytes across runs.
    """
    _seed_workspace_marker(tmp_path)
    mcp_json = tmp_path / ".mcp.json"

    run_install_mcp(tmp_path, client="claude")
    first_bytes = mcp_json.read_bytes()
    first_mtime_ns = mcp_json.stat().st_mtime_ns

    result = run_install_mcp(tmp_path, client="claude")
    claude_row = result["results"][0]
    assert claude_row["action"] == "unchanged", claude_row

    # Byte-identical; mtime untouched (we deliberately skipped the
    # rewrite).
    assert mcp_json.read_bytes() == first_bytes
    assert mcp_json.stat().st_mtime_ns == first_mtime_ns


def test_preserves_existing_indent_style(tmp_path: Path) -> None:
    """If the file uses 4-space indent, the rewrite stays 4-space.

    Keeps reviewer diffs focused on the actual config change rather
    than whitespace churn.
    """
    _seed_workspace_marker(tmp_path)
    mcp_json = tmp_path / ".mcp.json"
    original = (
        '{\n'
        '    "mcpServers": {\n'
        '        "github": {\n'
        '            "command": "uvx"\n'
        '        }\n'
        '    }\n'
        '}\n'
    )
    mcp_json.write_text(original, encoding="utf-8")

    run_install_mcp(tmp_path, client="claude")
    text = mcp_json.read_text(encoding="utf-8")
    # The first nested key line should still be indented by 4 spaces.
    nested_line = next(
        ln for ln in text.splitlines() if ln.startswith(" ") and ln.lstrip()
    )
    leading = len(nested_line) - len(nested_line.lstrip(" "))
    assert leading == 4, f"expected 4-space indent preserved; got {leading}: {nested_line!r}"
