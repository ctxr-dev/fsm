"""Alembic environment for the ctxr.fsm SQLite substrate.

Responsibilities
----------------
* Wire the SQLModel-registered metadata (combined across the three model
  modules) into Alembic's ``target_metadata`` so autogenerate can compare the
  ORM declarations against the live DB.
* Reuse the project's standard engine factory (``open_engine``) for online
  mode so the connect-time PRAGMAs (WAL, foreign_keys=ON, busy_timeout, …)
  apply during migrations exactly as they do at runtime.
* Enable ``render_as_batch=True`` so any future ALTER-style migrations
  recreate the table under SQLite's well-known batch pattern — a no-op for
  the initial CREATE TABLE migration, but the default is set here so we do
  not forget it later.
* Honour an env-var override (``CTXR_FSM_DB_URL``) so callers / tests can
  point alembic at an arbitrary database without rewriting ``alembic.ini``.

The metadata import below MUST stay even if it looks unused — importing the
model modules is precisely what registers the tables on ``SQLModel.metadata``.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlmodel import SQLModel

from alembic import context

# Importing these modules has the side effect of registering every table on
# ``SQLModel.metadata`` (the shared registry SQLModel inherits from
# SQLAlchemy's declarative base). The ``# noqa: F401`` markers tell linters
# the imports are deliberate even though no symbols are referenced directly.
from ctxr.fsm.sqlite import (  # noqa: F401
    models_core,
    models_enforcement,
    models_events,
)
from ctxr.fsm.sqlite.connection import open_engine

# ---------------------------------------------------------------------------
# Alembic config wiring
# ---------------------------------------------------------------------------

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Optional runtime override — the project facade (and tests) set
# CTXR_FSM_DB_URL to point at a per-run DB without mutating alembic.ini.
_env_url = os.environ.get("CTXR_FSM_DB_URL")
if _env_url:
    config.set_main_option("sqlalchemy.url", _env_url)

# The single combined metadata: every imported model module above contributes
# its tables to ``SQLModel.metadata`` at import time, so by the time we read
# this attribute it carries the full schema.
target_metadata = SQLModel.metadata


def _sqlite_path_from_url(url: str) -> str | None:
    """Extract the on-disk path from a ``sqlite:///...`` URL.

    Returns None for non-SQLite URLs (e.g. an in-memory ``:memory:`` form),
    so callers can fall back to a stock ``create_engine`` flow.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    return url[len(prefix):]


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Offline mode emits SQL to stdout without touching a database, so we just
    feed the configured URL straight into Alembic and let it render. The
    project's PRAGMAs are irrelevant here because no connection is opened.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # render_as_batch is harmless for offline initial creates but keeps
        # the offline/online behaviours symmetrical.
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Uses :func:`ctxr.fsm.sqlite.connection.open_engine` so the standard
    connect-time PRAGMAs (WAL journal mode, ``foreign_keys=ON``,
    ``busy_timeout=5000``, ``synchronous=NORMAL``) are applied to every
    connection alembic opens — migrations then see the same DB shape as
    runtime code.
    """
    url = config.get_main_option("sqlalchemy.url")
    sqlite_path = _sqlite_path_from_url(url or "")

    if sqlite_path:
        # Resolve relative paths against the alembic.ini location so the
        # default ``sqlite:///./.ctxr-fsm/fsm.db`` is anchored to the repo
        # root regardless of where the alembic CLI was invoked from.
        path_obj = Path(sqlite_path)
        if not path_obj.is_absolute():
            ini_dir = Path(config.config_file_name or ".").resolve().parent
            path_obj = (ini_dir / path_obj).resolve()
        connectable = open_engine(path_obj)
    else:
        # Non-SQLite or in-memory URL — fall back to the generic factory so
        # tests can still target ``sqlite+pysqlite:///:memory:`` if needed.
        from sqlalchemy import engine_from_config

        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
