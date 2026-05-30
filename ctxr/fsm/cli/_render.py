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

from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

__all__ = [
    "print_subsystem_table",
    "render_subsystem_table",
]


# Status display + Rich style mapping. Closed vocabulary, hosted here
# so call sites never reach for ad-hoc colour strings. New subsystem
# statuses must be added here (and only here) — the renderer falls
# back to ``unknown`` for anything outside the table so a typo in a
# caller surfaces visibly rather than silently.
#
# We deliberately do NOT introduce a ``Literal[...]`` typing on the
# input ``status`` field: the renderer accepts whatever the per-call
# site supplies (``EnsureActionStatus`` values, the supervisor's
# ``"ready"`` literal, the doctor's ``"unreachable"``) and falls back
# to ``unknown`` instead of raising. Keeping the surface forgiving
# means a future caller can add a new status word without breaking
# the renderer, and the per-call site keeps owning its own enum.
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
    drift; everything else is data-in / data-out.
    """
    return _STATUS_GLYPHS.get(status, _STATUS_GLYPHS["unknown"])


def _portable_project_repr(project_root: Path, *, base: Path | None = None) -> str:
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
        return "~/" + str(project_root.relative_to(home))
    except ValueError:
        return str(project_root)


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
    )
    table.add_column("Subsystem", style="bold")
    table.add_column("URL", overflow="fold")
    table.add_column("Swagger", overflow="fold")
    table.add_column("Health")
    table.add_column("PID", justify="right")

    # Leading project row — structural context, not a subsystem.
    table.add_row(
        "Project",
        _portable_project_repr(project_root),
        "",
        "",
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
        http_url = sub.get("http_url") or ""
        # Swagger column: explicit ``docs_url`` wins; otherwise we
        # derive ``<http_url>/docs`` for the api row only. Other rows
        # stay blank.
        docs_url_raw = sub.get("docs_url")
        if isinstance(docs_url_raw, str) and docs_url_raw:
            swagger = docs_url_raw
        elif name == "api" and http_url:
            swagger = http_url.rstrip("/") + "/docs"
        else:
            swagger = ""
        status_value = sub.get("status", "unknown")
        status_text, status_style = _format_status(
            status_value if isinstance(status_value, str) else "unknown"
        )
        pid = sub.get("pid")
        pid_repr = str(pid) if isinstance(pid, int) else "-"

        table.add_row(
            name,
            http_url,
            swagger,
            f"[{status_style}]{status_text}[/{status_style}]",
            pid_repr,
        )

    return table


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
