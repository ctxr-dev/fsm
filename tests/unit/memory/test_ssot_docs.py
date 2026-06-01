"""Tests for the W23-SSOT memory surface (AGENT_QUICKSTART, SKILL_TEMPLATE,
GATE_CONTRACT) reachability through the public package helpers."""

from __future__ import annotations

import pytest

from ctxr.fsm.memory import (
    PRINCIPLES_DIR,
    get_ssot_doc_path,
    list_ssot_doc_slugs,
)

EXPECTED_SLUGS: tuple[str, ...] = (
    "agent_quickstart",
    "gate_contract",
    "skill_template",
)


def test_list_ssot_doc_slugs_returns_known_set_sorted() -> None:
    assert list_ssot_doc_slugs() == list(EXPECTED_SLUGS)


@pytest.mark.parametrize("slug", EXPECTED_SLUGS)
def test_get_ssot_doc_path_resolves_to_existing_file(slug: str) -> None:
    path = get_ssot_doc_path(slug)
    assert path.is_file()
    # All three docs live alongside principles.md inside the package's
    # memory/ directory.
    assert path.parent == PRINCIPLES_DIR


@pytest.mark.parametrize("slug", EXPECTED_SLUGS)
def test_ssot_doc_carries_frontmatter_with_version(slug: str) -> None:
    body = get_ssot_doc_path(slug).read_text(encoding="utf-8")
    # Each SSOT doc opens with a Markdown YAML frontmatter block so
    # downstream tooling can parse name + version.
    assert body.startswith("---\n")
    assert "name: ctxr-fsm-" in body
    assert "version: " in body


def test_get_ssot_doc_path_rejects_unknown_slug() -> None:
    with pytest.raises(ValueError, match="unknown SSOT doc slug"):
        get_ssot_doc_path("nonexistent")


def test_agent_quickstart_references_canonical_contracts() -> None:
    body = get_ssot_doc_path("agent_quickstart").read_text(encoding="utf-8")
    # AGENT_QUICKSTART must link to the deeper contracts so a reader
    # arriving at it can drill into principles / bootstrap / template /
    # gate contract without guessing the file names.
    assert "principles.md" in body
    assert "bootstrap.md" in body
    assert "SKILL_TEMPLATE.md" in body
    assert "GATE_CONTRACT.md" in body


def test_gate_contract_documents_error_envelopes() -> None:
    body = get_ssot_doc_path("gate_contract").read_text(encoding="utf-8")
    # The gate contract is the only place that documents the resolve_gate
    # error envelope vocabulary. Skill authors and the upcoming W23g
    # implementation both read from this list, so a regression that
    # removes it should fail loudly here.
    for envelope in (
        "gate_value_required",
        "gate_schema_mismatch",
        "gate_source_run_not_found",
        "gate_source_stale",
    ):
        assert envelope in body, f"gate contract missing {envelope!r} envelope"


def test_skill_template_describes_canonical_package_layout() -> None:
    body = get_ssot_doc_path("skill_template").read_text(encoding="utf-8")
    # The skill template is the contract every future skill copies;
    # the spec / handlers / install / workers split must be in there.
    assert "spec.py" in body
    assert "handlers.py" in body
    assert "install.py" in body
    assert "workers/" in body
