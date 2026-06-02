"""``ctxr-fsm install-memory`` — inject FSM-usage principles into AI-client memory.

This command takes the canonical principles file shipped inside the
``ctxr-fsm`` package (``ctxr/fsm/memory/principles.<client>.md``) and
wires it into the consumer project's standard AI-client memory:

* **Claude Code** (``CLAUDE.md`` or ``.claude/CLAUDE.md``) — uses
  Claude's ``@<path>`` import syntax. We materialise (symlink or copy)
  the package file under ``./.ctxr-fsm/memory/principles.claude.md`` and
  append a single ``@.ctxr-fsm/memory/principles.claude.md`` line inside
  fence markers to the user's existing ``CLAUDE.md``.

* **Codex / OpenAI Agents SDK** (``AGENTS.md``) — Codex does NOT support
  the ``@<path>`` import idiom, so the full body of the principles file
  is inlined between fence markers at the END of ``AGENTS.md``. The
  codex-flavoured principles adapter ALSO inlines the bootstrap body
  inside Principle 0 (see ``tools/generate_memory_adapters.py``), so no
  separate ``.ctxr-fsm/memory/bootstrap.md`` is staged for Codex.

* **Cursor** (``.cursor/rules/ctxr-fsm.mdc``) — Cursor's rule files are
  standalone, so we write the principles file directly with no marker
  block. The rule filename itself is the namespace. Like Codex, the
  cursor adapter inlines the bootstrap body, so no separate staged
  bootstrap.md is needed.

For Claude ONLY we additionally stage ``bootstrap.md`` under
``./.ctxr-fsm/memory/bootstrap.md`` so Principle 0's
``@.ctxr-fsm/memory/bootstrap.md`` reference inside principles.md
resolves to a real file on disk via Claude's transitive ``@`` follow.
Codex and Cursor don't follow the ``@`` import at all, so the bootstrap
content is delivered to those clients by being INLINED into the
principles adapter file at generation time — staging the bootstrap
file separately for them would be pointless (their LLMs can't reach it
via the ``@`` syntax).

Idempotence
-----------

Every patch is wrapped between::

    <!-- ctxr-fsm:begin v=<version> -->
    ...payload...
    <!-- ctxr-fsm:end -->

If the markers already exist we **replace** the whole block (including
the version pinned in the begin marker). If they don't exist we append
the block to the end of the file, separated by a single blank line.
Running the command twice produces a byte-identical file the second
time around (asserted by the smoke loop in the task description).

For Cursor, idempotence is even simpler: we just overwrite the rule
file with the package file's bytes — they only differ across versions.

Modes
-----

* No flag — apply patches.
* ``--check`` — read each detected client's marker block, parse the
  pinned version, compare against the package's version. Print a
  per-client status table. Exits non-zero if any client is out of date
  or missing (useful for ``ctxr-fsm install-memory --check`` in CI).
* ``--dry-run`` — print the patch that *would* be written for each
  client without touching the filesystem.
* ``--no-symlink`` — for Claude, always copy the principles file under
  ``.ctxr-fsm/memory/`` instead of trying to symlink first. Useful on
  filesystems / consumers (eg. Windows without dev-mode) that don't
  cope with symlinks.

Auto-detection (``--client auto``)
----------------------------------

For each potential client we check the target directory:

* ``CLAUDE.md`` OR ``.claude/CLAUDE.md`` -> Claude.
* ``AGENTS.md`` -> Codex.
* ``.cursor/rules/`` (as a directory) -> Cursor.

A target without any of those is a no-op (the command reports
"no client detected" and exits 0 — installing memory into a project
that has no AI-client memory yet is a no-op, not an error).
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer

from ctxr.fsm.cli._clients import McpClient
from ctxr.fsm.cli._common import json_or_pretty
from ctxr.fsm.memory import (
    BOOTSTRAP_FILENAME,
    get_bootstrap_path,
    get_principles_path,
    get_ssot_doc_path,
    list_ssot_doc_slugs,
)

__all__ = [
    "BootstrapStatus",
    "MemoryInstallStatus",
    "install_memory",
    "run_install_memory",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class BootstrapStatus(StrEnum):
    """Status of the staged ``.ctxr-fsm/memory/bootstrap.md`` copy.

    Surfaced on per-client ``--check`` rows. ``inlined`` is the
    sentinel for codex / cursor where the bootstrap content lives
    inside the principles adapter (drift detected via principles
    version comparison, not via a separate hash check).

    Members:

    * ``ok``            — staged file present and content matches
      the package source's bytes.
    * ``out_of_date``   — staged file present but content differs
      from the package source.
    * ``not_installed`` — staged file is absent.
    * ``inlined``       — bootstrap content is inlined into the
      principles adapter for this client; no separate file to compare.

    Wire-format note: the legacy hyphenated ``"out-of-date"`` and
    ``"not-installed"`` values are replaced by snake-case
    ``"out_of_date"`` / ``"not_installed"`` so the enum member name
    matches the wire value (StrEnum members cannot contain hyphens).
    The other values (``ok``, ``inlined``) are byte-identical with
    the pre-W14i shape.
    """

    ok = "ok"
    out_of_date = "out_of_date"
    not_installed = "not_installed"
    inlined = "inlined"


class MemoryInstallStatus(StrEnum):
    """Per-client status surfaced on ``install-memory --check`` rows.

    Mirrors the historical free-form strings (``"ok"``,
    ``"out-of-date"``, ``"missing"``, ``"not-installed"``); kept as
    snake_case enum members. Wire-format note: ``"out-of-date"`` and
    ``"not-installed"`` (hyphenated) are replaced by snake-case
    ``"out_of_date"`` / ``"not_installed"`` so the enum member name
    matches the wire value.
    """

    ok = "ok"
    out_of_date = "out_of_date"
    missing = "missing"
    not_installed = "not_installed"


# Memory installer historically does NOT accept ``McpClient.none`` —
# callers that want to skip memory entirely pass ``--no-memory`` on
# the higher-level ``ensure`` surface, not ``--client none`` here.
# We exclude it from the install-memory choices but keep the shared
# enum elsewhere — the install path is the only one that narrows it.
_MEMORY_CLIENT_CHOICES: tuple[McpClient, ...] = (
    McpClient.auto,
    McpClient.claude,
    McpClient.codex,
    McpClient.cursor,
)

# The set of client identifiers the command understands. Kept as a
# tuple for the CLI's free-form ``--client`` validation; the enum
# above is the canonical source.
_CLIENT_CHOICES: tuple[str, ...] = tuple(
    member.value for member in _MEMORY_CLIENT_CHOICES
)

# The marker fences. We keep both as module constants so callers
# (tests, downstream tools) can import and match against them rather
# than open-coding the strings.
_MARKER_BEGIN_PREFIX: str = "<!-- ctxr-fsm:begin"
_MARKER_END: str = "<!-- ctxr-fsm:end -->"

# Regex that matches a full marker block including the begin/end fences.
# The version captured in group 1 is whatever appears after ``v=`` in
# the begin marker, up to but not including the closing ``-->``. We
# tolerate trailing whitespace inside the marker for robustness against
# hand-edits. ``re.DOTALL`` lets ``.`` span newlines so we capture the
# entire block as one match.
_MARKER_BLOCK_RE: re.Pattern[str] = re.compile(
    r"<!--\s*ctxr-fsm:begin\s+v=([^\s>]+)\s*-->.*?<!--\s*ctxr-fsm:end\s*-->",
    re.DOTALL,
)

# Relative path inside the target project where we materialise the
# Claude principles file so the ``@<path>`` import in CLAUDE.md resolves.
# Kept under ``.ctxr-fsm/`` so a ``rm -rf .ctxr-fsm`` is the project's
# reset (same rationale as the SQLite DB lives there).
_CLAUDE_LINKED_PATH: Path = Path(".ctxr-fsm") / "memory" / "principles.claude.md"

# Relative path inside the target project where we materialise the
# bootstrap doc. Principle 0 inside principles.md (the Claude flavour)
# references this path via ``@.ctxr-fsm/memory/bootstrap.md`` so
# Claude's transitive ``@`` import can find the file. This staging
# applies to the CLAUDE install path only; Codex and Cursor inline
# the bootstrap body inside their principles adapter file (see
# ``tools/generate_memory_adapters.py``) because their LLMs do not
# follow ``@`` imports, so a staged copy under this path would be
# unreachable for them.
_BOOTSTRAP_LINKED_PATH: Path = (
    Path(".ctxr-fsm") / "memory" / BOOTSTRAP_FILENAME
)


def _ssot_relative_paths() -> dict[str, Path]:
    """Map each W23-SSOT slug to its staged relative path under the target.

    Each canonical reference doc (AGENT_QUICKSTART.md, SKILL_TEMPLATE.md,
    GATE_CONTRACT.md) is staged under ``.ctxr-fsm/memory/<filename>.md``
    so any sibling skill or agent in the workspace can resolve the doc
    via the same Claude ``@.ctxr-fsm/memory/<filename>.md`` import
    convention used by principles.md and bootstrap.md. The slug list
    comes from :func:`list_ssot_doc_slugs` so adding a new SSOT doc in
    one place (``ctxr/fsm/memory/__init__.py``) automatically flows
    through to staging + drift detection.

    Codex and Cursor do NOT follow ``@`` imports, so this staging
    applies only to the Claude install path. The Codex/Cursor adapters
    inline the bootstrap body (the only SSOT doc that ships as part of
    the principles contract); the broader SSOT reference docs are not
    inlined because they are reference material a sibling skill links
    to, not principles a single LLM session must internalise.
    """

    return {
        slug: Path(".ctxr-fsm") / "memory" / get_ssot_doc_path(slug).name
        for slug in list_ssot_doc_slugs()
    }


# The relative path Cursor uses for project-scoped rule files. We pick a
# stable filename (``ctxr-fsm.mdc``) so the rule is greppable and so
# repeated installs overwrite the same file rather than accumulate
# duplicates.
_CURSOR_RULE_PATH: Path = Path(".cursor") / "rules" / "ctxr-fsm.mdc"


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def _parse_version(text: str) -> str | None:
    """Return the ``version:`` value from the first YAML frontmatter block.

    Both the package-shipped principles files and the locally-patched
    CLAUDE/AGENTS marker blocks (via the ``v=`` attribute on the begin
    marker) carry a version. This helper handles the *file-level*
    frontmatter form — the ``v=`` attribute on a marker is handled
    separately by :func:`_find_existing_block`.

    The frontmatter format we accept is the standard one::

        ---
        name: ctxr-fsm-principles
        version: 0.1.0
        ...
        ---

    Returns ``None`` if the file has no recognisable frontmatter or no
    ``version:`` key — both cases mean "I don't know the version" and
    the caller should treat it as "unknown" rather than crash.
    """
    # The Cursor adapter has a Cursor-specific frontmatter block at the
    # very top, then a generated-from HTML comment, then the *canonical*
    # frontmatter with the version. So we scan ALL frontmatter blocks
    # in the file and return the first one that has a ``version:`` key.
    # Simpler than special-casing Cursor and equally correct for the
    # Claude / Codex / canonical files that only have one block.
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == "---":
            # Find the closing fence.
            j = i + 1
            while j < len(lines) and lines[j].strip() != "---":
                j += 1
            if j >= len(lines):
                # Unterminated frontmatter — give up rather than scan
                # the entire document for a key that probably isn't
                # there.
                return None
            for block_line in lines[i + 1 : j]:
                stripped = block_line.strip()
                # Match ``version: X.Y.Z`` (with optional quotes). Be
                # tolerant of the YAML ``key : value`` spacing variants.
                if stripped.startswith("version:"):
                    value = stripped.split(":", 1)[1].strip()
                    # Strip surrounding quotes if any — YAML allows
                    # ``version: "0.1.0"`` and ``version: 0.1.0``.
                    value = value.strip('"').strip("'")
                    if value:
                        return value
            # No version key in this block; advance past the closing
            # fence and keep scanning.
            i = j + 1
            continue
        i += 1
    return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClientDetection:
    """Result of auto-detecting one client in the target directory.

    ``host_file`` is the file that will be patched (or None for Cursor,
    where we write a standalone rule file). ``detected`` is True iff the
    client's signature was found in the target.
    """

    name: str
    detected: bool
    host_file: Path | None


def _detect_claude(target: Path) -> _ClientDetection:
    """Look for ``CLAUDE.md`` or ``.claude/CLAUDE.md`` in ``target``.

    We prefer the top-level ``CLAUDE.md`` if both exist (it is the
    canonical Claude Code location); ``.claude/CLAUDE.md`` is the
    nested form Claude also reads.
    """
    top = target / "CLAUDE.md"
    nested = target / ".claude" / "CLAUDE.md"
    if top.is_file():
        return _ClientDetection(name="claude", detected=True, host_file=top)
    if nested.is_file():
        return _ClientDetection(name="claude", detected=True, host_file=nested)
    return _ClientDetection(name="claude", detected=False, host_file=None)


def _detect_codex(target: Path) -> _ClientDetection:
    """Look for ``AGENTS.md`` in ``target``."""
    host = target / "AGENTS.md"
    if host.is_file():
        return _ClientDetection(name="codex", detected=True, host_file=host)
    return _ClientDetection(name="codex", detected=False, host_file=None)


def _detect_cursor(target: Path) -> _ClientDetection:
    """Look for a ``.cursor/rules/`` directory in ``target``.

    For Cursor, the host_file is the absolute path where the rule file
    will live — Cursor rules are standalone files, not appendices to a
    user-edited host file.
    """
    rules_dir = target / ".cursor" / "rules"
    if rules_dir.is_dir():
        return _ClientDetection(
            name="cursor",
            detected=True,
            host_file=target / _CURSOR_RULE_PATH,
        )
    return _ClientDetection(name="cursor", detected=False, host_file=None)


def _detect_clients(target: Path, client: str) -> list[_ClientDetection]:
    """Resolve ``--client`` into the concrete list of detections to act on.

    For ``auto`` we run every detector and return only the ones that
    fired. For an explicit client we return a single detection — for
    the explicit case we treat *unconditional install* as the contract,
    so ``detected`` is forced to True and ``host_file`` defaults to the
    canonical location even if it doesn't yet exist (the patcher will
    create it).
    """
    if client == "auto":
        detections = [
            _detect_claude(target),
            _detect_codex(target),
            _detect_cursor(target),
        ]
        live = [d for d in detections if d.detected]
        if live:
            return live
        # BLOCKER-3 (W14k): in a COLD project (no CLAUDE.md, no
        # AGENTS.md, no .cursor/rules) every detector returns False
        # and ``auto`` would historically be a no-op — leaving the
        # SKILL.md ``@.ctxr-fsm/memory/bootstrap.md`` reference dead
        # because nothing ever stages bootstrap.md alongside
        # principles.claude.md. Fall back to claude (the dominant
        # client for `ctxr-fsm ensure` callers) + bootstrap a fresh
        # CLAUDE.md at the canonical location. The patcher creates
        # the file if it doesn't exist; the user can rename / move it
        # afterward and re-running ``install-memory`` is idempotent.
        return [
            _ClientDetection(
                name="claude", detected=True, host_file=target / "CLAUDE.md"
            )
        ]

    if client == "claude":
        detected = _detect_claude(target)
        if detected.detected:
            return [detected]
        # Explicit client + no existing file -> default to top-level CLAUDE.md.
        return [
            _ClientDetection(
                name="claude", detected=True, host_file=target / "CLAUDE.md"
            )
        ]

    if client == "codex":
        detected = _detect_codex(target)
        if detected.detected:
            return [detected]
        return [
            _ClientDetection(
                name="codex", detected=True, host_file=target / "AGENTS.md"
            )
        ]

    if client == "cursor":
        detected = _detect_cursor(target)
        if detected.detected:
            return [detected]
        return [
            _ClientDetection(
                name="cursor",
                detected=True,
                host_file=target / _CURSOR_RULE_PATH,
            )
        ]

    raise typer.BadParameter(
        f"unknown client {client!r}; expected one of: {', '.join(_CLIENT_CHOICES)}"
    )


# ---------------------------------------------------------------------------
# Block construction
# ---------------------------------------------------------------------------


def _build_marker_block(version: str, payload: str) -> str:
    """Compose the begin/end-fenced block with ``payload`` inside.

    The payload may be multi-line. We ensure exactly one trailing
    newline INSIDE the block so the closing marker sits on its own
    line. The block returned has NO leading/trailing whitespace outside
    its own fences — the caller is responsible for placing it in the
    host file with the right surrounding blank line.
    """
    payload_normalised = payload.rstrip("\n")
    return (
        f"<!-- ctxr-fsm:begin v={version} -->\n"
        f"{payload_normalised}\n"
        f"{_MARKER_END}"
    )


def _find_existing_block(host_text: str) -> tuple[re.Match[str], str] | None:
    """Locate an existing marker block in ``host_text``.

    Returns ``(match, version)`` if a block is found, where ``match`` is
    the full :class:`re.Match` and ``version`` is the version string
    parsed out of the begin marker. Returns ``None`` if no block is
    present.
    """
    match = _MARKER_BLOCK_RE.search(host_text)
    if match is None:
        return None
    return match, match.group(1)


def _apply_patch(host_text: str, new_block: str) -> str:
    """Return ``host_text`` with the marker block replaced or appended.

    * If an existing block is present, replace it in-place (preserving
      everything before and after) — this is the idempotent path.
    * If no block exists, append the new block to the END of the file,
      separated by a single blank line. We preserve the file's existing
      trailing-newline contract (always end with exactly one newline
      after our block).
    """
    existing = _find_existing_block(host_text)
    if existing is not None:
        match, _ = existing
        return host_text[: match.start()] + new_block + host_text[match.end() :]

    if not host_text:
        # An empty host file: just write our block + trailing newline.
        return new_block + "\n"

    # Normalise the host's trailing whitespace so the appended block
    # sits cleanly under whatever was last in the file.
    base = host_text.rstrip("\n")
    return f"{base}\n\n{new_block}\n"


# ---------------------------------------------------------------------------
# Per-client patch builders
# ---------------------------------------------------------------------------


def _materialise_package_file(
    target: Path,
    package_file: Path,
    relative_dest: Path,
    *,
    no_symlink: bool,
) -> tuple[Path, str]:
    """Stage a package file under ``target/<relative_dest>`` (symlink or copy).

    Returns ``(absolute_destination, mode)`` where ``mode`` is one of
    ``"symlink"`` (we managed to symlink) or ``"copy"`` (we fell back to
    copying because symlinks were disabled, refused, or unsupported).

    The destination directory is created if missing. If a stale file
    already exists at the destination we replace it — the source of
    truth is always the package's source file.

    Generic over which file is being staged: the principles adapter
    AND the bootstrap doc both go through this helper so the
    symlink/copy semantics (including the Windows / FUSE fallback) stay
    in one place.
    """

    dest = target / relative_dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Idempotency early-exit: if the destination is already in the
    # desired state, return without touching it. This keeps inode
    # ctime/mtime stable across repeated install-memory runs, which
    # the no-rewrite test in this module enforces.
    #
    # Symlink-mode requested + dest is the right symlink: keep it.
    # Copy-mode requested + dest is a regular file with the same
    # bytes: keep it. Any other shape (wrong symlink target, file
    # when symlink was requested, symlink when copy was requested,
    # mismatched bytes) falls through and gets re-materialised so
    # the destination converges on the requested shape.
    desired_symlink_target = Path(package_file).resolve()
    if not no_symlink and dest.is_symlink():
        try:
            current_target: Path | None = dest.readlink()
        except OSError:
            current_target = None
        if current_target == desired_symlink_target:
            return dest, "symlink"
    elif no_symlink and dest.is_file() and not dest.is_symlink():
        try:
            same_bytes = dest.read_bytes() == package_file.read_bytes()
        except OSError:
            same_bytes = False
        if same_bytes:
            return dest, "copy"

    # Remove any pre-existing entry (file, symlink, or broken symlink)
    # before re-materialising. ``Path.unlink`` with ``missing_ok=True``
    # handles all three cleanly without an explicit ``exists()`` race.
    if dest.is_symlink() or dest.exists():
        dest.unlink(missing_ok=True)

    if no_symlink:
        shutil.copy2(package_file, dest)
        return dest, "copy"

    try:
        # We use an ABSOLUTE symlink because the package file lives
        # outside the project dir (it's inside the installed package),
        # so a relative path across that boundary would be cosmetically
        # ugly. Resolving here also makes the symlink stable across a
        # ``git mv`` of the project, the link always points back at
        # the installed package.
        dest.symlink_to(desired_symlink_target)
        return dest, "symlink"
    except (OSError, NotImplementedError):
        # Filesystem or platform doesn't support symlinks (Windows
        # without dev mode, some FUSE mounts), fall back to copy.
        shutil.copy2(package_file, dest)
        return dest, "copy"


def _materialise_claude_link(
    target: Path,
    package_file: Path,
    *,
    no_symlink: bool,
) -> tuple[Path, str]:
    """Place the Claude principles file under ``target/.ctxr-fsm/memory/``.

    Thin wrapper over :func:`_materialise_package_file` that pins the
    relative destination to the Claude principles path. Kept as a named
    helper so call sites read declaratively rather than threading the
    relative-destination constant through.
    """

    return _materialise_package_file(
        target,
        package_file,
        _CLAUDE_LINKED_PATH,
        no_symlink=no_symlink,
    )


def _materialise_bootstrap_doc(
    target: Path,
    *,
    no_symlink: bool,
) -> tuple[Path, str]:
    """Stage the canonical bootstrap doc under ``target/.ctxr-fsm/memory/``.

    Wraps :func:`_materialise_package_file` against the package's
    bootstrap source so every client install path stages the file
    consistently. The bootstrap doc has no per-client variant, so
    there's exactly one ``package_file`` to materialise.
    """

    return _materialise_package_file(
        target,
        get_bootstrap_path(),
        _BOOTSTRAP_LINKED_PATH,
        no_symlink=no_symlink,
    )


def _materialise_ssot_docs(
    target: Path,
    *,
    no_symlink: bool,
) -> dict[str, str]:
    """Stage every W23-SSOT canonical reference doc under ``target/.ctxr-fsm/memory/``.

    Iterates :func:`_ssot_relative_paths` and runs each doc through
    :func:`_materialise_package_file`. Returns a slug -> link_mode
    mapping (``"symlink"`` or ``"copy"``) for inclusion in the install
    summary. Applies to the Claude install path only — Codex and Cursor
    do not follow ``@`` imports, so staging these docs under
    ``.ctxr-fsm/memory/`` for them would create unreachable files.
    """

    modes: dict[str, str] = {}
    for slug, relative_dest in _ssot_relative_paths().items():
        _, mode = _materialise_package_file(
            target,
            get_ssot_doc_path(slug),
            relative_dest,
            no_symlink=no_symlink,
        )
        modes[slug] = mode
    return modes


def _build_claude_payload() -> str:
    """The payload for the CLAUDE.md marker block — a single ``@<path>`` import.

    We deliberately point at the in-project ``.ctxr-fsm/memory/...``
    location (set up by :func:`_materialise_claude_link`) rather than at
    the package file's absolute path; that keeps CLAUDE.md
    relocation-safe (you can move the project, share it via git, etc.)
    and matches Claude Code's documented ``@<relative-path>`` idiom.
    """
    return f"@{_CLAUDE_LINKED_PATH.as_posix()}"


def _build_codex_payload(package_text: str) -> str:
    """The payload for the AGENTS.md marker block — the inlined principles.

    Codex doesn't support an ``@<path>`` import, so we inline the full
    body. We strip the file's outer frontmatter / generated-from header
    because Codex doesn't need the YAML frontmatter (it's not a Cursor
    rule file) — we keep the inlined content focused on the *content*
    the agent needs to read. The generated-from comment stays as it
    serves as a useful breadcrumb back to the package source.
    """
    return package_text.rstrip("\n")


# ---------------------------------------------------------------------------
# Per-client install actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InstallResult:
    """Per-client outcome surfaced in the JSON / pretty summary."""

    client: str
    host_file: Path
    action: str  # "wrote" | "noop" | "dry-run" | "would-write"
    package_version: str | None
    installed_version: str | None
    link_mode: str | None  # "symlink" | "copy" | None
    # ``bootstrap_link_mode`` records how ``.ctxr-fsm/memory/bootstrap.md``
    # was staged for this client (symlink / copy / None). The
    # bootstrap doc is staged ONLY for Claude — its ``@`` import in
    # Principle 0 needs a real file on disk. Codex and Cursor inline
    # the bootstrap body inside their principles adapter (see
    # ``tools/generate_memory_adapters.py``) and report ``None``
    # here because no separate file is materialised.
    bootstrap_link_mode: str | None = None
    # ``ssot_link_modes`` records how each W23-SSOT canonical reference
    # doc was staged for this client (slug -> ``"symlink"`` / ``"copy"``).
    # Populated on Claude in every code path: real installs report the
    # per-slug materialisation mode, and the dry-run path mirrors what
    # would be written (one entry per slug at the implied mode).
    # ``None`` on Codex / Cursor: those clients do not follow ``@``
    # imports and the SSOT docs are reference material, not adapter
    # content, so we do not inline them either. Drift on these docs is
    # hash-detected on the Claude row via
    # :func:`_check_ssot_statuses` (mirrors the bootstrap.md check).
    ssot_link_modes: dict[str, str] | None = None
    note: str = ""


def _install_claude(
    *,
    target: Path,
    host_file: Path,
    package_file: Path,
    package_version: str | None,
    dry_run: bool,
    no_symlink: bool,
) -> _InstallResult:
    """Idempotent install for Claude: symlink + marker block in CLAUDE.md.

    The marker block injected into ``CLAUDE.md`` only imports
    ``principles.<client>.md`` via Claude's ``@<path>`` syntax. Principle
    0 inside principles.md references ``@.ctxr-fsm/memory/bootstrap.md``
    in turn, so a separate ``@`` import for the bootstrap doc in the
    marker block would be redundant — Claude's transitive import
    resolves it through the principles file. We stage bootstrap.md
    alongside the principles file so that import has something to
    resolve to. Bootstrap-file staging is CLAUDE-ONLY: Codex and
    Cursor inline the bootstrap body into their principles adapter
    instead because their LLMs do not follow ``@`` imports.
    """
    if dry_run:
        # In dry-run mode we don't materialise either symlink — just show
        # what the patch would look like. The link mode is reported as
        # whatever ``no_symlink`` implies so the dry-run output matches
        # what would actually happen.
        link_mode = "copy" if no_symlink else "symlink"
        existing_text = host_file.read_text(encoding="utf-8") if host_file.is_file() else ""
        new_block = _build_marker_block(
            package_version or "unknown", _build_claude_payload()
        )
        new_text = _apply_patch(existing_text, new_block)
        installed_version = (
            _read_installed_version_from_text(existing_text) if existing_text else None
        )
        return _InstallResult(
            client="claude",
            host_file=host_file,
            action="dry-run",
            package_version=package_version,
            installed_version=installed_version,
            link_mode=link_mode,
            bootstrap_link_mode=link_mode,
            ssot_link_modes={slug: link_mode for slug in list_ssot_doc_slugs()},
            note=_diff_preview(existing_text, new_text),
        )

    _, link_mode = _materialise_claude_link(target, package_file, no_symlink=no_symlink)
    _, bootstrap_link_mode = _materialise_bootstrap_doc(target, no_symlink=no_symlink)
    ssot_link_modes = _materialise_ssot_docs(target, no_symlink=no_symlink)

    existing_text = host_file.read_text(encoding="utf-8") if host_file.is_file() else ""
    new_block = _build_marker_block(
        package_version or "unknown", _build_claude_payload()
    )
    new_text = _apply_patch(existing_text, new_block)

    if new_text == existing_text and host_file.is_file():
        return _InstallResult(
            client="claude",
            host_file=host_file,
            action="noop",
            package_version=package_version,
            installed_version=package_version,
            link_mode=link_mode,
            bootstrap_link_mode=bootstrap_link_mode,
            ssot_link_modes=ssot_link_modes,
        )

    host_file.parent.mkdir(parents=True, exist_ok=True)
    host_file.write_text(new_text, encoding="utf-8")
    return _InstallResult(
        client="claude",
        host_file=host_file,
        action="wrote",
        package_version=package_version,
        installed_version=package_version,
        link_mode=link_mode,
        bootstrap_link_mode=bootstrap_link_mode,
        ssot_link_modes=ssot_link_modes,
    )


def _install_codex(
    *,
    target: Path,
    host_file: Path,
    package_file: Path,
    package_version: str | None,
    dry_run: bool,
    no_symlink: bool,
) -> _InstallResult:
    """Idempotent install for Codex: inline the principles into AGENTS.md.

    Codex doesn't natively follow Claude's ``@<path>`` import syntax,
    so the principles body is inlined directly. The codex flavour of
    the principles adapter ALREADY inlines the bootstrap body inside
    Principle 0 (see ``tools/generate_memory_adapters.py``), so this
    install path does NOT stage a separate ``bootstrap.md`` under
    ``.ctxr-fsm/memory/`` — Codex's LLM cannot reach it through an
    ``@`` import anyway. ``bootstrap_link_mode`` is therefore always
    ``None`` for codex: drift detection for the bootstrap content
    happens via the principles-adapter version comparison instead.

    ``target`` and ``no_symlink`` are accepted for signature
    parity with the other ``_install_*`` helpers (the call site in
    :func:`run_install_memory` dispatches uniformly) but are unused
    for Codex.
    """
    del target, no_symlink  # unused — see docstring
    package_text = package_file.read_text(encoding="utf-8")
    existing_text = host_file.read_text(encoding="utf-8") if host_file.is_file() else ""

    new_block = _build_marker_block(
        package_version or "unknown", _build_codex_payload(package_text)
    )
    new_text = _apply_patch(existing_text, new_block)

    if dry_run:
        return _InstallResult(
            client="codex",
            host_file=host_file,
            action="dry-run",
            package_version=package_version,
            installed_version=_read_installed_version_from_text(existing_text)
            if existing_text
            else None,
            link_mode=None,
            bootstrap_link_mode=None,
            note=_diff_preview(existing_text, new_text),
        )

    if new_text == existing_text and host_file.is_file():
        return _InstallResult(
            client="codex",
            host_file=host_file,
            action="noop",
            package_version=package_version,
            installed_version=package_version,
            link_mode=None,
            bootstrap_link_mode=None,
        )

    host_file.parent.mkdir(parents=True, exist_ok=True)
    host_file.write_text(new_text, encoding="utf-8")
    return _InstallResult(
        client="codex",
        host_file=host_file,
        action="wrote",
        package_version=package_version,
        installed_version=package_version,
        link_mode=None,
        bootstrap_link_mode=None,
    )


def _install_cursor(
    *,
    target: Path,
    host_file: Path,
    package_file: Path,
    package_version: str | None,
    dry_run: bool,
    no_symlink: bool,
) -> _InstallResult:
    """Idempotent install for Cursor: write the standalone .mdc rule file.

    Cursor rule files live under ``.cursor/rules/`` and are evaluated
    standalone (no marker / no inlined-import contract). Bootstrap
    content is inlined into the .cursor rule file via the codex /
    cursor adapter (see ``tools/generate_memory_adapters.py``); we
    don't stage a separate ``bootstrap.md`` here because Cursor
    doesn't follow ``@`` imports. ``bootstrap_link_mode`` is always
    ``None`` for cursor: drift in the bootstrap content surfaces via
    the cursor adapter's content hash (the rule file itself is the
    source of truth).

    ``target`` and ``no_symlink`` are accepted for signature parity
    with the other ``_install_*`` helpers but are unused for Cursor.
    """
    del target, no_symlink  # unused — see docstring
    package_bytes = package_file.read_bytes()
    existing_bytes = host_file.read_bytes() if host_file.is_file() else b""

    if dry_run:
        installed_version: str | None = None
        if existing_bytes:
            installed_version = _parse_version(
                existing_bytes.decode("utf-8", errors="replace")
            )
        return _InstallResult(
            client="cursor",
            host_file=host_file,
            action="dry-run",
            package_version=package_version,
            installed_version=installed_version,
            link_mode=None,
            bootstrap_link_mode=None,
            note=f"would write {len(package_bytes)} bytes to {host_file}",
        )

    if existing_bytes == package_bytes:
        return _InstallResult(
            client="cursor",
            host_file=host_file,
            action="noop",
            package_version=package_version,
            installed_version=package_version,
            link_mode=None,
            bootstrap_link_mode=None,
        )

    host_file.parent.mkdir(parents=True, exist_ok=True)
    host_file.write_bytes(package_bytes)
    return _InstallResult(
        client="cursor",
        host_file=host_file,
        action="wrote",
        package_version=package_version,
        installed_version=package_version,
        link_mode=None,
        bootstrap_link_mode=None,
    )


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------


def _read_installed_version_from_text(text: str) -> str | None:
    """Pick the installed version out of a CLAUDE/AGENTS-style host file.

    First we look for a marker block (the ``v=`` attribute on the begin
    marker is the authoritative pin), then we fall back to scanning the
    file's frontmatter blocks via :func:`_parse_version` — that fallback
    covers Cursor's standalone rule file, where there is no marker
    block.
    """
    existing = _find_existing_block(text)
    if existing is not None:
        return existing[1]
    return _parse_version(text)


@dataclass(frozen=True)
class _CheckRow:
    """One row in the per-client ``--check`` status table.

    :attr:`status` and :attr:`installed_version` describe the
    principles file's drift status (parsed from the marker block's
    ``v=`` attribute or the cursor rule frontmatter).
    :attr:`bootstrap_status` describes the drift status of the
    bootstrap content for THIS client:

    * For Claude, the staged ``.ctxr-fsm/memory/bootstrap.md`` copy is
      hash-compared against the package source — bootstrap.md has no
      frontmatter version, so we compare content hashes.
    * For Codex and Cursor, the bootstrap content lives inlined inside
      the principles adapter file. Drift in that content shows up as
      drift in the principles version (the adapter regenerates when
      bootstrap.md changes, bumping the principles file's bytes), so
      :attr:`bootstrap_status` is :attr:`BootstrapStatus.inlined` —
      "checked elsewhere, not staged as a separate file".

    A client is considered out-of-date overall when EITHER axis is
    out of date or missing (where applicable — ``inlined`` is
    never a failure on its own).
    """

    client: str
    host_file: Path | None
    package_version: str | None
    installed_version: str | None
    status: MemoryInstallStatus
    bootstrap_status: BootstrapStatus = BootstrapStatus.not_installed
    # ``ssot_statuses`` maps each W23-SSOT slug to a
    # :class:`BootstrapStatus` (the same enum reused: ok / out_of_date /
    # not_installed for Claude, inlined for codex / cursor where the
    # docs are reference material reached through Claude's @ import
    # alone). Drift on any SSOT doc is treated the same way as
    # bootstrap drift for exit-code purposes.
    ssot_statuses: dict[str, BootstrapStatus] | None = None


def _sha256_file(path: Path) -> str:
    """Return the lowercase hex sha256 of ``path``'s bytes."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_bootstrap_status(
    target: Path, package_bootstrap_hash: str
) -> BootstrapStatus:
    """Compare the staged bootstrap copy under ``target`` against the package.

    Bootstrap.md ships without a frontmatter version (it's a procedural
    doc, not a versioned policy), so principles-style version-marker
    comparison doesn't apply. We hash the staged file's bytes and
    compare against the package source's hash, which is exactly what
    drift detection needs: any change to either file shows up as a
    hash mismatch. Applies ONLY to clients that stage bootstrap.md
    (i.e. Claude); codex/cursor consume the bootstrap content through
    the inlined block in their principles adapter and don't have a
    staged file to compare against.

    Returns:
        :attr:`BootstrapStatus.ok`            — staged file exists and
            content matches the package.
        :attr:`BootstrapStatus.out_of_date`   — staged file exists but
            content differs.
        :attr:`BootstrapStatus.not_installed` — staged file is absent.
    """

    staged = target / _BOOTSTRAP_LINKED_PATH
    if not staged.is_file():
        return BootstrapStatus.not_installed

    if _sha256_file(staged) == package_bootstrap_hash:
        return BootstrapStatus.ok
    return BootstrapStatus.out_of_date


