"""Shared helpers for the ``ctxr-fsm`` CLI.

Centralises four concerns that would otherwise be duplicated across
every command module:

1. **Database-path resolution** — ``resolve_db_path`` implements the
   layered precedence ``--db`` > ``$CTXR_FSM_DB`` >
   ``$(pwd)/.ctxr-fsm/fsm.db`` so every subcommand can accept a single
   ``DB_OPTION`` parameter and call one helper to materialise the
   ``Path``. The env-var path is also exposed via Typer's ``envvar=``
   binding on :data:`DB_OPTION` so ``--help`` documents it without us
   having to repeat the precedence note in each command's docstring.

2. **Project opening** — ``open_project_for_cli`` wraps
   :meth:`Project.open` with the defaults the CLI always wants:
   ``migrate=True`` so an out-of-date DB is upgraded transparently, and
   ``echo=False`` so we never spam stdout with SQL. The returned object
   is a context-manager because :class:`Project` implements ``__enter__``
   / ``__exit__``, so callers can simply write
   ``with open_project_for_cli(db) as proj: ...``.

3. **Pretty / JSON output** — ``json_or_pretty`` is the single place
   that knows the ``--json`` flag's contract. JSON output goes verbatim
   to stdout via :func:`json.dumps` (sorted keys, two-space indent so
   the output is diff-friendly when piped to ``less``); pretty output
   goes through :func:`rich.print` which gives us colours and table
   rendering for free.

4. **Failure exit** — ``die`` prints to stderr and raises
   :class:`typer.Exit` with the supplied non-zero code. We never
   ``sys.exit`` directly because Typer swallows the resulting
   :class:`SystemExit` cleanly and produces the right framework banner.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich import print as rich_print

from ctxr.fsm import FsmSpec
from ctxr.fsm.sqlite import Project

__all__ = [
    "DB_OPTION",
    "JSON_OPTION",
    "die",
    "json_or_pretty",
    "load_spec",
    "open_project_for_cli",
    "resolve_db_path",
]


# Default location for the project's SQLite database. We keep the
# ``.ctxr-fsm`` folder per-project (alongside the source tree) rather
# than in ``$XDG_DATA_HOME`` so the database is naturally tied to the
# project's lifetime — ``rm -rf .ctxr-fsm`` is the project's reset.
_DEFAULT_DB_RELATIVE: Path = Path(".ctxr-fsm") / "fsm.db"

# The environment variable name the CLI honours. Kept as a module
# constant rather than a literal so the test suite can ``monkeypatch``
# the same symbol the production code reads.
_ENV_VAR_NAME: str = "CTXR_FSM_DB"


def resolve_db_path(db_opt: Path | None) -> Path:
    """Apply the CLI's layered precedence to produce a concrete DB path.

    Precedence (highest first):

    1. ``--db`` / ``-d`` (the ``db_opt`` argument).
    2. ``$CTXR_FSM_DB`` (read live from ``os.environ`` so the lookup
       reflects any test ``monkeypatch.setenv`` call).
    3. ``$(pwd) / .ctxr-fsm / fsm.db`` — the canonical project-local
       location, matching the directory that ``ctxr-fsm init`` creates.

    The returned path is always absolute (resolved against the current
    working directory) so downstream code can compare and log paths
    without worrying about ambiguous relative segments.
    """
    if db_opt is not None:
        return db_opt.expanduser().resolve()
    env_value = os.environ.get(_ENV_VAR_NAME)
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (Path.cwd() / _DEFAULT_DB_RELATIVE).resolve()


def open_project_for_cli(db: Path) -> Project:
    """Open a :class:`Project` ready for a CLI command.

    Runs ``alembic upgrade head`` (via ``migrate=True``) so a CLI
    invocation never crashes with a "schema older than code" error;
    the migration is idempotent so the cost when the DB is already
    current is one ``SELECT version_num FROM alembic_version``.

    The caller is expected to use the result as a context manager so
    the engine is disposed deterministically when the command exits.
    """
    return Project.open(db, migrate=True, echo=False)


def json_or_pretty(payload: Any, json_mode: bool) -> None:
    """Print ``payload`` in either machine-readable or human-friendly form.

    ``json_mode=True`` calls :func:`json.dumps` with ``sort_keys=True``
    and ``indent=2`` so the output is stable across runs and
    diff-friendly; ``json_mode=False`` delegates to :func:`rich.print`
    which renders dicts/lists with colour and indentation.
    """
    if json_mode:
        sys.stdout.write(json.dumps(payload, sort_keys=True, indent=2, default=str))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return
    rich_print(payload)


def load_spec(import_path: str) -> FsmSpec:
    """Load an :class:`FsmSpec` from a ``module:attribute`` import path.

    The import path follows the same ``<module>:<attribute>`` shape used
    by other Python ecosystem tools (e.g. ``uvicorn``, ``gunicorn``,
    ``flask``). ``module`` is anything :func:`importlib.import_module`
    can resolve; ``attribute`` is the name of an :class:`FsmSpec` bound
    at module top level (commonly ``spec``).

    The current working directory is prepended to ``sys.path`` so users
    can run ``ctxr-fsm spec validate examples.foo:spec`` from the root
    of a project without having installed it as a package. We tolerate
    a duplicate insertion (``sys.path.insert`` is cheap and the resulting
    duplicate is harmless) rather than guarding so the helper stays
    stateless and re-entrant.

    Raises :class:`typer.BadParameter` for any of:

    * ``import_path`` missing the ``:`` separator.
    * The module failing to import (re-raised with a friendly hint).
    * The attribute being absent on the module.
    * The attribute not being an :class:`FsmSpec` instance.
    """
    if ":" not in import_path:
        raise typer.BadParameter(
            "spec-import-path must be <module>:<attribute> "
            "(e.g. 'examples.plan_implement_qa_fix:spec')"
        )
    module_path, attr_name = import_path.split(":", 1)
    if not module_path or not attr_name:
        raise typer.BadParameter(
            "spec-import-path must be <module>:<attribute> "
            "(both sides of the ':' are required)"
        )

    # Make the user's invocation directory importable so callers can
    # pass top-level fixture modules like ``fixture_spec:spec`` or
    # package-relative paths like ``examples.plan:spec`` without a
    # prior install. We prefer ``$PWD`` over ``os.getcwd()`` because
    # tools like ``uv run --directory <pkg>`` chdir into the package
    # before exec'ing the script, which would otherwise hide the
    # directory the user was actually standing in. The Unix shell sets
    # ``PWD`` to the *logical* cwd at invocation time, so it stays
    # truthful through that style of launcher. We insert at index 0 so
    # the user's own modules shadow any same-named installed package.
    invocation_dir = os.environ.get("PWD") or os.getcwd()
    if invocation_dir not in sys.path:
        sys.path.insert(0, invocation_dir)

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise typer.BadParameter(
            f"could not import module {module_path!r}: {exc}"
        ) from exc

    try:
        attr = getattr(module, attr_name)
    except AttributeError as exc:
        raise typer.BadParameter(
            f"module {module_path!r} has no attribute {attr_name!r}"
        ) from exc

    if not isinstance(attr, FsmSpec):
        raise typer.BadParameter(
            f"{import_path} is not an FsmSpec instance (got {type(attr).__name__})"
        )
    return attr


def die(message: str, code: int = 1) -> NoReturn:
    """Print ``message`` to stderr and abort the CLI with ``code``.

    We always go through :class:`typer.Exit` rather than
    :func:`sys.exit` so Typer's outer handler can do its own bookkeeping
    (eg. closing the Click context) before the process terminates.
    """
    sys.stderr.write(f"error: {message}\n")
    sys.stderr.flush()
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# Reusable Typer Option definitions
# ---------------------------------------------------------------------------

# ``DB_OPTION`` is the standard ``--db`` / ``-d`` parameter every
# subcommand accepts. Typer reads ``envvar=`` automatically so callers
# can also set ``CTXR_FSM_DB`` in their shell environment — the layered
# precedence in :func:`resolve_db_path` matches that contract.
DB_OPTION: Any = typer.Option(
    None,
    "--db",
    "-d",
    help=(
        "Path to project SQLite database (default: $CTXR_FSM_DB, "
        "otherwise ./.ctxr-fsm/fsm.db)."
    ),
    envvar=_ENV_VAR_NAME,
)


# ``JSON_OPTION`` toggles machine-readable output. Defaulting to False
# keeps the CLI friendly for humans while still giving scripts a clean
# integration point.
JSON_OPTION: Any = typer.Option(
    False,
    "--json",
    help="Emit machine-readable JSON instead of pretty-printed output.",
)
