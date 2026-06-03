"""Unit tests for the ``ctxr-fsm --version`` Typer eager callback.

The bootstrap detect probe (``@.ctxr-fsm/memory/bootstrap.md`` Step 1)
runs ``ctxr-fsm --version`` as a quick "is the package installed in
this workdir?" check. The contract is: exit 0 with one line of output
on stdout matching ``ctxr-fsm <version>``. These tests pin that
contract so a future Typer refactor cannot silently drop the flag.
"""

from __future__ import annotations

import importlib.metadata

from typer.testing import CliRunner

from ctxr.fsm.cli import app


def test_version_flag_prints_package_version_and_exits_zero() -> None:
    """``--version`` exits 0 and prints ``ctxr-fsm <version>`` on stdout."""
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    expected_version = importlib.metadata.version("ctxr-fsm")
    assert result.output.strip() == f"ctxr-fsm {expected_version}"


def test_version_flag_does_not_require_subcommand() -> None:
    """``--version`` works as the sole argv (no subcommand required).

    The bootstrap probe runs the flag in isolation. If the CLI required
    a subcommand to satisfy ``no_args_is_help`` semantics, the probe
    would exit non-zero and the bootstrap would treat the package as
    missing — triggering a redundant install attempt.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    # The probe contract is the exit code, not the help screen.
    assert result.exit_code == 0
    assert "Usage:" not in result.output