def _check_ssot_statuses(
    target: Path, package_ssot_hashes: dict[str, str]
) -> dict[str, BootstrapStatus]:
    """Per-slug drift check for the staged W23-SSOT canonical reference docs.

    Mirrors :func:`_check_bootstrap_status` but iterates each SSOT slug:
    compare the staged file's sha256 against the package source. Slugs
    whose staged file is absent flip to
    :attr:`BootstrapStatus.not_installed`; bytes-differ slugs flip to
    :attr:`BootstrapStatus.out_of_date`. Applies to the Claude install
    path only (the only path that stages the SSOT docs as separate
    files on disk).
    """

    staged_paths = _ssot_relative_paths()
    out: dict[str, BootstrapStatus] = {}
    for slug, relative in staged_paths.items():
        staged = target / relative
        if not staged.is_file():
            out[slug] = BootstrapStatus.not_installed
            continue
        if _sha256_file(staged) == package_ssot_hashes[slug]:
            out[slug] = BootstrapStatus.ok
        else:
            out[slug] = BootstrapStatus.out_of_date
    return out


def _check_one(
    target: Path,
    detection: _ClientDetection,
    package_version: str | None,
    package_bootstrap_hash: str,
    package_ssot_hashes: dict[str, str],
) -> _CheckRow:
    """Compute the status for one detected client.

    For Claude the bootstrap-axis status reflects the staged copy's
    hash against the package source. For Codex and Cursor we return
    :attr:`BootstrapStatus.inlined`: the bootstrap content lives inside
    the principles adapter for those clients, so drift in bootstrap
    content surfaces via the principles version comparison rather
    than a separate hash check.

    SSOT docs follow the same shape: Claude gets per-slug hash drift
    via :func:`_check_ssot_statuses`; Codex / Cursor get the
    :attr:`BootstrapStatus.inlined` sentinel uniformly across every
    slug (the reference docs are reachable only through Claude's
    ``@`` import — not inlined into the adapter — but for drift
    purposes they are treated like the bootstrap doc, with drift
    detected only on the staged-file client).
    """
    host = detection.host_file
    if detection.name == McpClient.claude.value:
        bootstrap_status = _check_bootstrap_status(target, package_bootstrap_hash)
        ssot_statuses = _check_ssot_statuses(target, package_ssot_hashes)
    else:
        bootstrap_status = BootstrapStatus.inlined
        ssot_statuses = {slug: BootstrapStatus.inlined for slug in package_ssot_hashes}

    if host is None or not host.is_file():
        return _CheckRow(
            client=detection.name,
            host_file=host,
            package_version=package_version,
            installed_version=None,
            status=MemoryInstallStatus.not_installed,
            bootstrap_status=bootstrap_status,
            ssot_statuses=ssot_statuses,
        )

    installed = _read_installed_version_from_text(
        host.read_text(encoding="utf-8")
    )

    if installed is None:
        return _CheckRow(
            client=detection.name,
            host_file=host,
            package_version=package_version,
            installed_version=None,
            status=MemoryInstallStatus.missing,
            bootstrap_status=bootstrap_status,
            ssot_statuses=ssot_statuses,
        )
    if installed == package_version:
        return _CheckRow(
            client=detection.name,
            host_file=host,
            package_version=package_version,
            installed_version=installed,
            status=MemoryInstallStatus.ok,
            bootstrap_status=bootstrap_status,
            ssot_statuses=ssot_statuses,
        )
    return _CheckRow(
        client=detection.name,
        host_file=host,
        package_version=package_version,
        installed_version=installed,
        status=MemoryInstallStatus.out_of_date,
        bootstrap_status=bootstrap_status,
        ssot_statuses=ssot_statuses,
    )


