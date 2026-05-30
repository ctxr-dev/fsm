"""Unit tests for the W14j Rich subsystem-URL renderer.

The renderer (:mod:`ctxr.fsm.cli._render`) is shared by ``ensure``,
``doctor``, and the ``serve`` supervisor banner. These tests pin the
column ordering, the project-row content, the swagger-derivation
rule for the ``api`` row, the "skip missing subsystems" semantics
(``--mode mcp_only`` only ships an ``mcp`` block; W14i renamed the
wire value from the legacy hyphenated form, which Typer still accepts
with a deprecation warning via ``ensure_cmd._MODE_ALIASES``), and the
colour mapping per status.

ANSI capture pattern
--------------------

Rich detects TTY-ness from the underlying file. To capture coloured
output deterministically every test constructs a private
``rich.console.Console(file=io.StringIO(), force_terminal=True,
width=120)`` and prints the table to that console — that way the
ANSI escape codes survive in the captured string regardless of how
pytest is configured. Tests that only need the structural skeleton
(headers, row text) skip ``force_terminal`` so the captured string
stays human-readable.
"""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from ctxr.fsm.cli._render import (
    portable_project_repr,
    render_subsystem_table,
)


def _render_to_string(table_input: dict, *, project_root: Path, force_terminal: bool = False) -> str:
    """Build a Console-backed StringIO, render the table, return the captured text."""
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=force_terminal,
        color_system="truecolor" if force_terminal else None,
        width=160,
        legacy_windows=False,
    )
    console.print(render_subsystem_table(table_input, project_root=project_root))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixture payloads
# ---------------------------------------------------------------------------


def _full_active_mcp() -> dict:
    """Representative payload with mcp + api + ui all present (status=ready)."""
    return {
        "started_at": "2026-05-30T12:00:00.000Z",
        "supervisor_pid": 9999,
        "version": "0.1.0a1",
        "subsystems": {
            "mcp": {
                "http_url": "http://127.0.0.1:8770/sse",
                "healthz_url": "http://127.0.0.1:8770/healthz",
                "pid": 1001,
                "status": "ready",
            },
            "api": {
                "http_url": "http://127.0.0.1:8765",
                "healthz_url": "http://127.0.0.1:8765/healthz",
                "pid": 1002,
                "docs_url": "http://127.0.0.1:8765/docs",
                "status": "ready",
            },
            "ui": {
                "http_url": "http://127.0.0.1:5173",
                "healthz_url": None,
                "pid": 1003,
                "status": "ready",
            },
        },
    }


# ---------------------------------------------------------------------------
# Column ordering + project-row content
# ---------------------------------------------------------------------------


def test_render_subsystem_table_columns_in_order(tmp_path: Path) -> None:
    """Headers appear in the locked order: Subsystem | URL | Swagger | Health | PID.

    The order is part of the operator's mental model — a regression
    that reshuffled the columns would silently change every screenshot
    in the docs. We capture the rendered text and assert the columns
    appear left-to-right in the right order.
    """
    output = _render_to_string(_full_active_mcp(), project_root=tmp_path)
    # Each header is its own substring; pinning indices makes the
    # order-property explicit.
    idx_subsystem = output.index("Subsystem")
    idx_url = output.index("URL")
    idx_swagger = output.index("Swagger")
    idx_health = output.index("Health")
    idx_pid = output.index("PID")
    assert idx_subsystem < idx_url < idx_swagger < idx_health < idx_pid, (
        f"unexpected column order in:\n{output}"
    )


def test_render_subsystem_table_includes_project_row(tmp_path: Path) -> None:
    """First data row is ``Project`` with a portable-rendered path.

    The project row gives the operator the context ("this table is
    about *this* checkout") before listing subsystems. The path uses
    the portable repr (relative-to-cwd / ``~``-prefixed / absolute)
    so multi-project terminals stay legible.
    """
    output = _render_to_string(_full_active_mcp(), project_root=tmp_path)
    # The project row appears before any subsystem name.
    idx_project = output.index("Project")
    idx_mcp = output.index("mcp")
    assert idx_project < idx_mcp
    # The portable repr appears verbatim in the project row.
    expected_repr = portable_project_repr(tmp_path)
    assert expected_repr in output


# ---------------------------------------------------------------------------
# Swagger derivation
# ---------------------------------------------------------------------------


