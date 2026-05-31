"""CLI-surface tests for ``ctxr-fsm ensure`` (W14b).

Invokes the Typer command via ``uv run`` (subprocess) so the
JSON-on-pipe default + the typer exit-code contract are observed
end-to-end. The actual ensure pipeline is exercised in --check mode
with all sub-steps skipped so the test stays fast (no supervisor
spawn, no real init).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "ctxr-fsm", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_json_is_default_when_stdout_is_a_pipe() -> None:
    """Stdout-not-a-TTY (subprocess.PIPE) → emit JSON without explicit ``--json``.

    Skills consuming ensure parse stdout as JSON; defaulting to JSON
    on a pipe is what makes "ensure --check" → ``json.loads(...)``
    a one-line idiom.
    """
    with tempfile.TemporaryDirectory(prefix="ensure-cli-") as tmpdir:
        proj = Path(tmpdir)
        result = _run_cli(
            [
                "ensure",
                "--check",
                "--no-memory",
                "--no-mcp-config",
                "--client",
                "none",
                "--project-root",
                str(proj),
            ],
            cwd=proj,
        )
        # --check on a fresh dir exits non-zero (status: missing:...).
        # That's fine; the test cares about stdout being parseable JSON.
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"ensure stdout was not JSON despite being on a pipe.\n"
                f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
            ) from exc
        assert "status" in payload
        assert "project_root" in payload


def test_check_mode_exits_nonzero_when_missing() -> None:
    """`--check` exits non-zero on a fresh tmpdir (status: missing:...).

    Scripts can use the exit code as a quick "is everything wired up?"
    gate without parsing the JSON body.
    """
    with tempfile.TemporaryDirectory(prefix="ensure-cli-") as tmpdir:
        proj = Path(tmpdir)
        result = _run_cli(
            [
                "ensure",
                "--check",
                "--no-memory",
                "--no-mcp-config",
                "--client",
                "none",
                "--project-root",
                str(proj),
            ],
            cwd=proj,
        )
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        # W14i: ``status`` is one of the typed ``missing_*`` enum
        # members; init is the most-upstream missing step on a fresh
        # tmpdir so it wins.
        assert payload["status"].startswith("missing_")


def test_help_renders() -> None:
    """``ctxr-fsm ensure --help`` exits 0 and prints flag descriptions."""
    result = subprocess.run(
        ["uv", "run", "ctxr-fsm", "ensure", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--client" in result.stdout
    assert "--mode" in result.stdout
    assert "--check" in result.stdout
    assert "--timeout" in result.stdout