# ---------------------------------------------------------------------------
# Dry-run preview helper
# ---------------------------------------------------------------------------


def _diff_preview(old: str, new: str) -> str:
    """Compose a short, human-friendly summary of the patch.

    We deliberately don't try to produce a unified diff (the printable
    surface stays small and predictable across very different file
    shapes). Just report whether the file would be created / replaced /
    appended and the size delta.
    """
    if not old:
        return f"would create file (+{len(new)} bytes)"
    if _find_existing_block(old) is not None:
        return f"would replace existing block (delta {len(new) - len(old):+d} bytes)"
    return f"would append new block (delta {len(new) - len(old):+d} bytes)"


# ---------------------------------------------------------------------------
# Pure entry point — used by both the Typer command and ``ctxr-fsm init``
# ---------------------------------------------------------------------------


def run_install_memory(
    *,
    target: Path,
    client: str = "auto",
    dry_run: bool = False,
    no_symlink: bool = False,
) -> dict[str, Any]:
    """Run the install (or no-op) and return a JSON-serialisable summary.

    Intentionally does NOT print, so callers that compose this
    function's output into their own report (eg. ``ctxr-fsm init``) can
    do so without polluting stdout. The Typer command :func:`install_memory`
    is a thin wrapper that calls this then prints the result.

    Raises :class:`typer.BadParameter` for an unknown client name (same
    behaviour as the CLI surface).
    """
    if client not in _CLIENT_CHOICES:
        raise typer.BadParameter(
            f"--client must be one of {', '.join(_CLIENT_CHOICES)}, got {client!r}"
        )

    target = target.resolve()
    detections = _detect_clients(target, client)

    if not detections:
        return {
            "target": str(target),
            "client": client,
            "detected": [],
            "message": (
                "no AI-client memory files found "
                "(looked for CLAUDE.md, .claude/CLAUDE.md, AGENTS.md, .cursor/rules/)"
            ),
        }

    canonical_package_file = get_principles_path("canonical")
    package_version = _parse_version(
        canonical_package_file.read_text(encoding="utf-8")
    )

    results: list[_InstallResult] = []
    for detection in detections:
        package_file = get_principles_path(detection.name)
        assert detection.host_file is not None

        if detection.name == "claude":
            results.append(
                _install_claude(
                    target=target,
                    host_file=detection.host_file,
                    package_file=package_file,
                    package_version=package_version,
                    dry_run=dry_run,
                    no_symlink=no_symlink,
                )
            )
        elif detection.name == "codex":
            results.append(
                _install_codex(
                    target=target,
                    host_file=detection.host_file,
                    package_file=package_file,
                    package_version=package_version,
                    dry_run=dry_run,
                    no_symlink=no_symlink,
                )
            )
        elif detection.name == "cursor":
            results.append(
                _install_cursor(
                    target=target,
                    host_file=detection.host_file,
                    package_file=package_file,
                    package_version=package_version,
                    dry_run=dry_run,
                    no_symlink=no_symlink,
                )
            )
        else:  # pragma: no cover — guarded by _detect_clients
            raise typer.BadParameter(f"unknown client {detection.name!r}")

    return {
        "target": str(target),
        "package_version": package_version,
        "dry_run": dry_run,
        "results": [
            {
                "client": r.client,
                "host_file": str(r.host_file),
                "action": r.action,
                "package_version": r.package_version,
                "installed_version": r.installed_version,
                "link_mode": r.link_mode,
                "bootstrap_link_mode": r.bootstrap_link_mode,
                "ssot_link_modes": r.ssot_link_modes,
                "note": r.note,
            }
            for r in results
        ],
    }


