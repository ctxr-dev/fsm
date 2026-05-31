"""``ctxr-fsm urls`` (alias ``show``) - minimal "where do I go?" command.

Prints ONLY the Rich subsystem table (Project / mcp / api / ui rows,
each with URL + Swagger + Health + PID). No DB diagnostic panel,
no alembic migration chatter, no JSON envelope clutter.

This is the one-keystroke shortcut for the common operator question
"where is the UI / Swagger / MCP endpoint on this project?" so an
operator does not have to remember ``ctxr-fsm doctor`` or read its
multi-line diagnostic body just to see four URLs.

Reads the supervisor's ``.ctxr-fsm/active-mcp.json`` discovery file
(W14c) for the URL set. If the supervisor isn't running, prints a
friendly "no supervisor" message + a hint to run ``ctxr-fsm ensure``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ctxr.fsm.cli._render import print_subsystem_table
from ctxr.fsm.cli.lifecycle.primitives import read_active_mcp_file

__all__ = ["urls"]


def urls(
    project_root: Annotated[
        Path | None,
        typer.Option(
            "--project-root",
            "-r",
            help=(
                "Project root to inspect. Defaults to walk-up from cwd "
                "looking for .ctxr-fsm/; falls back to cwd when no "
                "ancestor matches."
            ),
        ),
    ] = None,
) -> None:
    """Print the subsystem URL table for the current project.

    The one-keystroke "where do I go?" command. Equivalent to
    ``ctxr-fsm doctor`` minus every diagnostic line except the URL
    table. Use this when you just want to click through to the UI /
    Swagger / MCP endpoint.
    """
    root = _resolve_project_root(project_root)
    active = read_active_mcp_file(root)
    if active is None:
        Console().print(
            "[yellow]no supervisor running for this project[/yellow]\n"
            f"  project_root: {root}\n"
            "  no [bold].ctxr-fsm/active-mcp.json[/bold] found\n\n"
            "  Start it with: [bold]uv run ctxr-fsm ensure[/bold]\n"
        )
        raise typer.Exit(code=1)
    print_subsystem_table(active, project_root=root, console=Console())


def _resolve_project_root(explicit: Path | None) -> Path:
    """Mirror ensure_cmd / mcp.server walk-up so behaviour stays consistent.

    Three-tier resolution: explicit flag > walk-up for ``.ctxr-fsm/`` >
    cwd fallback. Same convention every ctxr-fsm CLI command follows so
    the operator does not have to think about which command is which.
    """
    if explicit is not None:
        return explicit.expanduser().resolve()
    current = Path.cwd().resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".ctxr-fsm").is_dir():
            return candidate
    return current


def main() -> int:  # pragma: no cover - CLI entry mirror
    try:
        urls()
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
