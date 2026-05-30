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
  is inlined between fence markers at the END of ``AGENTS.md``.

* **Cursor** (``.cursor/rules/ctxr-fsm.mdc``) — Cursor's rule files are
  standalone, so we write the principles file directly with no marker
  block. The rule filename itself is the namespace.

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

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from ctxr.fsm.cli._common import json_or_pretty
from ctxr.fsm.memory import get_principles_path

__all__ = ["install_memory", "run_install_memory", "run_install_memory_check"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The set of client identifiers the command understands. ``auto`` is a
# meta-value that fans out to the detected clients in the target dir.
_CLIENT_CHOICES: tuple[str, ...] = ("auto", "claude", "codex", "cursor")

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
        return [d for d in detections if d.detected]

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


def _materialise_claude_link(
    target: Path,
    package_file: Path,
    *,
    no_symlink: bool,
) -> tuple[Path, str]:
    """Place the Claude principles file under ``target/.ctxr-fsm/memory/``.

    Returns ``(absolute_destination, mode)`` where ``mode`` is one of
    ``"symlink"`` (we managed to symlink) or ``"copy"`` (we fell back to
    copying because symlinks were disabled, refused, or unsupported).

    The destination directory is created if missing. If a stale file
    already exists at the destination we replace it — the source of
    truth is always the package's principles file.
    """
    dest = target / _CLAUDE_LINKED_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Remove any pre-existing entry (file, symlink, or broken symlink)
    # before re-materialising. ``Path.unlink`` with ``missing_ok=True``
    # handles all three cleanly without an explicit ``exists()`` race.
    if dest.is_symlink() or dest.exists():
        dest.unlink(missing_ok=True)

    if no_symlink:
        shutil.copy2(package_file, dest)
        return dest, "copy"

    try:
        # We use a RELATIVE symlink so a tarball / git move of the
        # project directory still resolves correctly. The relative
        # target is computed from the destination's parent.
        rel_target = Path(package_file).resolve()
        # ``os.path.relpath`` would be the obvious helper but it
        # produces strings; we want a Path. The principles file lives
        # outside the project dir (it's inside the installed package),
        # so an absolute symlink is fine and unambiguous — relpath
        # across that boundary would be cosmetically ugly.
        dest.symlink_to(rel_target)
        return dest, "symlink"
    except (OSError, NotImplementedError):
        # Filesystem or platform doesn't support symlinks (Windows
        # without dev mode, some FUSE mounts) — fall back to copy.
        shutil.copy2(package_file, dest)
        return dest, "copy"


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
    """Idempotent install for Claude: symlink + marker block in CLAUDE.md."""
    if dry_run:
        # In dry-run mode we don't materialise the symlink — just show
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
            note=_diff_preview(existing_text, new_text),
        )

    _, link_mode = _materialise_claude_link(target, package_file, no_symlink=no_symlink)

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
    )


def _install_codex(
    *,
    host_file: Path,
    package_file: Path,
    package_version: str | None,
    dry_run: bool,
) -> _InstallResult:
    """Idempotent install for Codex: inline the principles into AGENTS.md."""
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
    )


def _install_cursor(
    *,
    host_file: Path,
    package_file: Path,
    package_version: str | None,
    dry_run: bool,
) -> _InstallResult:
    """Idempotent install for Cursor: write the standalone .mdc rule file."""
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
    """One row in the per-client ``--check`` status table."""

    client: str
    host_file: Path | None
    package_version: str | None
    installed_version: str | None
    status: str  # "ok" | "out-of-date" | "missing" | "not-installed"


def _check_one(
    detection: _ClientDetection, package_version: str | None
) -> _CheckRow:
    """Compute the status for one detected client."""
    host = detection.host_file
    if host is None or not host.is_file():
        return _CheckRow(
            client=detection.name,
            host_file=host,
            package_version=package_version,
            installed_version=None,
            status="not-installed",
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
            status="missing",
        )
    if installed == package_version:
        return _CheckRow(
            client=detection.name,
            host_file=host,
            package_version=package_version,
            installed_version=installed,
            status="ok",
        )
    return _CheckRow(
        client=detection.name,
        host_file=host,
        package_version=package_version,
        installed_version=installed,
        status="out-of-date",
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


def run_install_memory_check(
    *,
    target: Path,
    client: str = "auto",
) -> dict[str, Any]:
    """Programmatic ``install-memory --check`` probe.

    Pure function returning a JSON-serialisable summary; intentionally
    does NOT print, raise on drift, or call ``typer.Exit``. Callers
    (notably ``ctxr-fsm ensure --check``) decide how to surface drift
    by looking at each row's ``status`` field
    (``"ok"`` / ``"out-of-date"`` / ``"missing"`` / ``"not-installed"``).
    The Typer command :func:`install_memory` wraps this then prints
    and exits non-zero when any row is out-of-date.

    Returns ``{"target", "package_version", "results": [...]}``;
    each result row has ``client``, ``host_file``, ``package_version``,
    ``installed_version``, ``status``. When no clients are detected the
    summary uses the same ``{"detected": [], "message": ...}`` shape
    as :func:`run_install_memory`.

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
                "(looked for CLAUDE.md, .claude/CLAUDE.md, AGENTS.md, "
                ".cursor/rules/)"
            ),
        }

    canonical_package_file = get_principles_path("canonical")
    package_version = _parse_version(
        canonical_package_file.read_text(encoding="utf-8")
    )
    rows = [_check_one(d, package_version) for d in detections]
    return {
        "target": str(target),
        "package_version": package_version,
        "results": [
            {
                "client": r.client,
                "host_file": str(r.host_file) if r.host_file else None,
                "package_version": r.package_version,
                "installed_version": r.installed_version,
                "status": r.status,
            }
            for r in rows
        ],
    }


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
                    host_file=detection.host_file,
                    package_file=package_file,
                    package_version=package_version,
                    dry_run=dry_run,
                )
            )
        elif detection.name == "cursor":
            results.append(
                _install_cursor(
                    host_file=detection.host_file,
                    package_file=package_file,
                    package_version=package_version,
                    dry_run=dry_run,
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
            "For Claude, always copy the principles file under "
            "TARGET/.ctxr-fsm/memory/ instead of trying to symlink first. "
            "Default tries symlink and falls back to copy on failure."
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
        # Delegate to the programmatic probe so ``ctxr-fsm ensure --check``
        # can call the same code path without going through Typer.
        summary = run_install_memory_check(target=resolved_target, client=client)
        json_or_pretty(summary, json_mode)
        results = summary.get("results", [])
        out_of_date = [
            r for r in results
            if r.get("status") in ("out-of-date", "missing")
        ]
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