# ---------------------------------------------------------------------------
# Typer entry point
# ---------------------------------------------------------------------------


def install_memory(
    target: Path | None = typer.Option(  # noqa: B008 — typer sentinel, not a value
        None,
        "--target",
        help=(
            "Directory containing the AI-client memory files. "
            "Defaults to the current working directory. Mostly for tests."
        ),
        resolve_path=True,
    ),
    client: str = typer.Option(
        "auto",
        "--client",
        help=(
            "Which AI-client memory to patch: 'auto' (detect), 'claude', "
            "'codex', or 'cursor'. 'auto' patches every client detected in "
            "TARGET in a single pass."
        ),
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help=(
            "Don't write — compare installed version against package version "
            "for each detected client and print a status table. Exits "
            "non-zero if any client is out of date or missing."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print the patch that would be applied for each detected client "
            "without writing to disk."
        ),
    ),
    no_symlink: bool = typer.Option(
        False,
        "--no-symlink",
        help=(
            "Always copy Claude's staged files (the principles adapter and "
            "the bootstrap doc) under TARGET/.ctxr-fsm/memory/ instead of "
            "trying to symlink first. Default tries symlink and falls back "
            "to copy on failure (Windows without dev-mode, some FUSE mounts). "
            "No-op for Codex and Cursor (nothing is staged for those clients)."
        ),
    ),
    json_mode: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of pretty-printed output.",
    ),
) -> None:
    """Install (or check) ctxr-fsm FSM-usage principles into AI-client memory.

    See module docstring for the full per-client behaviour, marker
    format, and idempotence guarantees.
    """
    if client not in _CLIENT_CHOICES:
        raise typer.BadParameter(
            f"--client must be one of {', '.join(_CLIENT_CHOICES)}, got {client!r}"
        )

    # Resolve ``--target`` lazily so the default reflects the cwd at
    # *invocation* time, not the cwd at module import (which is what a
    # ``Path.cwd()`` default on the Typer ``Option`` would freeze).
    resolved_target: Path = (target if target is not None else Path.cwd()).resolve()

    if check:
        # --check has its own report shape (per-client status rows) and
        # an explicit non-zero exit when anything is out of date — keep
        # its logic local rather than threading another mode through
        # ``run_install_memory``.
        detections = _detect_clients(resolved_target, client)
        if not detections:
            json_or_pretty(
                {
                    "target": str(resolved_target),
                    "client": client,
                    "detected": [],
                    "message": (
                        "no AI-client memory files found "
                        "(looked for CLAUDE.md, .claude/CLAUDE.md, AGENTS.md, "
                        ".cursor/rules/)"
                    ),
                },
                json_mode,
            )
            return
        canonical_package_file = get_principles_path("canonical")
        package_version = _parse_version(
            canonical_package_file.read_text(encoding="utf-8")
        )
        package_bootstrap_hash = _sha256_file(get_bootstrap_path())
        package_ssot_hashes = {
            slug: _sha256_file(get_ssot_doc_path(slug))
            for slug in list_ssot_doc_slugs()
        }
        rows = [
            _check_one(
                resolved_target,
                d,
                package_version,
                package_bootstrap_hash,
                package_ssot_hashes,
            )
            for d in detections
        ]
        # Drift on EITHER axis triggers a non-zero exit so CI can
        # fail fast. The principles axis applies to every client; the
        # bootstrap axis is only meaningful when bootstrap.md is
        # staged separately (Claude). For codex/cursor the bootstrap
        # content is inlined into the principles adapter, so its
        # drift is already detected by the principles version check
        # plus the adapter-regeneration step; ``bootstrap_status``
        # is ``"inlined"`` there and is NOT treated as a failure on
        # its own.
        memory_fail_statuses = {
            MemoryInstallStatus.out_of_date,
            MemoryInstallStatus.missing,
        }
        bootstrap_fail_statuses = {
            BootstrapStatus.out_of_date,
            BootstrapStatus.not_installed,
        }

        def _any_ssot_drift(row: _CheckRow) -> bool:
            """Return True iff any staged SSOT doc is drifted or absent.

            The ``inlined`` sentinel (codex / cursor) is never a failure
            on its own — for those clients the SSOT axis is checked
            elsewhere via the principles version. Only the per-slug
            ``out_of_date`` / ``not_installed`` statuses count as drift.
            """

            if not row.ssot_statuses:
                return False
            return any(
                status in bootstrap_fail_statuses
                for status in row.ssot_statuses.values()
            )

        out_of_date = [
            r
            for r in rows
            if r.status in memory_fail_statuses
            or r.bootstrap_status in bootstrap_fail_statuses
            or _any_ssot_drift(r)
        ]
        json_or_pretty(
            {
                "target": str(resolved_target),
                "package_version": package_version,
                "results": [
                    {
                        "client": r.client,
                        "host_file": str(r.host_file) if r.host_file else None,
                        "package_version": r.package_version,
                        "installed_version": r.installed_version,
                        "status": r.status.value,
                        "bootstrap_status": r.bootstrap_status.value,
                        "ssot_statuses": {
                            slug: status.value
                            for slug, status in (r.ssot_statuses or {}).items()
                        },
                    }
                    for r in rows
                ],
            },
            json_mode,
        )
        if out_of_date:
            raise typer.Exit(1)
        return

    summary = run_install_memory(
        target=resolved_target,
        client=client,
        dry_run=dry_run,
        no_symlink=no_symlink,
    )
    json_or_pretty(summary, json_mode)
