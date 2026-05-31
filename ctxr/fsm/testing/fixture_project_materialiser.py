"""Copy the bundled ``fixture_project/`` template into a caller tmpdir.

Resolves ``gitignore.template`` to ``.gitignore`` so the materialised
copy carries a real ignore file (the template ships under a renamed
name to avoid the dotfile being suppressed by the surrounding repo's
ignore patterns or the build backend's include filters).

After copying, runs ``git init`` and creates two commits: a base
commit holding the seed files unchanged, then a head commit appending
a single ``// HEAD_DIFF_MARKER`` line to ``src/bad_type.ts`` so the
fixture supports a non-empty ``HEAD~1..HEAD`` diff for skills that
inspect the changeset.
"""

from __future__ import annotations

import shutil
import subprocess
from importlib import resources
from pathlib import Path

__all__ = ["materialise_fixture_project"]


_GITIGNORE_TEMPLATE_NAME = "gitignore.template"
_GITIGNORE_DESTINATION_NAME = ".gitignore"
_HEAD_DIFF_TARGET = "src/bad_type.ts"
_HEAD_DIFF_MARKER = "\n// HEAD_DIFF_MARKER: added on top of the base commit so the fixture has a non-empty HEAD~1..HEAD diff for skills that inspect the changeset.\n"


def materialise_fixture_project(dest: Path) -> Path:
    """Copy the bundled fixture project into ``dest``.

    Parameters
    ----------
    dest:
        Target directory. Must not already exist (the helper refuses
        to overwrite a non-empty directory because every test fixture
        should run against a known-clean tree). The parent must exist.

    Returns
    -------
    Path
        ``dest`` after the copy, as an absolute path.
    """
    dest = Path(dest).resolve()
    if dest.exists():
        raise FileExistsError(
            f"{dest} already exists; refusing to overwrite a non-empty "
            "fixture target. Pass a fresh tmpdir path."
        )
    if not dest.parent.exists():
        raise FileNotFoundError(
            f"{dest.parent} does not exist; create the parent before "
            "calling materialise_fixture_project."
        )

    template_root = resources.files("ctxr.fsm.testing.fixture_project")

    # ``importlib.resources`` returns a Traversable that may live
    # inside a zipped wheel. Use ``as_file`` to obtain a real on-disk
    # path even when installed in zip mode, then walk + copy. We do
    # not use ``shutil.copytree(traversable, ...)`` directly because
    # Traversables aren't always coercible to str on every Python
    # build.
    with resources.as_file(template_root) as template_dir:
        shutil.copytree(template_dir, dest)

    # Rename gitignore.template -> .gitignore.
    template_ignore = dest / _GITIGNORE_TEMPLATE_NAME
    if template_ignore.exists():
        template_ignore.rename(dest / _GITIGNORE_DESTINATION_NAME)

    # README.md is for human readers of the template directory in the
    # source repo; do not ship it into the materialised copy.
    readme = dest / "README.md"
    if readme.exists():
        readme.unlink()

    _seed_git_history(dest)
    return dest


def _seed_git_history(project_root: Path) -> None:
    """``git init`` + base commit + head-diff commit."""
    env = {
        "GIT_AUTHOR_NAME": "ctxr-fsm-fixture",
        "GIT_AUTHOR_EMAIL": "fixture@ctxr-fsm.invalid",
        "GIT_COMMITTER_NAME": "ctxr-fsm-fixture",
        "GIT_COMMITTER_EMAIL": "fixture@ctxr-fsm.invalid",
        # Pin commit timestamps to make the resulting SHAs deterministic
        # across cycles, which the W14h consistency battery relies on
        # when comparing report.md outputs byte-for-byte.
        "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
    }
    _git(project_root, ["init", "--quiet", "--initial-branch=main"], env)
    _git(project_root, ["add", "."], env)
    _git(
        project_root,
        ["commit", "--quiet", "-m", "base: seed deliberately-bad TS files"],
        env,
    )

    head_file = project_root / _HEAD_DIFF_TARGET
    head_file.write_text(head_file.read_text() + _HEAD_DIFF_MARKER)

    env_head = {
        **env,
        "GIT_AUTHOR_DATE": "2024-01-02T00:00:00Z",
        "GIT_COMMITTER_DATE": "2024-01-02T00:00:00Z",
    }
    _git(project_root, ["add", _HEAD_DIFF_TARGET], env_head)
    _git(
        project_root,
        ["commit", "--quiet", "-m", "head: append HEAD_DIFF_MARKER for diffable fixture"],
        env_head,
    )


def _git(cwd: Path, args: list[str], env: dict[str, str]) -> None:
    """Run a git subcommand with the locked-down fixture env."""
    full_env = {
        # Strip the caller's HOME-derived git config so a user's
        # ~/.gitconfig (signing keys, hooks, templates) cannot
        # contaminate the fixture's deterministic shape.
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(cwd),  # forces git to not pick up the user's config
        **env,
    }
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args} failed (rc={result.returncode}) in {cwd}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
