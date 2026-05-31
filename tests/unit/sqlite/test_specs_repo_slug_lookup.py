"""Unit tests for SpecsRepo.get_latest_by_slug (W14k BLOCKER-4).

Covers the slug-based spec lookup that backs ``fsm.start_run`` accepting
either a UUID primary key OR a human-readable slug. SKILL.md authors
pass the slug because the UUID isn't known at skill-author time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ctxr.fsm.core import FsmSpec
from ctxr.fsm.sqlite.project import Project
from ctxr.fsm.sqlite.repos_core import SpecsRepo


def _fixture_spec(slug: str, version_marker: int = 1) -> FsmSpec:
    """Build a minimal valid FsmSpec with the given slug + marker."""

    return FsmSpec.model_validate(
        {
            "id": slug,
            "version": 1,
            "entry": "only",
            "states": [
                {
                    "id": "only",
                    "purpose": f"v{version_marker}",
                    "outputs": [],
                    "transitions": [],
                }
            ],
        }
    )


@pytest.fixture()
def project(tmp_path: Path) -> Any:
    """Open a fresh project DB under tmp_path."""

    p = Project.open(tmp_path / ".ctxr-fsm" / "fsm.db")
    try:
        yield p
    finally:
        p.close()


def test_get_latest_by_slug_returns_only_registered_version(project: Any) -> None:
    """One registered spec → get_latest_by_slug returns it."""
    project.register_spec(_fixture_spec("code-reviewer"))

    with project.session_factory() as session:
        row = SpecsRepo.get_latest_by_slug(session, "code-reviewer")
    assert row is not None
    assert row.slug == "code-reviewer"
    assert row.version == 1


def test_get_latest_by_slug_returns_highest_version(project: Any) -> None:
    """When multiple versions exist for the same slug, returns the highest."""
    project.register_spec(_fixture_spec("code-reviewer", version_marker=1))
    # A second registration with a DIFFERENT body (purpose field changed)
    # bumps the version per SpecsRepo.register's content-hash dedup rule.
    project.register_spec(_fixture_spec("code-reviewer", version_marker=2))
    project.register_spec(_fixture_spec("code-reviewer", version_marker=3))

    with project.session_factory() as session:
        row = SpecsRepo.get_latest_by_slug(session, "code-reviewer")
    assert row is not None
    assert row.version == 3


def test_get_latest_by_slug_returns_none_for_unknown_slug(project: Any) -> None:
    """Slug lookup misses cleanly (no row) instead of raising."""
    with project.session_factory() as session:
        row = SpecsRepo.get_latest_by_slug(session, "no-such-slug")
    assert row is None


def test_get_latest_by_slug_with_project_id_scopes_search(project: Any) -> None:
    """Passing project_id narrows the lookup to that project's rows.

    Forward-compat with the multi-project schema. Today there's a single
    project per DB so this is mostly a smoke test that the optional
    parameter doesn't break the lookup when supplied.
    """
    project.register_spec(_fixture_spec("scoped-spec"))

    with project.session_factory() as session:
        all_projects = project.projects.list(session)
    assert len(all_projects) == 1
    pid = all_projects[0].id

    with project.session_factory() as session:
        row_scoped = SpecsRepo.get_latest_by_slug(session, "scoped-spec", project_id=pid)
        row_other = SpecsRepo.get_latest_by_slug(
            session, "scoped-spec", project_id="00000000-0000-0000-0000-000000000000"
        )
    assert row_scoped is not None
    assert row_other is None
