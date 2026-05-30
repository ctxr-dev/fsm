"""Unit tests for ``scripts/sync_bootstrap_doc.py`` (the repo-root mirror).

The canonical bootstrap doc lives at
``ctxr/fsm/memory/bootstrap.md`` so ``ctxr-fsm install-memory`` can
stage it under each project's ``.ctxr-fsm/memory/``. The repo-root
``BOOTSTRAP.md`` is a generated mirror that lets cross-workspace
skills ``@import`` the doc via the repo's GitHub raw URL without
reaching inside the Python package.

These tests pin two invariants:

* Running ``sync()`` against the live repo is a no-op when the mirror
  is in sync (so CI can run the script as a freshness gate and fail
  on drift).
* The mirror body — everything below the generated-from header line —
  is byte-identical to the package source.
* Running ``sync()`` against a deliberately-stale or absent mirror in
  a tmpdir rewrites it to match the source, and a follow-up call
  returns ``False`` (no further write).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest

from ctxr.fsm.memory import get_bootstrap_path

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
SCRIPT_PATH: Path = REPO_ROOT / "scripts" / "sync_bootstrap_doc.py"


def _load_sync_module() -> object:
    """Import ``scripts/sync_bootstrap_doc.py`` as an attribute-accessible module.

    The script lives outside any installed package, so we go through
    :mod:`importlib.util` to load it from its file path. We do this in
    a helper rather than at module-import time so individual tests can
    monkeypatch the loaded module's :data:`SOURCE` / :data:`MIRROR`
    constants without leaking state between tests.
    """

    spec = importlib.util.spec_from_file_location(
        "sync_bootstrap_doc", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sync_mod() -> object:
    """Fresh import of the sync script for each test (no shared state)."""

    return _load_sync_module()


# ---------------------------------------------------------------------------
# Repo-level invariants
# ---------------------------------------------------------------------------


def test_script_exists_at_expected_path() -> None:
    """The sync script lives at ``fsm/scripts/sync_bootstrap_doc.py``."""

    assert SCRIPT_PATH.is_file(), SCRIPT_PATH


def test_repo_root_mirror_in_sync_with_package_source(sync_mod: object) -> None:
    """``sync()`` is a no-op against the live repo (drift-detector gate)."""

    changed: bool = sync_mod.sync()  # type: ignore[attr-defined]
    assert changed is False, (
        "BOOTSTRAP.md is out of sync with ctxr/fsm/memory/bootstrap.md; "
        "run 'uv run python scripts/sync_bootstrap_doc.py' and commit "
        "the result."
    )


def test_mirror_body_byte_identical_to_source_minus_header(
    sync_mod: object,
) -> None:
    """Mirror body (below the generated-from header) equals the source bytes."""

    mirror_path: Path = sync_mod.MIRROR  # type: ignore[attr-defined]
    header: str = sync_mod.HEADER  # type: ignore[attr-defined]

    mirror_text = mirror_path.read_text(encoding="utf-8")
    source_text = get_bootstrap_path().read_text(encoding="utf-8")

    assert mirror_text.startswith(header), "mirror must lead with generated-from header"
    assert mirror_text[len(header):] == source_text


# ---------------------------------------------------------------------------
# Behaviour against a tmpdir copy
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_pair(tmp_path: Path, sync_mod: object) -> Iterator[tuple[Path, Path]]:
    """Yield ``(source, mirror)`` paths inside a tmpdir for safe mutation.

    Note: macOS's default filesystem (APFS/HFS+) is case-insensitive,
    so ``bootstrap.md`` and ``BOOTSTRAP.md`` would resolve to the SAME
    file on developer machines while still being distinct on Linux
    CI. We sidestep the ambiguity by giving the fixture files
    obviously-distinct stems.
    """

    source = tmp_path / "source_bootstrap.md"
    mirror = tmp_path / "MIRROR_bootstrap.md"
    source.write_text("# bootstrap fixture\n\nbody.\n", encoding="utf-8")
    yield source, mirror


def test_sync_writes_mirror_when_absent(
    tmp_pair: tuple[Path, Path], sync_mod: object
) -> None:
    """A missing mirror is created on first ``sync()``; second call is a noop."""

    source, mirror = tmp_pair
    assert not mirror.exists()

    first: bool = sync_mod.sync(source=source, mirror=mirror)  # type: ignore[attr-defined]
    assert first is True
    assert mirror.is_file()

    second: bool = sync_mod.sync(source=source, mirror=mirror)  # type: ignore[attr-defined]
    assert second is False


def test_sync_rewrites_mirror_when_stale(
    tmp_pair: tuple[Path, Path], sync_mod: object
) -> None:
    """A mirror whose content lags the source is rewritten."""

    source, mirror = tmp_pair
    mirror.write_text("stale content\n", encoding="utf-8")

    changed: bool = sync_mod.sync(source=source, mirror=mirror)  # type: ignore[attr-defined]
    assert changed is True

    header: str = sync_mod.HEADER  # type: ignore[attr-defined]
    assert mirror.read_text(encoding="utf-8") == header + source.read_text(
        encoding="utf-8"
    )


def test_sync_raises_when_source_missing(
    tmp_path: Path, sync_mod: object
) -> None:
    """A missing source surfaces as :class:`FileNotFoundError`, not silent skip."""

    missing_source = tmp_path / "does_not_exist.md"
    mirror = tmp_path / "BOOTSTRAP.md"

    with pytest.raises(FileNotFoundError):
        sync_mod.sync(source=missing_source, mirror=mirror)  # type: ignore[attr-defined]
