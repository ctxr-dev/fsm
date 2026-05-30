"""``ctxr-fsm init`` — bootstrap a project for FSM use.

Side effects this command intentionally performs (in order):

1. Creates ``./.ctxr-fsm/`` and ``./.ctxr-fsm/pids/``. The pids
   subdirectory is reserved for future MCP / API server bookkeeping
   (W4+); we provision it now so subsequent components do not have to
   worry about creating it themselves.
2. Runs ``alembic upgrade head`` against the resolved DB path. This
   leaves the database fully migrated even on first init — we always
   call :func:`run_migrations` directly (never ``subprocess(alembic)``)
   so the command works without an external alembic binary on PATH.
3. If the current working directory is a git checkout, appends
   ``.ctxr-fsm/`` to ``.gitignore`` — idempotent: we read the existing
   ignore file and only append the entry when it is genuinely missing.
   The file is created if absent.
4. Emits a friendly summary (DB path, alembic revision, follow-up
   memory-installer note) via :func:`json_or_pretty` so ``--json`` is
   honoured for scripting.

The ``--no-memory`` flag is recognised today (so the contract is
forward-compatible) but the actual memory installer ships in W11; for
now we simply note the deferral in the summary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from sqlalchemy import text

from ctxr.fsm.cli._common import (
    DB_OPTION,
    JSON_OPTION,
    json_or_pretty,
    resolve_db_path,
)
from ctxr.fsm.sqlite.project import run_migrations

__all__ = ["init"]


_GITIGNORE_ENTRY: str = ".ctxr-fsm/"


def _ensure_gitignore_entry(repo_root: Path) -> bool:
    """Append ``.ctxr-fsm/`` to ``repo_root/.gitignore`` if missing.

    Returns ``True`` if an append happened, ``False`` if the entry was
    already present (or the file did not need touching for any other
    reason). We tolerate both LF- and CRLF-terminated lines because
    Windows-checkout collaborators sometimes commit ``.gitignore``
    with CRLF endings.
    """
    gitignore = repo_root / ".gitignore"
    existing_lines: list[str] = []
    if gitignore.exists():
        # Read as text with the universal-newlines default so a CRLF
        # file does not produce phantom trailing ``\r`` characters in
        # the stripped comparison below.
        existing_lines = gitignore.read_text(encoding="utf-8").splitlines()
    # Idempotent: if any non-blank, non-comment line equals our entry
    # (after stripping), we have nothing to do. We intentionally do
    # NOT try to match wildcard patterns like ``.ctxr-fsm`` (no
    # trailing slash) — being strict means re-running init is safe
    # and predictable.
    for line in existing_lines:
        if line.strip() == _GITIGNORE_ENTRY:
            return False
    # Preserve the existing trailing newline contract: if the file is
    # non-empty and doesn't end with a newline, add one before our
    # appended line so we don't produce "lastEntry.ctxr-fsm/".
    prefix = ""
    if existing_lines:
        existing_text = gitignore.read_text(encoding="utf-8")
        if not existing_text.endswith("\n"):
            prefix = "\n"
    with gitignore.open("a", encoding="utf-8") as fp:
        fp.write(f"{prefix}{_GITIGNORE_ENTRY}\n")
    return True


def _read_alembic_revision(db_path: Path) -> str | None:
    """Return the current ``alembic_version.version_num`` for ``db_path``.

    Returns ``None`` when the table does not exist (which would be
    surprising right after ``run_migrations`` but we tolerate it to
    keep the summary output robust against a half-initialised file).
    """
    # We use a short-lived Project so the engine's PRAGMAs apply and
    # the SELECT happens through the same path the rest of the CLI
    # uses. Avoid running migrations a second time — they were just
    # applied — by going through ``Project.open(migrate=False)`` and
    # using the engine's raw ``connect``.
    from ctxr.fsm.sqlite import Project as _Project

    project = _Project.open(db_path, migrate=False, echo=False)
    try:
        with project.engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    finally:
        project.close()
    if row is None:
        return None
    return str(row[0])


def init(
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
    no_memory: bool = typer.Option(
        False,
        "--no-memory",
        help=(
            "Skip queueing the memory installer for follow-up. "
            "The installer itself ships in W11 — until then this flag "
            "only suppresses the post-init reminder."
        ),
    ),
) -> None:
    """Initialise a ctxr-fsm project under the current directory.

    Creates ``./.ctxr-fsm`` (plus ``pids/`` subdir), runs
    ``alembic upgrade head`` against the project's SQLite DB,
    appends ``.ctxr-fsm/`` to ``.gitignore`` when in a git checkout,
    and prints a summary.
    """
    db_path = resolve_db_path(db)
    project_dir = db_path.parent
    project_dir.mkdir(parents=True, exist_ok=True)
    pids_dir = project_dir / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)

    # Run migrations directly — never via subprocess. ``run_migrations``
    # handles the env-var dance so the alembic env.py picks up the
    # right URL.
    run_migrations(db_path)

    cwd = Path.cwd()
    gitignore_updated = False
    if (cwd / ".git").is_dir():
        gitignore_updated = _ensure_gitignore_entry(cwd)

    revision = _read_alembic_revision(db_path)

    summary: dict[str, Any] = {
        "db_path": str(db_path),
        "project_dir": str(project_dir),
        "pids_dir": str(pids_dir),
        "alembic_revision": revision,
        "gitignore_updated": gitignore_updated,
        "ports_json": str(project_dir / "ports.json")
        + " (placeholder — populated by the serve subcommand in W7)",
        "memory_installer": (
            "skipped (--no-memory)"
            if no_memory
            else "memory installer comes in W11; re-run init then to wire it up"
        ),
    }
    json_or_pretty(summary, json_mode)
