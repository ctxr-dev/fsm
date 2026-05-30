"""``ctxr-fsm ui`` — stub for the Vite UI dev server launcher.

W3 placeholder for the W6 UI surface. ``--port`` is accepted today with
the same validation the real launcher will perform, so any documentation
or shell wrappers that pin the invocation continue to work unchanged
once the body lands.

See ``ctxr/fsm/cli/serve_cmd.py`` for the broader rationale on shipping
command shapes ahead of their implementations.
"""

from __future__ import annotations

import typer

__all__ = ["ui"]


def ui(
    port: int = typer.Option(
        5173,
        "--port",
        min=1,
        max=65535,
        help=(
            "TCP port the Vite dev server will bind to (1-65535). "
            "Defaults to Vite's own 5173. Effective when W6 lands."
        ),
    ),
) -> None:
    """Stub for the W6 Vite UI launcher.

    Emits the structured deferral message on stderr and exits with code
    ``1``. The ``--port`` range is enforced today so a typo (e.g.
    ``--port 51730000``) fails the same way it will when the body lands.
    """
    # ``port`` is validated by Typer's ``min``/``max`` bounds above; we
    # accept it here purely so the eventual W6 invocation does not break
    # any scripts written against today's stub.
    _ = port

    typer.echo("Vite UI dev server lands in W6. Stub.", err=True)
    raise typer.Exit(1)