def test_render_subsystem_table_swagger_derived_for_api(tmp_path: Path) -> None:
    """When ``docs_url`` is absent on the api payload, derive ``http_url + /docs``.

    The W14c discovery doc historically carried ``docs_url`` for
    api; the renderer must still cope when a future payload variant
    omits it (e.g. a check-mode run that only fills the URL). Other
    rows (mcp / ui) MUST stay blank — Swagger is api-specific.
    """
    payload = _full_active_mcp()
    del payload["subsystems"]["api"]["docs_url"]  # force derivation

    output = _render_to_string(payload, project_root=tmp_path)
    # The derived swagger URL appears for api.
    assert "http://127.0.0.1:8765/docs" in output
    # The mcp row has no swagger column content (we cannot grep for
    # "empty cell" but the mcp http_url and the api swagger URL are
    # different strings, so the per-row content stays unambiguous).
    # We additionally check that no swagger appears for the mcp URL:
    assert "http://127.0.0.1:8770/sse/docs" not in output


# ---------------------------------------------------------------------------
# Missing-subsystem semantics
# ---------------------------------------------------------------------------


def test_render_subsystem_table_skips_missing_subsystems(tmp_path: Path) -> None:
    """In ``--mode mcp_only`` only the mcp row appears (plus the project row)."""
    payload = _full_active_mcp()
    # Drop api + ui as the mcp_only mode would.
    del payload["subsystems"]["api"]
    del payload["subsystems"]["ui"]

    output = _render_to_string(payload, project_root=tmp_path)
    assert "Project" in output
    assert "mcp" in output
    # api / ui rows are absent. We assert on the URL substrings rather
    # than the bare names ("api" / "ui" are tiny tokens that could be
    # part of other words like "swagger" or "subsystems").
    assert "127.0.0.1:8765" not in output  # api URL absent
    assert "127.0.0.1:5173" not in output  # ui URL absent


def test_render_subsystem_table_handles_empty_subsystems(tmp_path: Path) -> None:
    """An empty ``subsystems`` block leaves the table with just the project row.

    Defensive coverage for the case where ``active-mcp.json`` exists
    but has not been populated yet (a supervisor mid-boot, or a stale
    file from a previous crash). The renderer MUST NOT raise; it
    surfaces a project-only table the operator can still read.
    """
    payload = {"subsystems": {}}
    output = _render_to_string(payload, project_root=tmp_path)
    assert "Project" in output
    assert "mcp" not in output
    assert "api" not in output


# ---------------------------------------------------------------------------
# Status colours (ANSI capture)
# ---------------------------------------------------------------------------


def test_render_subsystem_table_status_colours(tmp_path: Path) -> None:
    """Each closed-vocabulary status maps to its expected Rich colour.

    Rich uses ANSI escape codes for colours; we ``force_terminal``
    so they survive the StringIO capture. We assert on the SGR
    PARAMETER tokens (``1`` for bold, ``32`` / ``33`` / ``31`` for the
    8-colour green / yellow / red foregrounds) regex-matched inside a
    ``CSI ... m`` escape sequence, NOT on a fixed concatenation like
    ``\\x1b[1;32m``. Rich's emit format varies across versions and
    color-system settings (combined ``\\x1b[1;32m`` vs split
    ``\\x1b[1m\\x1b[32m`` vs reordered parameters), so a regex that
    accepts either ordering survives Rich upgrades while still pinning
    the actual SGR intent (bold AND the specific colour code on the
    same status row, not just any digit pair that happens to appear).
    """
    # Helper: parse out every SGR escape's parameter set into a frozenset.
    # An SGR escape is ``CSI <params> m`` where params is a ``;``-joined
    # decimal list. We collect every SGR set that appears in the output
    # so the assertion can ask "did some SGR include both 1 (bold) and N
    # (the target colour code)?" without caring about parameter order or
    # whether Rich split bold + colour into two consecutive escapes.
    import re as _re
    from itertools import pairwise as _pairwise

    sgr_re = _re.compile(r"\x1b\[([\d;]*)m")

    def _styling_sets(output: str) -> list[frozenset[str]]:
        """Return one parameter-set per SGR escape in ``output``."""
        return [frozenset(m.group(1).split(";")) for m in sgr_re.finditer(output)]

    def _has_bold_color(output: str, color: str) -> bool:
        """True iff some SGR includes BOTH the bold flag (1) AND ``color``.

        Covers the combined form (``\\x1b[1;32m`` -> ``{1, 32}``) AND the
        split form (``\\x1b[1m\\x1b[32m`` -> ``{1}`` then ``{32}``,
        which we accept when adjacent in the output's SGR sequence).
        """
        sets = _styling_sets(output)
        for params in sets:
            if "1" in params and color in params:
                return True
        # Split form: find a ``{1}``-only SGR immediately followed by a
        # ``{color}``-only SGR.
        return any("1" in prev and color in curr for prev, curr in _pairwise(sets))

    # Green (ready / spawned).
    green_payload = {
        "subsystems": {
            "mcp": {
                "http_url": "http://127.0.0.1:8770/sse",
                "healthz_url": "http://127.0.0.1:8770/healthz",
                "pid": 1,
                "status": "ready",
            }
        }
    }
    green_output = _render_to_string(green_payload, project_root=tmp_path, force_terminal=True)
    assert _has_bold_color(green_output, "32"), "expected bold-green SGR"

    # Yellow (reused / degraded).
    yellow_payload = {
        "subsystems": {
            "mcp": {
                "http_url": "http://127.0.0.1:8770/sse",
                "healthz_url": "http://127.0.0.1:8770/healthz",
                "pid": 1,
                "status": "reused",
            }
        }
    }
    yellow_output = _render_to_string(yellow_payload, project_root=tmp_path, force_terminal=True)
    assert _has_bold_color(yellow_output, "33"), "expected bold-yellow SGR"

    # Red (missing / unreachable / failed).
    red_payload = {
        "subsystems": {
            "mcp": {
                "http_url": "",
                "healthz_url": None,
                "pid": None,
                "status": "missing",
            }
        }
    }
    red_output = _render_to_string(red_payload, project_root=tmp_path, force_terminal=True)
    assert _has_bold_color(red_output, "31"), "expected bold-red SGR"


