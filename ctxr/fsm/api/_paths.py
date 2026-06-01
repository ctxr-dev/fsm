"""Path-display helpers shared across the API surface.

Lives in its own module so :mod:`ctxr.fsm.api.routes_admin`,
:mod:`ctxr.fsm.api.__init__` (the ProjectMetadata route), and any
future surface that wants to render a portable, relative DB path can
all import without creating a circular dependency. The :class:`Project`
handle owns the absolute path; this module owns the
project-root-relative rewrite the UI prefers for display.
"""

from __future__ import annotations

from pathlib import Path

def looks_like_filesystem_db_path(db_url_database: str | None) -> bool:
    """Return whether ``db_url_database`` is plausibly a real on-disk path.

    Filters out every non-filesystem sentinel SQLAlchemy's SQLite
    dialect can emit on ``engine.url.database`` — enumerated by the
    W22a adversarial-verify workflow (``wbyobu5wr``):

    * :data:`None` — ``sqlite://`` (no database segment).
    * ``""`` — ``sqlite:///`` (empty database segment; pysqlite
      internally substitutes ``:memory:`` at connect time).
    * ``":memory:"`` — canonical SQLite in-memory sentinel
      (``sqlite:///:memory:``).
    * Any string starting with ``"file:"`` (case-insensitive) —
      SQLite's URI-filename scheme. Used for the shared-cache
      in-memory form ``file::memory:?cache=shared`` AND for any
      ``mode=ro|rw|rwc|memory`` URI. SQLAlchemy's URL parser
      strips the query string into ``url.query`` so the bare
      ``file:test.db`` value lands in ``url.database`` — looking
      like a relative filename, but it's not: real filesystem
      paths cannot start with ``file:`` because POSIX / Windows
      would need a separator before that token. Anything with the
      prefix needs URI-aware decoding (which is outside the scope
      of a display helper) so we treat it as non-filesystem.

    Future remote backends (postgresql, mysql, mssql) will populate
    ``url.database`` with a DSN component, not a path — those would
    fool a path-derivation function the same way. Adding a guard
    once a remote backend lands is the right time to extend this
    function; today it's SQLite-only.
    """
    if not db_url_database:
        return False
    if db_url_database == ":memory:":
        return False
    if db_url_database.lower().startswith("file:"):
        return False
    return True


def project_root_and_relative(db_path: str) -> tuple[Path, str]:
    """Return ``(project_root, db_path_relative)`` for the open DB.

    ``project_root`` is the directory that hosts ``.ctxr-fsm/`` — found
    by walking up from the resolved DB path. ``db_path_relative`` is
    the DB path rendered relative to ``project_root``; for the
    canonical ``ctxr-fsm init`` layout this is ``.ctxr-fsm/fsm.db``.

    When the DB lives outside a ``.ctxr-fsm/`` layout (operator passed
    ``--db /some/random.db``), we fall back to the DB's parent as the
    root and surface just the filename as the relative path — an
    honest signal that the layout isn't canonical, without breaking
    the UI shape.

    The path is ``.resolve()``-d up front so symlinks resolve to their
    target. The trade-off: when a project sits behind a symlink, the
    reported ``project_root`` reflects the resolved target, which can
    differ from where the CLI was invoked. Documented for the API
    consumers; the UI should treat the value as a stable identifier
    rather than a navigation target.
    """
    abs_db = Path(db_path).resolve()
    for ancestor in abs_db.parents:
        if ancestor.name == ".ctxr-fsm":
            root = ancestor.parent
            return root, str(abs_db.relative_to(root))
    # Non-canonical layout: fall back to the DB's parent.
    fallback_root = abs_db.parent
    try:
        return fallback_root, str(abs_db.relative_to(fallback_root))
    except ValueError:
        return fallback_root, abs_db.name


__all__ = ["looks_like_filesystem_db_path", "project_root_and_relative"]
