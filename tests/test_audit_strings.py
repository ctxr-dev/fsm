"""Smoke test for the W14i ``audit_strings.sh`` guard script.

The script greps the source tree for closed-vocabulary string literals
that should have been enum-referenced (the user's PR #39 correction).
This test asserts the script exits 0 on the current tree — every
finding is either fixed or carries an explicit ``# audit-strings:
justified`` marker for the genuinely-open one-off Literal cases.

If a future PR introduces a new bare ``Literal[...]`` narrowing that
shadows an existing StrEnum, this test fails with the offending
``file:line`` so CI catches the regression in the same commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_AUDIT_SCRIPT: Path = _REPO_ROOT / "scripts" / "audit_strings.sh"


def test_audit_strings_script_exists() -> None:
    """The audit script lives where ``Makefile`` and tests reference it."""

    assert _AUDIT_SCRIPT.is_file(), _AUDIT_SCRIPT


def test_audit_strings_script_is_executable() -> None:
    """The audit script ships executable so ``make audit-strings`` works.

    A non-executable script is a contributor-trap on a fresh clone:
    the Makefile target works but a manual ``./scripts/audit_strings.sh``
    invocation fails with ``Permission denied``. The bit is enforced
    in the repo so both code paths behave identically.
    """

    assert _AUDIT_SCRIPT.stat().st_mode & 0o111, (
        f"{_AUDIT_SCRIPT} is not executable; chmod +x and re-commit"
    )


def test_audit_strings_clean_on_current_tree() -> None:
    """``audit_strings.sh`` reports zero findings on the current source tree.

    If a future PR introduces an inline ``Literal[...]`` narrowing that
    shadows a StrEnum, or a raw ``== "always"`` / ``== "passed"`` /
    similar comparison on an enum-vocabulary value, this assertion
    fails — the failure message includes the offending file:line so
    the contributor can either reference the enum or add an explicit
    ``# audit-strings: justified`` comment for a genuinely-open
    one-off Literal.
    """

    result = subprocess.run(
        ["bash", str(_AUDIT_SCRIPT), str(_REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        "audit_strings.sh reported findings (re-run "
        "`bash scripts/audit_strings.sh` to see them):\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
