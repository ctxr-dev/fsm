"""``ctxr-fsm serve`` — unified service supervisor (W7).

This module is the public Typer shim for the long-running development
trio (MCP, API, UI) that lives behind ``ctxr-fsm serve``. The heavy
lifting — task-group orchestration, child-process spawn + drain,
watchfiles reload loop, signal translation — is implemented in
:mod:`ctxr.fsm.cli.lifecycle.supervisor`. This file's job is to expose
the operator-facing surface (the command name and its options) and
hand control to the supervisor's :func:`main` entry point.

Design notes
------------

* **Surface stability.** The W3 stub already shipped ``--mode dev|prod``
  so operator scripts and CI workflows could pin against it. We preserve
  the same flag shape (name, allowed values, default) here so anything
  pinned against the stub keeps working unchanged once W7 lands.

* **``--db`` parity.** Every other ``ctxr-fsm`` subcommand accepts a
  ``--db`` option via :data:`DB_OPTION`; ``serve`` exposes it too so the
  supervisor can pass the same path down to the MCP and API children
  (their own ``--db`` resolution is the existing layered precedence
  ``--db`` > ``$CTXR_FSM_DB`` > project default).

* **Thin shim.** The body intentionally does no orchestration of its
  own — it validates the small things Typer can't (``--mode`` allowlist
  re-check, defensive) and immediately delegates to
  :func:`supervisor.main`. Keeping the shim thin means the supervisor
  module owns the lifecycle contract end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ctxr.fsm.cli._common import DB_OPTION
from ctxr.fsm.cli.lifecycle.supervisor import main

__all__ = ["serve"]


# Allowed values for the ``--mode`` flag. The set is small and stable
# enough that an Enum would be overkill; a tuple of literals keeps the
# validation one ``in`` check away while making the supervisor's
# Literal annotation visibly the same contract.  # audit-strings: justified
_VALID_MODES: tuple[str, ...] = ("dev", "prod")


def serve(
    db: Path | None = DB_OPTION,
    mode: str = typer.Option(
        "dev",
        "--mode",
        help=(
            "Supervisor mode: 'dev' (foreground, verbose, watchfiles "
            "reload on source change, UI dev server included) or 'prod' "
            "(no reload, no UI; MCP + API only). The supervisor stays in "
            "the foreground in both modes — daemonisation is the "
            "operator's job (systemd, nohup, container PID 1)."
        ),
    ),
    mcp_only: bool = typer.Option(
        False,
        "--mcp-only",
        help=(
            "Boot ONLY the MCP child; skip the API and UI subsystems. "
            "Useful for headless CI and for ``ctxr-fsm ensure --mode "
            "mcp-only``. The active-mcp.json discovery file still lands "
            "with only the mcp block populated."
        ),
    ),
) -> None:
    """Run the unified ``ctxr-fsm serve`` supervisor.

    Boots the MCP server (HTTP transport), the FastAPI server, and (in
    ``dev`` mode) the Vite UI dev server as a single supervised process
    tree. SIGINT or SIGTERM drains the children with a 5s budget per
    child, escalating to ``kill()`` for anything that overruns.

    Raises :class:`typer.BadParameter` for ``--mode`` values outside the
    documented set — the same guard the W3 stub carried, kept in place
    so a typo trips Typer's standard usage-error path (exit code 2)
    rather than reaching the supervisor with an invalid literal.
    """
    if mode not in _VALID_MODES:
        # ``typer.BadParameter`` produces the standard Typer / Click
        # error rendering and the right exit code (2) so the user sees
        # the same UX they would get from any other validation failure.
        raise typer.BadParameter(
            f"--mode must be one of {_VALID_MODES!r} (got {mode!r})"
        )

    # Delegate to the supervisor's synchronous entry point. ``main``
    # wraps :func:`anyio.run` around the async ``run_supervisor`` body
    # and handles the SIGINT/SIGTERM translation internally, so this
    # call only returns once every child has been drained.
    main(mode=mode, db_path=db, mcp_only=mcp_only)
