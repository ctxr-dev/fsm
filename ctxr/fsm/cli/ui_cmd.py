"""``ctxr-fsm ui`` — boot the Vite dev server for the W6 UI surface.

This is the W6 implementation that replaces the W3 stub. The command
is a thin shim around ``npm install`` (lazy, one-shot) and ``npm run
dev``: it shells out to the Node toolchain inside ``fsm/ui/`` so the
TypeScript / Preact / Vite stack stays a self-contained subproject
with its own ``package.json`` and dependency graph.

Why a Python shim at all
------------------------

``ctxr-fsm`` is the single operator entry point. Operators should not
have to remember "for the API run ``ctxr-fsm api``, but for the UI
``cd ui && npm run dev``" — the friction of two different invocation
styles invites mistakes. The shim normalises the surface so
``ctxr-fsm ui`` mirrors ``ctxr-fsm api``: one command, one set of
options, one process to ``Ctrl-C``.

Option semantics
----------------

* ``--port`` — TCP port for the Vite dev server. Defaults to Vite's
  own canonical ``5173`` so muscle memory and docs links keep working.
  Forwarded to ``npm run dev`` via ``-- --port <port>`` (the leading
  ``--`` separator tells npm to hand the rest of the argv to the
  underlying ``vite`` binary).
* ``--api-port`` — TCP port where the W5 ``ctxr-fsm api`` server is
  listening. Defaults to ``8765`` (the conventional dev port used
  throughout the W5 docs and the Vite proxy config). Exported as
  ``VITE_API_PORT`` in the child process environment; the Vite config
  reads it at startup and rewires the ``/api/v1`` proxy target.
* ``--no-install`` — skip the "install on first run" leg. Useful when
  the operator manages dependencies out-of-band (CI caches, monorepo
  hoists) or when the auto-install would mask a deliberate broken
  state during debugging.

Subprocess shape
----------------

Both ``npm`` invocations inherit the parent process's stdio so the
operator sees Vite's banner, file-change logs, and error traces
exactly as they would running ``npm run dev`` directly. We do NOT
capture output — capturing would defeat the live-reload workflow
this command exists to support. ``Ctrl-C`` propagates as SIGINT to
the child via the inherited foreground process group.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer

__all__ = ["ui"]


# Resolved once at import time so the path is stable regardless of
# the operator's CWD. ``parents[3]`` walks
# ``cli/ui_cmd.py`` -> ``cli`` -> ``fsm`` -> ``ctxr`` -> repo root,
# then we append ``ui`` to land on the Vite subproject.
_UI_DIR: Path = Path(__file__).resolve().parents[3] / "ui"


def ui(
    port: int = typer.Option(
        5173,
        "--port",
        min=1,
        max=65535,
        help=(
            "TCP port the Vite dev server will bind to (1-65535). "
            "Defaults to Vite's canonical 5173 so muscle memory and "
            "docs links keep working."
        ),
    ),
    api_port: int = typer.Option(
        8765,
        "--api-port",
        min=1,
        max=65535,
        help=(
            "TCP port where the `ctxr-fsm api` server is listening "
            "(1-65535). Exported as VITE_API_PORT so the Vite config "
            "can rewire the /api/v1 proxy target. Defaults to 8765 "
            "(the conventional dev port used in the W5 docs)."
        ),
    ),
    no_install: bool = typer.Option(
        False,
        "--no-install",
        help=(
            "Skip the auto `npm install` that runs on first invocation "
            "when ui/node_modules is missing. Useful when dependencies "
            "are managed out-of-band (CI caches, monorepo hoists) or "
            "when you want a deliberate broken state preserved for "
            "debugging."
        ),
    ),
) -> None:
    """Run the Vite dev server for the ctxr-fsm UI subproject.

    Blocks until the Vite process exits (typically via ``Ctrl-C``).
    Stdio is inherited so the operator sees the live dev-server
    output exactly as if they had run ``npm run dev`` in ``fsm/ui/``
    directly.
    """
    # Lazy dependency install. We check for ``node_modules`` rather
    # than running ``npm install`` unconditionally so the steady-state
    # ``ctxr-fsm ui`` invocation is a fast no-op on the install leg
    # and a single ``npm run dev`` call on the run leg. ``npm install``
    # itself is idempotent but takes seconds even when there is
    # nothing to do — not acceptable for an interactive dev command.
    if not no_install and not (_UI_DIR / "node_modules").exists():
        typer.echo(
            f"ui/node_modules missing — running `npm install` in {_UI_DIR}",
            err=True,
        )
        install_result = subprocess.run(
            ["npm", "install"],
            cwd=str(_UI_DIR),
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False,
        )
        if install_result.returncode != 0:
            # Propagate the npm exit code so wrapping scripts that
            # pipe through ``set -e`` see the failure rather than
            # silently moving on to the (broken) dev-server boot.
            raise typer.Exit(install_result.returncode)

    # Forward the chosen port to the Vite binary. ``npm run <script>
    # -- <args>`` is the canonical way to pass through to the
    # underlying command — npm consumes everything up to the ``--``
    # and hands the rest to the script.
    cmd = ["npm", "run", "dev", "--", "--port", str(port)]

    # Child env = parent env + VITE_API_PORT. We copy rather than
    # mutate ``os.environ`` so the parent process's environment stays
    # untouched (matters for embedded use and for any future code
    # that runs after ``ui`` returns in the same interpreter).
    child_env = os.environ.copy()
    child_env["VITE_API_PORT"] = str(api_port)

    dev_result = subprocess.run(
        cmd,
        cwd=str(_UI_DIR),
        env=child_env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        check=False,
    )
    # Mirror the child's exit code so ``ctxr-fsm ui`` is a faithful
    # proxy for ``npm run dev`` from the caller's perspective.
    if dev_result.returncode != 0:
        raise typer.Exit(dev_result.returncode)
