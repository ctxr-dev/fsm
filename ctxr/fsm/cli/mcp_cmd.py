"""``ctxr-fsm mcp`` — stub for the Model Context Protocol server.

W3 placeholder for what W4 fills in. We accept the eventual transport
flag (``--transport stdio|http``) today so any operator scripts that
pin the invocation continue to work unchanged when the real server
lands; validation is identical to what W4 will perform, so a value
accepted today is guaranteed to be accepted tomorrow.

See ``ctxr/fsm/cli/serve_cmd.py`` for the broader rationale on shipping
command shapes ahead of their implementations.
"""

from __future__ import annotations

import typer

__all__ = ["mcp"]


# Allowed values for the ``--transport`` flag. Pinned as a tuple of
# literals so the W4 implementer can grep one place to widen the set
# (e.g. adding ``sse`` or ``websocket``) without hunting for stringly-
# typed checks.
_VALID_TRANSPORTS: tuple[str, ...] = ("stdio", "http")


def mcp(
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help=(
            "MCP transport: 'stdio' (the canonical client-launched "
            "stdio framing) or 'http' (long-poll HTTP). The full server "
            "ships in W4; today this flag is validated but otherwise unused."
        ),
    ),
) -> None:
    """Stub for the W4 MCP server.

    Validates ``--transport`` against the documented set, then emits
    the structured deferral message on stderr and exits with code ``1``.
    """
    if transport not in _VALID_TRANSPORTS:
        raise typer.BadParameter(
            f"--transport must be one of {_VALID_TRANSPORTS!r} "
            f"(got {transport!r})"
        )

    typer.echo("MCP server lands in W4. Stub.", err=True)
    raise typer.Exit(1)
