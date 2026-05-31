"""Unit tests for ``ctxr-fsm spec validate``.

These tests exercise the user-facing contract of the ``spec validate``
subcommand and the underlying :func:`ctxr.fsm.cli._common.load_spec`
helper that powers it:

* A ``<module>:<attribute>`` import path resolving to a well-formed
  :class:`FsmSpec` produces ``valid=true`` (exit code 0) and surfaces
  no diagnostics.
* The same path resolving to a structurally-broken spec produces
  ``valid=false`` (exit code 1) with the offending breadcrumbs echoed
  in the JSON payload.
* An import path with no ``:`` separator raises
  :class:`typer.BadParameter` (Click renders this as an "Invalid
  value" usage error with exit code 2 through ``CliRunner``).
* An import path that resolves to a non-:class:`FsmSpec` attribute
  raises :class:`typer.BadParameter`.

Fixture FSM specs are written into a private temp directory and made
importable by prepending that directory to :data:`sys.path` for the
duration of each test that needs them.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.cli._common import load_spec

runner = CliRunner()


def _flatten(text: str) -> str:
    """Collapse Rich's word-wrapped, box-drawn output into a single line.

    The CLI's error banners are rendered through Rich, which inserts
    ``\\n│ `` line wraps inside the box characters. Substring assertions
    on the raw output therefore miss split words like ``Invalid\\nvalue``.
    Collapsing whitespace lets us assert on the logical message text
    without coupling to the visual rendering.
    """
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Fixture-spec source bodies
# ---------------------------------------------------------------------------

# A minimal two-state spec that the structural validator accepts. Kept
# inline (rather than referencing a shared fixture file) so each test
# materialises its own pristine module on disk and can be reasoned
# about in isolation.
_VALID_SPEC_SOURCE = """\
from ctxr.fsm.core.models import FsmSpec, State, Transition

spec = FsmSpec(
    id="cli_valid",
    entry="a",
    states=[
        State(
            id="a",
            purpose="start",
            transitions=[Transition(to="b", when="always")],
        ),
        State(id="b", purpose="end"),
    ],
)

# A deliberately non-FsmSpec attribute so we can exercise the
# "attribute exists but is the wrong type" branch of load_spec.
not_a_spec = 42
"""

# Single-state spec whose only transition targets a non-existent state.
# The structural validator records this as a dangling transition and
# flips ``valid`` to ``False``.
_INVALID_SPEC_SOURCE = """\
from ctxr.fsm.core.models import FsmSpec, State, Transition

spec = FsmSpec(
    id="cli_invalid",
    entry="a",
    states=[
        State(
            id="a",
            purpose="start",
            transitions=[Transition(to="ghost", when="always")],
        ),
    ],
)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _materialise_fixture_module(
    tmp_dir: Path, module_name: str, source: str
) -> None:
    """Write ``source`` to ``tmp_dir/<module_name>.py`` and put ``tmp_dir`` on sys.path.

    We use a per-test temporary directory + a per-test module name so
    repeated runs never collide with each other or with already-imported
    modules from earlier tests. The directory is prepended to
    :data:`sys.path` so :func:`importlib.import_module` resolves the
    fixture before any same-named site-packages module.
    """
    (tmp_dir / f"{module_name}.py").write_text(source, encoding="utf-8")
    sys.path.insert(0, str(tmp_dir))


@pytest.fixture()
def tmp_db_path() -> Iterator[Path]:
    """Yield a path to ``fsm.db`` inside a fresh temporary directory.

    ``spec validate`` is a pure in-memory check and never opens the
    DB, so this path is kept on hand only as a convention marker for
    tests that may grow to invoke neighbouring DB-touching subcommands
    in the future; the ``validate`` subcommand itself does not expose
    a ``--db`` flag and so we do not pass one in the assertions below.
    """
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "fsm.db"


