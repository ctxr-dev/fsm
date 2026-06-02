"""Unit tests for ``ctxr.fsm.sqlite.project._find_alembic_ini``.

Cross-client bootstrap regression: wheel installs used to crash on
first ``Project.open(..., migrate=True)`` because the wheel did not
ship ``alembic.ini`` and the walk-up search only worked from a
source checkout. The resolver now checks the bundled location first
(``ctxr/fsm/_alembic/alembic.ini`` via the wheel's force-include)
before falling through to the editable-install walk-up. These tests
pin both paths so a future packaging change cannot silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctxr.fsm.sqlite.project import _find_alembic_ini


def test_find_alembic_ini_resolves_to_a_real_file() -> None:
    """The resolver returns an existing alembic.ini.

    Works in both the source-checkout layout (alembic.ini at repo
    root, walked-up from this module) and the wheel-installed layout
    (alembic.ini under ``ctxr/fsm/_alembic/``). Either way, the
    returned path must exist on disk because Alembic opens it.
    """
    ini_path = _find_alembic_ini()
    assert ini_path.is_file()
    assert ini_path.name == "alembic.ini"
    # The sibling migrations/ directory MUST exist next to alembic.ini
    # because alembic.ini's script_location is %(here)s/migrations.
    assert (ini_path.parent / "migrations").is_dir()


def test_find_alembic_ini_prefers_bundled_copy_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundled ``_alembic/alembic.ini`` is preferred over the walk-up.

    Simulated by patching ``__file__`` on the project module to point
    at a fake package tree whose ``_alembic/alembic.ini`` exists; the
    resolver must return that path even when a different alembic.ini
    sits higher up in the walk-up chain.
    """
    fake_pkg = tmp_path / "ctxr" / "fsm" / "sqlite"
    fake_pkg.mkdir(parents=True)
    fake_project_py = fake_pkg / "project.py"
    fake_project_py.write_text("# fake")

    bundled = tmp_path / "ctxr" / "fsm" / "_alembic" / "alembic.ini"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("[alembic]\nscript_location = %(here)s/migrations\n")

    # Also place a decoy alembic.ini up the walk-up tree so the test
    # proves the bundled copy is checked FIRST.
    decoy = tmp_path / "alembic.ini"
    decoy.write_text("[alembic]\n# decoy\n")

    monkeypatch.setattr(
        "ctxr.fsm.sqlite.project.__file__", str(fake_project_py)
    )
    result = _find_alembic_ini()
    assert result == bundled
