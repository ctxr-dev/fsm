#!/usr/bin/env python3
"""Sync the repo-root ``BOOTSTRAP.md`` mirror from the package source.

The canonical bootstrap doc lives inside the ``ctxr-fsm`` package at
``ctxr/fsm/memory/bootstrap.md`` (so ``ctxr-fsm install-memory`` can
stage it next to ``principles.md`` under ``.ctxr-fsm/memory/``). For
cross-workspace skills that prefer to ``@import`` the doc via the repo's
GitHub raw URL we also mirror the file at ``fsm/BOOTSTRAP.md``.

This script keeps the two in sync. It is the single source: the mirror
is REGENERATED from the package file every time the package file
changes. The mirror is byte-identical to the package file with one
extra single-line HTML header prepended that points back at the
source path so a casual reader knows where to edit.

Contract
--------

* Exit ``0`` and writes nothing when the mirror is already in sync.
* Exit ``0`` and writes the mirror when the mirror is missing or stale.
* Exit non-zero when the package source is missing (a CI / dev mistake
  the human needs to surface, not silently paper over).

The :func:`sync` function below is the import-friendly entry point used
by the unit test ``tests/unit/memory/test_bootstrap_doc_sync.py`` to
guarantee no drift slips through review.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
SOURCE: Path = REPO_ROOT / "ctxr" / "fsm" / "memory" / "bootstrap.md"
MIRROR: Path = REPO_ROOT / "BOOTSTRAP.md"

# The single-line header pinned at the top of the mirror so any human
# who opens BOOTSTRAP.md without context immediately knows where the
# canonical copy lives + how to regenerate. We keep it as a one-liner
# (no trailing blank line baked in) so the mirror body below is
# byte-identical to the source — the test compares the mirror minus
# this header against the source bytes.
HEADER: str = (
    "<!-- generated from ctxr/fsm/memory/bootstrap.md "
    "by scripts/sync_bootstrap_doc.py — do not edit directly -->\n"
)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a tmp + rename so reads are atomic.

    A naive ``Path.write_text`` would briefly truncate the destination
    on POSIX, which is fine for a script run by a human but bad for any
    concurrent reader (CI, editor, watcher). The tmp-in-same-dir +
    ``Path.replace`` pattern gives us an all-or-nothing swap.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def sync(*, source: Path = SOURCE, mirror: Path = MIRROR) -> bool:
    """Refresh ``mirror`` from ``source`` if (and only if) they drifted.

    Returns ``True`` when the mirror was rewritten, ``False`` when it
    was already in sync. The boolean is what the unit test asserts
    against to verify "running the script after a clean checkout is a
    no-op".

    Raises :class:`FileNotFoundError` if ``source`` is missing — a
    structural project problem the caller must surface, not paper over.
    """

    if not source.is_file():
        raise FileNotFoundError(
            f"bootstrap source missing at {source}; cannot sync mirror"
        )

    source_text = source.read_text(encoding="utf-8")
    desired = HEADER + source_text

    if mirror.is_file() and mirror.read_text(encoding="utf-8") == desired:
        return False

    _atomic_write_text(mirror, desired)
    return True


def main() -> int:
    """CLI entry point: returns ``0`` on success / no-op, non-zero on error."""

    try:
        changed = sync()
    except FileNotFoundError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if changed:
        print(f"updated: {MIRROR.relative_to(REPO_ROOT)}")
    else:
        print(f"unchanged: {MIRROR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
