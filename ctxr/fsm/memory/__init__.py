"""Canonical FSM-usage principles + bootstrap doc shipped with the ctxr-fsm package.

This package directory holds:

* the source-of-truth principles file (``principles.md``) plus per-client
  adapters generated from it (``principles.claude.md``,
  ``principles.codex.md``, ``principles.cursor.md``).
* the source-of-truth bootstrap doc (``bootstrap.md``) describing how a
  skill / agent should ensure ``ctxr-fsm`` is ready before any work.
  The repo-root ``BOOTSTRAP.md`` is a generated mirror of this file
  (see ``scripts/sync_bootstrap_doc.py``).
* W23-SSOT canonical reference docs that any skill / agent in the
  workspace links to instead of inlining the contract themselves:
  ``AGENT_QUICKSTART.md`` (five-minute orientation),
  ``SKILL_TEMPLATE.md`` (skill authoring template),
  ``GATE_CONTRACT.md`` (cross-FSM gate protocol). These are reachable
  from any Python consumer via :func:`get_ssot_doc_path` so no caller
  needs to know the on-disk layout.

The adapters are byte-stable re-emits of ``principles.md`` with the
right per-client frontmatter and a generated-from header. Regenerate
via:

    uv run python tools/generate_memory_adapters.py
"""

from __future__ import annotations

from pathlib import Path

PRINCIPLES_DIR: Path = Path(__file__).parent

# Filename of the bootstrap doc inside this package, kept as a module
# constant so the installer + tests + sync script all reference the
# same symbol rather than open-coding the string.
BOOTSTRAP_FILENAME: str = "bootstrap.md"

_CLIENT_FILENAMES: dict[str, str] = {
    "canonical": "principles.md",
    "claude": "principles.claude.md",
    "codex": "principles.codex.md",
    "cursor": "principles.cursor.md",
}

# W23-SSOT canonical reference docs. Keyed by short slug so callers
# (skills, CLI commands, IDE integrations) reference them by name
# rather than open-coding the on-disk filename. Adding a new SSOT doc
# means landing the file under this directory and adding one entry
# here; nothing else needs to know.
_SSOT_FILENAMES: dict[str, str] = {
    "agent_quickstart": "AGENT_QUICKSTART.md",
    "skill_template": "SKILL_TEMPLATE.md",
    "gate_contract": "GATE_CONTRACT.md",
}


def get_principles_path(client: str = "claude") -> Path:
    """Return the absolute path to the principles file for ``client``.

    Parameters
    ----------
    client:
        One of ``"canonical"``, ``"claude"``, ``"codex"``, ``"cursor"``.
        Defaults to ``"claude"``.

    Raises
    ------
    ValueError
        If ``client`` is not a recognised key.
    FileNotFoundError
        If the requested file is not present on disk.
    """

    try:
        filename = _CLIENT_FILENAMES[client]
    except KeyError as exc:
        valid = ", ".join(sorted(_CLIENT_FILENAMES))
        raise ValueError(
            f"unknown client {client!r}; expected one of: {valid}"
        ) from exc

    path = PRINCIPLES_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"principles file missing for client={client!r}: {path}"
        )
    return path


def get_bootstrap_path() -> Path:
    """Return the absolute path to the canonical ``bootstrap.md`` doc.

    The bootstrap doc is client-agnostic (every skill / agent reads the
    same text, regardless of which MCP client hosts it), so there is no
    per-client variant — one file, one path.

    Raises
    ------
    FileNotFoundError
        If the bootstrap doc is missing from the installed package — a
        packaging bug the caller should surface rather than paper over.
    """

    path = PRINCIPLES_DIR / BOOTSTRAP_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"bootstrap doc missing in package: {path}")
    return path


def get_ssot_doc_path(slug: str) -> Path:
    """Return the absolute path to a W23-SSOT canonical reference doc.

    Parameters
    ----------
    slug:
        One of ``"agent_quickstart"``, ``"skill_template"``,
        ``"gate_contract"``.

    Raises
    ------
    ValueError
        If ``slug`` is not a recognised SSOT doc key.
    FileNotFoundError
        If the doc is missing from the installed package — a packaging
        bug the caller should surface rather than paper over.

    The SSOT docs are intentionally client-agnostic plain Markdown. A
    skill or agent that needs the contract reads the file via this
    helper rather than re-deriving the same content; that way an update
    to the contract lands in one place and propagates uniformly.
    """

    try:
        filename = _SSOT_FILENAMES[slug]
    except KeyError as exc:
        valid = ", ".join(sorted(_SSOT_FILENAMES))
        raise ValueError(
            f"unknown SSOT doc slug {slug!r}; expected one of: {valid}"
        ) from exc

    path = PRINCIPLES_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"SSOT doc missing in package: {path}")
    return path


def list_ssot_doc_slugs() -> list[str]:
    """Return the sorted list of known W23-SSOT doc slugs.

    Useful for CLI commands that iterate over the full set (e.g.
    ``ctxr-fsm doctor --skill-consumer`` checks each one is reachable
    after install) without hard-coding the slug list.
    """

    return sorted(_SSOT_FILENAMES)


__all__ = [
    "BOOTSTRAP_FILENAME",
    "PRINCIPLES_DIR",
    "get_bootstrap_path",
    "get_principles_path",
    "get_ssot_doc_path",
    "list_ssot_doc_slugs",
]