@pytest.fixture()
def fixture_module_root() -> Iterator[Path]:
    """Yield a temp dir registered on :data:`sys.path` for the test's lifetime.

    The fixture removes its inserted ``sys.path`` entry and purges any
    test-created modules from :data:`sys.modules` on teardown so test
    ordering can never let one fixture spec leak into another test's
    import resolution.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        before_modules = set(sys.modules)
        try:
            yield tmp_path
        finally:
            # Drop any sys.path entries we (or load_spec) added that
            # point inside this temp directory.
            sys.path[:] = [p for p in sys.path if Path(p) != tmp_path]
            # Drop any modules imported from the temp directory so a
            # later test importing the same module name re-reads disk.
            for name in set(sys.modules) - before_modules:
                module = sys.modules.get(name)
                module_file = getattr(module, "__file__", None)
                if module_file and Path(module_file).is_relative_to(tmp_path):
                    sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# CLI behaviour — happy path
# ---------------------------------------------------------------------------


def test_validate_valid_spec_prints_valid_true_and_exits_zero(
    tmp_db_path: Path, fixture_module_root: Path
) -> None:
    """A well-formed FsmSpec yields ``valid=true`` and exit code 0.

    ``spec validate`` does not expose a ``--db`` flag (it is a pure
    in-memory check), so we drive the subcommand with just the import
    path and ``--json`` to get a parseable payload.
    """
    _materialise_fixture_module(
        fixture_module_root, "cli_spec_validate_ok", _VALID_SPEC_SOURCE
    )

    result = runner.invoke(
        app,
        [
            "spec",
            "validate",
            "cli_spec_validate_ok:spec",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["errors"] == []
    assert payload["unreachable_states"] == []
    assert payload["dangling_transitions"] == []
    assert payload["invalid_predicates"] == []
    assert payload["spec_id"] == "cli_valid"
    # The tmp_db_path fixture is held for parity with neighbouring
    # subcommands that DO require --db; we touch it here so static
    # checkers don't flag the parameter as unused.
    assert tmp_db_path.name == "fsm.db"


# ---------------------------------------------------------------------------
# CLI behaviour — invalid spec
# ---------------------------------------------------------------------------


def test_validate_invalid_spec_prints_valid_false_with_errors(
    tmp_db_path: Path, fixture_module_root: Path
) -> None:
    """A dangling-transition spec yields ``valid=false`` and exit code 1.

    The structured payload must surface the dangling pair and an error
    message that names the missing target, so a CI gate can interpret
    the JSON without re-running the validator.
    """
    _materialise_fixture_module(
        fixture_module_root, "cli_spec_validate_bad", _INVALID_SPEC_SOURCE
    )

    result = runner.invoke(
        app,
        [
            "spec",
            "validate",
            "cli_spec_validate_bad:spec",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    # Hold the tmp_db_path fixture only for symmetry with neighbouring
    # subcommands; ``spec validate`` does not open the DB itself.
    assert tmp_db_path.name == "fsm.db"
    # The JSON payload is emitted on stdout; the "spec validation
    # failed" banner goes to stderr via die(). We only parse the
    # stdout segment so the JSON decoder sees pure JSON.
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert payload["dangling_transitions"] == [["a", "ghost"]]
    assert any("ghost" in err for err in payload["errors"])


# ---------------------------------------------------------------------------
# load_spec helper — BadParameter contracts
# ---------------------------------------------------------------------------


def test_load_spec_without_colon_raises_bad_parameter() -> None:
    """An import path missing the ``:`` separator is rejected at parse time.

    We exercise :func:`load_spec` directly with :func:`pytest.raises`
    because Click swallows :class:`typer.BadParameter` and converts it
    to a "Invalid value" usage error at the CLI boundary; the direct
    call gives us a precise, deterministic assertion on the exception
    type without coupling to Click's rendered output.
    """
    with pytest.raises(typer.BadParameter) as exc_info:
        load_spec("no_colon_here")
    assert "<module>:<attribute>" in str(exc_info.value)


def test_load_spec_non_fsmspec_attribute_raises_bad_parameter(
    fixture_module_root: Path,
) -> None:
    """An attribute that is not an :class:`FsmSpec` is rejected.

    ``cli_spec_validate_ok`` exposes both ``spec`` (an FsmSpec) and
    ``not_a_spec`` (an int). Pointing at the int must raise
    :class:`typer.BadParameter` with a message that mentions the
    actual type the loader found, so the operator can debug the typo
    without grepping the source.
    """
    _materialise_fixture_module(
        fixture_module_root,
        "cli_spec_validate_wrong_type",
        _VALID_SPEC_SOURCE,
    )

    with pytest.raises(typer.BadParameter) as exc_info:
        load_spec("cli_spec_validate_wrong_type:not_a_spec")
    message = str(exc_info.value)
    assert "FsmSpec" in message
    assert "int" in message


# ---------------------------------------------------------------------------
# CLI surface — BadParameter shows up as exit code 2
# ---------------------------------------------------------------------------


def test_cli_rejects_import_path_without_colon_at_exit_code_two(
    tmp_db_path: Path,
) -> None:
    """The CLI surfaces a no-colon import path as a Click usage error.

    Click translates :class:`typer.BadParameter` into a UsageError that
    prints "Invalid value: ..." and exits with code 2. This test pins
    that translation so any future refactor that drops the
    ``typer.BadParameter`` raise (and silently exits 0) is caught.
    """
    result = runner.invoke(
        app,
        [
            "spec",
            "validate",
            "no_colon_here",
        ],
    )

    assert result.exit_code == 2
    flat = _flatten(result.output)
    assert "Invalid value" in flat
    assert "<module>:<attribute>" in flat
    # Hold the tmp_db_path fixture only for parity with sibling tests.
    assert tmp_db_path.name == "fsm.db"


def test_cli_rejects_non_fsmspec_attribute_at_exit_code_two(
    tmp_db_path: Path, fixture_module_root: Path
) -> None:
    """The CLI surfaces a wrong-type attribute as a Click usage error."""
    _materialise_fixture_module(
        fixture_module_root,
        "cli_spec_validate_cli_wrong_type",
        _VALID_SPEC_SOURCE,
    )

    result = runner.invoke(
        app,
        [
            "spec",
            "validate",
            "cli_spec_validate_cli_wrong_type:not_a_spec",
        ],
    )

    assert result.exit_code == 2
    flat = _flatten(result.output)
    assert "Invalid value" in flat
    assert "FsmSpec" in flat
    # Hold the tmp_db_path fixture only for parity with sibling tests.
    assert tmp_db_path.name == "fsm.db"
