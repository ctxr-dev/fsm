"""Rich renderers for the human-facing CLI surface.

Every command that surfaces "where do I go" information uses
:func:`render_subsystem_table` so the table shape stays consistent
across ``ctxr-fsm ensure``, ``ctxr-fsm doctor``, and the
``ctxr-fsm serve`` supervisor boot banner. JSON output paths in those
commands stay untouched — wire-format compatibility for machine
consumers is sacred.

W14j (per the locked plan) collapses three previously-divergent
pretty-print bodies (ensure's actions summary, doctor's free-form
``rich.print`` of the report dict, and the supervisor's
``[ctxr-fsm supervisor] booted: ...`` banner line) onto one shared
table renderer so the human surface is byte-identical for "the same
state" no matter which command surfaced it. The closed-vocabulary
status mapping lives here as well so call sites do not scatter colour
choices.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

__all__ = [
    "portable_project_repr",
    "print_subsystem_table",
    "render_subsystem_table",
    "render_subsystem_urls",
]


# Status display + Rich style mapping. Closed vocabulary, hosted here
# so call sites never reach for ad-hoc colour strings.
#
# Canonical input vocabulary: ``EnsureActionStatus`` members
# (``applied``, ``current``, ``skipped``, ``unchanged``, ``reused``,
# ``spawned``, ``failed``, ``missing``) PLUS the supervisor's
# ``"ready"`` post-boot literal and the doctor's ``"unreachable"``
# probe outcome. Every member of that set MUST have an entry below.
#
# Soft-validation policy: the renderer never raises on a status word
# outside the table — it falls back to the ``unknown`` row (dim white).
# That keeps the renderer forgiving so a future caller can add a new
# status word without breaking the table, BUT every unknown lookup
# fires a one-line stderr warning so a typo in a caller surfaces
# visibly during development rather than getting silently absorbed.
# Adding a new permanent status word means adding it to the table
# below; the warning is the alarm that says "do that now".
_STATUS_GLYPHS: dict[str, tuple[str, str]] = {
    "ready": ("ready", "bold green"),
    "reused": ("reused", "bold yellow"),
    "spawned": ("spawned", "bold green"),
    "degraded": ("degraded", "bold yellow"),
    "unreachable": ("unreachable", "bold red"),
    "missing": ("missing", "bold red"),
    "failed": ("failed", "bold red"),
    "unknown": ("unknown", "dim white"),
}


def _format_status(status: str) -> tuple[str, str]:
    """Look up display label + Rich style for a subsystem status.

    Falls back to the ``unknown`` row so a brand-new status word from
    a caller renders visibly (dim white) rather than crashing the
    renderer. The fallback is the only place the renderer tolerates
    drift; everything else is data-in / data-out. An unknown lookup
    also fires a one-line stderr warning so a typo in a caller's
    status string is visible during development instead of being
    silently absorbed — see the module-level ``_STATUS_GLYPHS``
    comment for the soft-validation rationale.
    """
    glyph = _STATUS_GLYPHS.get(status)
    if glyph is None:
        sys.stderr.write(
            f"_render: warning: status {status!r} not in _STATUS_GLYPHS; "
            "falling back to 'unknown'. Add the new status word to the "
            "table if it is a permanent vocabulary extension.\n"
        )
        return _STATUS_GLYPHS["unknown"]
    return glyph


def portable_project_repr(project_root: Path, *, base: Path | None = None) -> str:
    """Render ``project_root`` in the most-portable form for the table.

    Mirrors :func:`ctxr.fsm.cli.install_mcp_cmd._portable_repr` so the
    project row never bakes a machine-specific absolute path into
    screen text either. Resolution order:

    1. Relative-to-``base`` (default cwd) — printed as ``./<rel>`` so
       a copy/paste from the terminal lands as a usable path. The
       cwd-itself case collapses to a bare ``.`` for the same reason.
    2. ``~``-prefixed when ``project_root`` lives under ``$HOME``.
    3. Absolute path — the user explicitly pointed at a project
       outside both cwd and ``$HOME``; we surface that honestly.
    """
    base = base or Path.cwd()
    try:
        rel = project_root.relative_to(base)
    except ValueError:
        pass
    else:
        rel_str = str(rel)
        return "." if rel_str == "." else f"./{rel_str}"
    home = Path.home()
    try:
        rel_home = project_root.relative_to(home)
    except ValueError:
        return str(project_root)
    # ``project_root == home`` collapses ``relative_to(home)`` to
    # ``Path(".")``; render that as a bare ``~`` instead of ``~/.``
    # so the cell stays idiomatic (matching the cwd branch's bare
    # ``.`` collapse above).
    rel_home_str = str(rel_home)
    return "~" if rel_home_str == "." else "~/" + rel_home_str


def render_subsystem_table(
    active_mcp: dict[str, Any],
    *,
    project_root: Path,
    title: str | None = None,
) -> Table:
    """Build a Rich :class:`Table` from the active-mcp.json payload.

    Input ``active_mcp`` shape (W14c discovery document)::

        {
          "started_at": "...",
          "supervisor_pid": int,
          "version": str,
          "subsystems": {
            "mcp": {"http_url": str, "healthz_url": str, "pid": int,
                    "status": str (optional)},
            "api": {"http_url": str, "healthz_url": str, "pid": int,
                    "docs_url": str (optional), "status": str (optional)},
            "ui":  {"http_url": str, "healthz_url": str | None,
                    "pid": int, "status": str (optional)},
          }
        }

    Columns (locked order, W14j.a):

    * **Subsystem** — ``Project`` for the leading row, then
      ``mcp`` / ``api`` / ``ui`` in that fixed order.
    * **URL** — ``http_url`` from the payload; verbatim so the
      operator can copy-paste straight into a browser or curl.
    * **Swagger** — ``docs_url`` if the payload carries one; otherwise
      derived as ``http_url + /docs`` for the api row only. Blank for
      other rows.
    * **Health** — coloured + labelled per :data:`_STATUS_GLYPHS`.
    * **PID** — right-aligned integer; ``-`` when the payload omits it
      (the supervisor file has not been rewritten yet on a reload).

    The leading **Project** row carries the portable project-root path
    (relative-to-cwd, ``~``-prefixed, or absolute fallback) so an
    operator juggling multiple checkouts can confirm which one this
    table describes without scrolling back through their shell.
    Project / blank / blank / blank / blank — the row is structural,
    not a subsystem.
    """
    table = Table(
        title=title or "ctxr-fsm subsystems",
        show_lines=False,
        title_style="bold",
        header_style="bold cyan",
        # NOT ``expand=True``: that would split available width across
        # ratio'd columns and force URL truncation when the terminal
        # is narrow. Default sizing (auto-fit-to-content) instead
        # claims exactly the width each URL needs; if the table
        # overflows the terminal, the terminal handles the horizontal
        # overflow (or wraps the WHOLE table line — readable OSC 8
        # links survive a terminal-level reflow because the markup
        # already encodes the click target before any visual wrap).
    )
    # URL + Swagger cells are wrapped in Rich ``[link=…]`` markup below
    # so terminals that support OSC 8 hyperlinks (iTerm2, macOS
    # Terminal.app on Sequoia+, VS Code integrated terminal, Wezterm,
    # Kitty, Alacritty with the right config, modern GNOME Terminal +
    # Konsole) render the visible URL as CLICKABLE — Cmd-click /
    # Ctrl-click opens the browser straight from the table. Setting
    # ``no_wrap=True`` keeps the visible URL on a single line so the
    # operator can read AND click it; if the column overflows the
    # terminal width the column expands rather than folding into a
    # broken two-line URL that no copy-paste survives.
    # The table no longer carries the URLs themselves — Rich's
    # column-fit math will TRUNCATE URLs with ``…`` on any terminal
    # narrower than the natural table+URL width, which is exactly the
    # operator pain that motivated W16 in the first place ("the dots
    # disarm all the point of having this info!!! how can i follow
    # these links????"). Instead the table is the at-a-glance status
    # view (which subsystem, healthz status, pid) and the URLs are
    # printed BELOW the table as plain lines wrapped in OSC 8
    # hyperlink escapes — one URL per line, ALWAYS full, ALWAYS
    # clickable on supporting terminals (iTerm2, modern macOS
    # Terminal.app, VSCode terminal, Wezterm, Kitty).
    table.add_column("Subsystem", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("PID", justify="right", no_wrap=True)

    # Leading project row — structural context, not a subsystem.
    table.add_row(
        "Project",
        portable_project_repr(project_root),
        "",
        style="dim",
    )

    subsystems_raw = active_mcp.get("subsystems") or {}
    subsystems = subsystems_raw if isinstance(subsystems_raw, dict) else {}

    # Fixed render order matches the supervisor's own boot order +
    # the documented ``mcp / api / ui`` triple. We never iterate the
    # dict directly: that would let a payload-mutation accidentally
    # change column order between commands.
    for name in ("mcp", "api", "ui"):
        sub = subsystems.get(name)
        if not isinstance(sub, dict):
            continue
        status_value = sub.get("status", "unknown")
        status_text, status_style = _format_status(
            status_value if isinstance(status_value, str) else "unknown"
        )
        pid = sub.get("pid")
        pid_repr = str(pid) if isinstance(pid, int) else "-"

        table.add_row(
            name,
            f"[{status_style}]{status_text}[/{status_style}]",
            pid_repr,
        )

    return table


def render_subsystem_urls(active_mcp: dict[str, Any]) -> list[str]:
    """Build clickable-URL lines for the post-table "open this" block.

    Each line carries an OSC 8 hyperlink (via Rich ``[link=…]`` markup
    expanded at render time) wrapping the URL itself, so the visible
    text and the click target are byte-identical AND the link survives
    terminals that strip OSC 8 (the user sees the URL anyway). Order
    matches the table: MCP first (the bootstrap entry point), then API
    + the Swagger doc URL, then UI.

    Returns a list of Rich markup strings; the caller prints each line.
    Empty list when no subsystem reported a URL (e.g. ensure failed).
    """
    subsystems_raw = active_mcp.get("subsystems") or {}
    subsystems = subsystems_raw if isinstance(subsystems_raw, dict) else {}

    lines: list[str] = []

    def _link_line(label: str, url: str) -> str:
        """One ``label  link`` line. Label padded to a constant width
        so a column of multiple labels visually aligns."""
        return f"  [bold cyan]{label:<10}[/bold cyan] [link={url}]{url}[/link]"

    for name in ("mcp", "api", "ui"):
        sub = subsystems.get(name)
        if not isinstance(sub, dict):
            continue
        http_url = sub.get("http_url")
        if isinstance(http_url, str) and http_url:
            lines.append(_link_line(name, http_url))
        # Swagger lands on its own line right after the api row so
        # operators looking for "the docs URL" find it adjacent in
        # the visual flow.
        if name == "api":
            docs_url_raw = sub.get("docs_url")
            if isinstance(docs_url_raw, str) and docs_url_raw:
                lines.append(_link_line("swagger", docs_url_raw))
            elif isinstance(http_url, str) and http_url:
                lines.append(
                    _link_line("swagger", http_url.rstrip("/") + "/docs")
                )

    return lines


def print_subsystem_table(
    active_mcp: dict[str, Any],
    *,
    project_root: Path,
    title: str | None = None,
    console: Console | None = None,
) -> None:
    """Render and print the subsystem table to ``console`` (or a fresh one).

    Shorthand wrapper so call sites do not have to import :class:`Console`
    themselves. Tests that need deterministic capture should pass their
    own ``Console(file=StringIO(), force_terminal=True, width=120)``
    instance and skip this helper.
    """
    console = console or Console()
    console.print(
        render_subsystem_table(active_mcp, project_root=project_root, title=title)
    )
    url_lines = render_subsystem_urls(active_mcp)
    if url_lines:
        # Single-line header + the link block. The header is short
        # enough to live above the URLs without crowding the table.
        console.print("\n[bold]Open in your browser:[/bold]")
        for line in url_lines:
            console.print(line)