def test_render_subsystem_table_unknown_status_falls_back(tmp_path: Path) -> None:
    """An unrecognised status word renders as ``unknown`` (dim white), not a crash."""
    payload = {
        "subsystems": {
            "mcp": {
                "http_url": "http://127.0.0.1:8770/sse",
                "healthz_url": "http://127.0.0.1:8770/healthz",
                "pid": 1,
                "status": "totally-made-up",
            }
        }
    }
    output = _render_to_string(payload, project_root=tmp_path)
    # The literal "unknown" appears in the rendered table.
    assert "unknown" in output


# ---------------------------------------------------------------------------
# PID column behaviour
# ---------------------------------------------------------------------------


def test_render_subsystem_table_missing_pid_shows_dash(tmp_path: Path) -> None:
    """When the payload omits ``pid``, the column renders ``-`` (not crash, not blank).

    The supervisor briefly leaves ``pid`` unset between
    ``acquire_singleton`` and the child-pid rewrite; a fresh
    ``--check`` invocation can hit the file in that window. The
    renderer must produce a stable column value.
    """
    payload = {
        "subsystems": {
            "mcp": {
                "http_url": "http://127.0.0.1:8770/sse",
                "healthz_url": "http://127.0.0.1:8770/healthz",
                "pid": None,
                "status": "ready",
            }
        }
    }
    output = _render_to_string(payload, project_root=tmp_path)
    # Project row has empty PID; the mcp row's PID column should show "-".
    assert " - " in output or "-\n" in output or "│ -" in output


# ---------------------------------------------------------------------------
# Portable project repr (helper)
# ---------------------------------------------------------------------------


def testportable_project_repr_under_cwd(tmp_path: Path) -> None:
    """``project_root`` under ``base`` renders as ``./<rel>``."""
    nested = tmp_path / "inner" / "project"
    nested.mkdir(parents=True)
    assert portable_project_repr(nested, base=tmp_path) == "./inner/project"


def testportable_project_repr_equal_to_base(tmp_path: Path) -> None:
    """``project_root`` equal to ``base`` collapses to ``.`` (not ``./``)."""
    assert portable_project_repr(tmp_path, base=tmp_path) == "."


def testportable_project_repr_absolute_fallback(tmp_path: Path) -> None:
    """An outside-of-base + outside-of-home path renders as an absolute path."""
    # tmp_path is typically under /private/var or /tmp — but we pass a
    # base that's *under* tmp_path so tmp_path itself becomes "outside"
    # base. tmp_path is also outside ``Path.home()`` in CI sandboxes,
    # so the absolute fallback kicks in.
    inner = tmp_path / "inner"
    inner.mkdir()
    rendered = portable_project_repr(tmp_path, base=inner)
    # When tmp_path is also under Path.home() (some test runners),
    # the ``~``-prefixed form wins; otherwise the absolute path.
    assert rendered.startswith("~/") or rendered == str(tmp_path)
