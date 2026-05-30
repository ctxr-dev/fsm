"""``ctxr-fsm api`` — stub for the FastAPI HTTP / SSE server.

W3 placeholder for the W5 FastAPI surface. ``--host`` and ``--port`` are
accepted today (with validation matching what W5 will perform) so any
operator scripts or systemd units that pin the invocation continue to
work unchanged once the real server ships.

See ``ctxr/fsm/cli/serve_cmd.py`` for the broader rationale on shipping
command shapes ahead of their implementations.
"""

from __future__ import annotations

import typer

__all__ = ["api"]


def api(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help=(
            "Bind address for the FastAPI server. Defaults to localhost "
            "so the dev workflow is safe-by-default; production deployments "
            "should set this explicitly. Effective when W5 lands."
        ),
    ),
    port: int = typer.Option(
        8080,
        "--port",
        min=1,
        max=65535,
        help=(
            "TCP port for the FastAPI server (1-65535). "
            "Effective when W5 lands."
        ),
    ),
) -> None:
    """Stub for the W5 FastAPI server.

    Emits the structured deferral message on stderr and exits with code
    ``1``. The ``--port`` range is enforced today so a typo (e.g.
    ``--port 808080``) fails the same way it will when the body lands.
    """
    # ``host`` is accepted as free-form text — we deliberately do NOT
    # validate it as an IP / hostname here because the W5 server will
    # ultimately delegate to uvicorn which accepts both. Stricter
    # checking belongs in the body, not the stub.
    _ = host

    typer.echo("FastAPI server lands in W5. Stub.", err=True)
    raise typer.Exit(1)
