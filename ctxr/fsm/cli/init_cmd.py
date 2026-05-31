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
4. Unless ``--no-memory`` is passed, invokes the W11 memory installer
   (``install_memory(target=cwd, client='auto')``) so any AI-client
   memory files already present in the project (CLAUDE.md, AGENTS.md,
   .cursor/rules/) get the FSM-usage principles wired in. The call is
   idempotent — a re-run of ``ctxr-fsm init`` re-asserts the marker
   block without duplicating it. Any exception from the installer is
   caught and surfaced in the summary instead of failing init.
5. Emits a friendly summary (DB path, alembic revision, memory
   installer outcome) via :func:`json_or_pretty` so ``--json`` is
   honoured for scripting.
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

__all__ = ["init", "run_init"]


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


def run_init(
    *,
    db_path: Path,
    no_memory: bool = False,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Pure (non-printing) core of ``ctxr-fsm init``.

    Used both by the Typer command :func:`init` and by
    ``ctxr-fsm ensure`` (W14b), which calls this directly so the
    bootstrap pipeline can compose init + install-memory +
    install-mcp + supervisor into a single per-call summary.

    Parameters
    ----------
    db_path:
        Concrete SQLite DB path. The caller resolved it via
        :func:`resolve_db_path` (no env-var or precedence work
        happens here).
    no_memory:
        When ``True``, skip the W11 memory installer call (matches
        the CLI ``--no-memory`` flag).
    cwd:
        Directory used for the ``.gitignore`` check + memory
        installer target. Defaults to :func:`Path.cwd`. The ensure
        command passes the resolved project root explicitly so a
        deeper invocation cwd doesn't end up patching the wrong tree.
    """
    project_dir = db_path.parent
    project_dir.mkdir(parents=True, exist_ok=True)
    pids_dir = project_dir / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)

    run_migrations(db_path)

    effective_cwd = cwd if cwd is not None else Path.cwd()
    gitignore_updated = False
    if (effective_cwd / ".git").is_dir():
        gitignore_updated = _ensure_gitignore_entry(effective_cwd)

    revision = _read_alembic_revision(db_path)

    memory_installer: dict[str, Any] | str
    if no_memory:
        memory_installer = "skipped (--no-memory)"
    else:
        try:
            from ctxr.fsm.cli.install_memory_cmd import run_install_memory

            memory_installer = run_install_memory(
                target=effective_cwd, client="auto"
            )
        except Exception as exc:
            memory_installer = f"failed: {type(exc).__name__}: {exc}"

    return {
        "db_path": str(db_path),
        "project_dir": str(project_dir),
        "pids_dir": str(pids_dir),
        "alembic_revision": revision,
        "gitignore_updated": gitignore_updated,
        "ports_json": str(project_dir / "ports.json")
        + " (placeholder — populated by the serve subcommand in W7)",
        "memory_installer": memory_installer,
    }


def init(
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
    no_memory: bool = typer.Option(
        False,
        "--no-memory",
        help=(
            "Skip invoking the AI-client memory installer at the end of "
            "init. Useful for non-interactive setups (CI, scripted "
            "container builds) where touching CLAUDE.md / AGENTS.md / "
            ".cursor/rules/ is undesirable."
        ),
    ),
) -> None:
    """Initialise a ctxr-fsm project under the current directory.

    Creates ``./.ctxr-fsm`` (plus ``pids/`` subdir), runs
    ``alembic upgrade head`` against the project's SQLite DB,
    appends ``.ctxr-fsm/`` to ``.gitignore`` when in a git checkout,
    then (unless ``--no-memory``) invokes the W11 memory installer to
    wire FSM-usage principles into any detected AI-client memory.
    Finishes by printing a summary.
    """
    db_path = resolve_db_path(db)
    summary = run_init(db_path=db_path, no_memory=no_memory, cwd=Path.cwd())
    json_or_pretty(summary, json_mode)
