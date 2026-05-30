"""``ctxr-fsm serve`` — stub for the long-running service supervisor.

This module is the W3 placeholder for what W7 will turn into the real
orchestrator-supervisor. We ship the *command shape* now — name,
options, exit semantics — so any scripts, documentation, or operator
muscle memory built around ``ctxr-fsm serve`` keeps working unchanged
once the implementation lands.

Why ship the stub at all
------------------------

Two reasons:

1. The brief explicitly lists ``serve`` as part of the W3 CLI surface,
   so ``ctxr-fsm --help`` must already mention it. Hiding the command
   until W7 would create a confusing "where did serve go?" gap in the
   docs.
2. By emitting a structured "not yet implemented; coming in W7" message
   and exiting non-zero, any wrapping shell script that pipes through
   ``set -e`` fails loudly today rather than silently no-op'ing — the
   loudest failure mode is the friendliest one when capabilities slide
   between workstreams.

The actual options (e.g. ``--mode dev|prod``) are accepted today so
operator scripts and CI workflows can already pin the invocation; we
re-validate them when W7 fills in the body so a future change does
not silently accept previously-rejected values.
"""

from __future__ import annotations

import typer

__all__ = ["serve"]


# Allowed values for the ``--mode`` flag. The set is small and stable
# enough that an Enum would be overkill; a tuple of literals keeps the
# validation one ``in`` check away while making the W7 implementer's job
# obvious — these two strings are the contract.
_VALID_MODES: tuple[str, ...] = ("dev", "prod")


def serve(
    mode: str = typer.Option(
        "dev",
        "--mode",
        help=(
            "Supervisor mode: 'dev' (foreground, verbose, auto-reload) "
            "or 'prod' (daemonised, structured logs). The full semantics "
            "land in W7; today this flag is validated but otherwise unused."
        ),
    ),
) -> None:
    """Stub for the W7 supervisor.

    Emits the structured deferral message on stderr and exits with code
    ``1`` so callers that pipe through ``set -e`` fail loudly rather
    than silently doing nothing.

    Raises :class:`typer.BadParameter` for ``--mode`` values outside the
    documented set so we never accept input today that the future
    implementation would reject — a slow-burn API break is exactly the
    kind of thing the stub is here to prevent.
    """
    if mode not in _VALID_MODES:
        # ``typer.BadParameter`` produces the standard Typer / Click
        # error rendering and the right exit code (2) so the user sees
        # the same UX they would get from any other validation failure.
        raise typer.BadParameter(
            f"--mode must be one of {_VALID_MODES!r} (got {mode!r})"
        )

    # The exact wording matches the brief verbatim so any doc / runbook
    # that quotes this string keeps matching. Writing to stderr (the
    # default for ``typer.echo(err=True)``) keeps stdout clean for
    # scripts that pipe ``ctxr-fsm`` output into ``jq`` and friends.
    typer.echo(
        "Service supervisor lands in W7 (lifecycle). Use `ctxr-fsm mcp` "
        "/ `ctxr-fsm api` / `ctxr-fsm ui` per-process subcommands for "
        "now (also stubs).",
        err=True,
    )
    raise typer.Exit(1)
