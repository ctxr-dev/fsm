"""Pin the W14j doctor pretty-print migration (panel + subsystem table).

The doctor command's pretty surface (W14j) is the Rich Panel
summarising DB path + alembic revision, followed by the shared
subsystem table. The earlier free-form ``rich.print(report)`` dict
dump is gone — these tests pin the NEW shape AND the absence of the
OLD format so a regression that brought back the dict dump trips
loudly.

The ``--json`` surface is unchanged and is covered by the existing
``tests/cli/test_cli_doctor.py`` battery; we deliberately do NOT
re-assert any JSON contracts here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from typer.testing import CliRunner

from ctxr.fsm.cli import app

runner = CliRunner()


def _init_project(db_path: Path) -> None:
    """Land a fresh project DB at ``db_path``."""
    result = runner.invoke(app, ["init", "--db", str(db_path), "--json"])
    assert result.exit_code == 0, result.stdout


# ---------------------------------------------------------------------------
# New pretty surface present (Panel + Table)
# ---------------------------------------------------------------------------


def test_doctor_pretty_renders_rich_panel_header() -> None:
    """The Rich Panel carries the ``ctxr-fsm doctor`` title + DB + Revision rows."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path)])

        assert result.exit_code == 0, result.stderr
        assert "ctxr-fsm doctor" in result.stdout
        # The panel body labels are present (the values themselves
        # vary by tempdir layout / migration revision).
        assert "DB" in result.stdout
        assert "Revision" in result.stdout


def test_doctor_pretty_renders_subsystem_table_skeleton() -> None:
    """The shared subsystem-table headers + the Project row are always present."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path)])

        assert result.exit_code == 0, result.stderr
        # W16 split: the table carries Subsystem/Status/PID; URL +
        # Swagger now print in the OSC-8 link block below the table.
        # The runner's narrow terminal elides Subsystem→Sub…, so pin
        # only headers that survive the 80-col fit.
        for header in ("Status", "PID"):
            assert header in result.stdout, (
                f"missing header {header!r} in:\n{result.stdout}"
            )
        assert "Project" in result.stdout
        assert "ctxr-fsm subsystems" in result.stdout


# ---------------------------------------------------------------------------
# Old free-form dict dump is gone
# ---------------------------------------------------------------------------


def test_doctor_pretty_omits_legacy_dict_dump_keys() -> None:
    """The pre-W14j ``rich.print(report)`` exposed every report key directly.

    Specifically: ``file_size_bytes``, ``sqlite_version``, ``pragmas``,
    ``tables``, ``journal_txns``, ``locks``, ``supervisor`` appeared as
    dict-key substrings in the pretty output. The W14j surface drops
    all of these from the human-facing path (operators script the
    JSON surface for these; the pretty surface is dashboard-style).

    Pinning the absence of these keys here means a regression that
    re-introduces the dict dump trips immediately.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path)])

        assert result.exit_code == 0, result.stderr

        # Keys that were dict-dumped in the old pretty surface and
        # MUST NOT appear in the new one. We pin a handful — enough
        # to surface a regression without coupling to every key.
        legacy_keys = (
            "file_size_bytes",
            "sqlite_version",
            "journal_txns",
            "pragmas",
        )
        for key in legacy_keys:
            assert key not in result.stdout, (
                f"legacy report key {key!r} leaked into pretty output:\n{result.stdout}"
            )


def test_doctor_json_surface_unchanged() -> None:
    """``--json`` still emits the full report shape (wire-format compatibility).

    Belt-and-braces: even though the dedicated battery in
    ``test_cli_doctor.py`` covers the JSON surface, we keep one
    smoke-check here so a regression that migrated the JSON path
    too aggressively shows up in this file as well.
    """
    import json as _json

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.stderr
        payload = _json.loads(result.stdout)
        # The shape every script depends on:
        for key in (
            "db_path",
            "file_size_bytes",
            "sqlite_version",
            "pragmas",
            "alembic_revision",
            "tables",
            "journal_txns",
            "locks",
            "supervisor",
        ):
            assert key in payload, f"json surface lost key {key!r}: {payload}"
