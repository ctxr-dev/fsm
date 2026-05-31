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
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console

from ctxr.fsm.cli._render import print_subsystem_table
from ctxr.fsm.cli.lifecycle.primitives import (
    pid_is_alive,
    read_active_mcp_file,
)

__all__ = ["urls"]


def _live_status_for_subsystem(block: dict[str, Any]) -> str:
    """Probe one subsystem's current liveness for the status column.

    The W14c discovery file describes "what the supervisor published at
    boot", not "what's healthy right now" — pids die, daemons crash,
    healthz starts failing. ``urls`` is an operator question ("can I
    click these?") so the answer MUST be live; trusting a stale
    snapshot is exactly how the user got dead links displayed as
    "ready".

    Probe order: pid alive? → healthz responds 200? When the subsystem
    has no healthz url (the UI's Vite dev server case), a live pid is
    enough. Returns one of the keys ``_render._STATUS_GLYPHS`` knows
    about (``ready`` / ``unreachable`` / ``missing``).
    """
    pid = block.get("pid")
    if not isinstance(pid, int) or not pid_is_alive(pid):
        return "missing"

    healthz_url = block.get("healthz_url")
    if not isinstance(healthz_url, str) or not healthz_url:
        # Live pid, no probe URL declared (UI). Best signal we have.
        return "ready"

    try:
        resp = httpx.get(healthz_url, timeout=0.5)
    except (httpx.HTTPError, OSError):
        return "unreachable"
    if resp.status_code != 200:
        return "unreachable"
    return "ready"


def _augment_active_with_live_status(active: dict[str, Any]) -> dict[str, Any]:
    """Re-probe every subsystem in ``active`` and inject the live status.

    Mirror of ``supervisor._augment_active_with_status`` but probes the
    real OS + network state instead of setting a default. The W14j
    renderer's colour mapping is keyed on the per-subsystem ``status``;
    feeding it live values means an operator running ``urls`` sees the
    actual current state, not the stale boot-time snapshot.
    """
    subsystems = active.get("subsystems") or {}
    if not isinstance(subsystems, dict):
        return active
    new_subs: dict[str, Any] = {}
    for name, block in subsystems.items():
        if isinstance(block, dict):
            new_block = dict(block)
            new_block["status"] = _live_status_for_subsystem(block)
            new_subs[name] = new_block
        else:
            new_subs[name] = block
    augmented = dict(active)
    augmented["subsystems"] = new_subs
    return augmented


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
    # LIVE-probe healthz + pid_is_alive for each subsystem before
    # rendering. The discovery file is a boot-time snapshot; trusting
    # it would display dead URLs as "ready" / "spawned". The augment
    # injects per-subsystem ``status`` keys the W14j renderer's
    # colour mapping consumes.
    print_subsystem_table(
        _augment_active_with_live_status(active),
        project_root=root,
        console=Console(),
    )


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
