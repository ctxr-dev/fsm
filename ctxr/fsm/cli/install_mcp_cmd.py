"""``ctxr-fsm install-mcp`` — register ctxr-fsm as an MCP server in client configs.

This command wires the ctxr-fsm stdio MCP entry into whichever AI-client
config files we can find:

* **Claude Code project-local** — ``<target>/.mcp.json`` (the
  workspace-scoped MCP server map) and/or
  ``<target>/.claude/settings.json`` (the project-scoped settings
  block with an ``mcpServers`` key). Both are JSON and we only OWN
  the ``mcpServers.ctxr-fsm`` key — every other top-level key and
  every other server in ``mcpServers`` passes through unmodified.
* **Codex user-level** — ``~/.codex/config.toml`` ``[mcp_servers.ctxr-fsm]``
  table. We prefer ``codex mcp add`` when the ``codex`` binary is on
  PATH; otherwise we hand-edit the TOML directly. Our table is the
  only one we touch.
* **Cursor user-level** — ``~/.cursor/mcp.json`` ``mcpServers.ctxr-fsm``.

The stdio entry written is the same in every shape:

* JSON: ``{"command":"ctxr-fsm","args":["mcp","--transport","stdio"],"env":{}}``
* TOML: ``command = "ctxr-fsm"\\nargs = ["mcp", "--transport", "stdio"]``

The MCP server learns its DB path at startup by walking up from the
inherited cwd looking for ``.ctxr-fsm/`` (see ``ctxr.fsm.cli.mcp_cmd``
— that walk-up is what makes the stdio entry portable across
projects).

Modes
-----

* ``run_install_mcp(target_dir, client="auto")`` — apply patches.
* ``run_install_mcp(target_dir, client=..., check=True)`` — read-only
  probe; returns ``status: installed|missing|out-of-date`` per detected
  client.
* ``run_install_mcp(target_dir, client=..., dry_run=True)`` — describe
  the patches that would be written; no filesystem mutation.

Idempotency
-----------

* If the existing on-disk file already contains an entry deep-equal to
  ours, we do not rewrite the file at all (``action="unchanged"``).
* Otherwise the merge preserves every other key verbatim, writes via
  tmp+rename, and indents output to match the file's existing style
  (2-space / 4-space / tab; defaults to 2-space when undetermined or
  empty).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

from ctxr.fsm.cli._clients import McpClient, McpConfigStatus
from ctxr.fsm.cli._common import json_or_pretty

__all__ = ["install_mcp", "run_install_mcp"]


# ---------------------------------------------------------------------------
# Constants — entry shape + supported clients
# ---------------------------------------------------------------------------

# Allowed values for the ``--client`` flag. ``auto`` is a meta-value
# that fans out to every detected client. ``none`` short-circuits to a
# no-op — useful for ``ctxr-fsm ensure --no-mcp-config`` plumbing.
# The tuple form is kept for backward-compatible callers that grep on
# the constant; the enum is the canonical source.
_CLIENT_CHOICES: tuple[str, ...] = tuple(member.value for member in McpClient)

# The MCP entry's logical name as it appears in every client config.
# Kept as a constant so a rename only touches one place + every
# matching test can grep for the same literal.
_ENTRY_NAME: str = "ctxr-fsm"


def _resolve_stdio_entry() -> dict[str, Any]:
    """Resolve the stdio MCP entry shape for the current invocation.

    Cross-client bootstrap regression: writing the bare literal
    ``command="ctxr-fsm"`` made the registered entry unreachable for
    operators whose shell PATH did not pick up the project venv's
    console scripts (the most common case: an operator running
    ``uv run ctxr-fsm install-mcp`` inside a uv-managed project; the
    venv's ``.venv/bin/ctxr-fsm`` is on PATH only for the duration of
    that single ``uv run`` call, never for the long-running client
    process that later spawns the stdio MCP).

    Resolution order:

    1. If we were invoked via ``uv run`` (detected by a co-located
       ``VIRTUAL_ENV`` pointing at the same dist-info we are running
       from, plus ``sys.executable`` living inside that venv), write
       ``command="uv"`` + ``args=["run", "ctxr-fsm", "mcp",
       "--transport", "stdio"]``. ``uv run`` re-resolves the venv at
       spawn time, so any client (Claude Code, Codex, Cursor) that
       launches with a different cwd / PATH still ends up running the
       correct binary against the project's pinned environment.
    2. Otherwise, write the absolute resolved path of the current
       ``ctxr-fsm`` console script. We use ``sys.argv[0]`` only when
       it already points at an existing file (e.g. a uv-run invocation
       that bypassed the ``uv`` branch above, or a direct call against
       the venv's ``.venv/bin/ctxr-fsm``). For the common pipx /
       global-install case, ``sys.argv[0]`` is the bare name
       ``ctxr-fsm`` — resolving that with :meth:`Path.resolve` would
       produce ``<cwd>/ctxr-fsm``, a path that does not exist on the
       operator's machine. To avoid persisting that broken absolute
       path into long-lived client configs (which would reintroduce
       the unreachable-MCP regression this whole resolver guards
       against), we fall back to :func:`shutil.which` to discover
       where the installed console script actually lives.
    3. If neither path produces a real file, raise
       :class:`FileNotFoundError` — silently persisting an unreachable
       command into Claude / Codex / Cursor configs is much worse than
       a loud failure at install time.

    The Codex TOML emitter derives its own block from the returned
    dict so the two surfaces stay in lockstep.
    """
    py_exe = Path(sys.executable).resolve()
    virtual_env = os.environ.get("VIRTUAL_ENV")
    is_uv_run = (
        virtual_env is not None
        and py_exe.is_relative_to(Path(virtual_env).resolve())
    )
    if is_uv_run and shutil.which("uv") is not None:
        return {
            "command": "uv",
            "args": ["run", "ctxr-fsm", "mcp", "--transport", "stdio"],
            "env": {},
        }

    # Fall back to the absolute resolved script path.
    resolved: Path | None = None
    argv0_raw = sys.argv[0] if sys.argv else ""
    if argv0_raw:
        candidate = Path(argv0_raw)
        # ``Path.is_file()`` against a relative path resolves against
        # cwd, but we ONLY accept it when it is a real file at that
        # location — for a bare console-script name like ``ctxr-fsm``
        # this almost always returns False (no ``./ctxr-fsm`` in cwd),
        # which is precisely the case that must fall through to
        # ``shutil.which``.
        if candidate.is_file():
            resolved = candidate.resolve()

    if resolved is None:
        which_hit = shutil.which("ctxr-fsm")
        if which_hit is not None:
            resolved = Path(which_hit).resolve()

    if resolved is None:
        raise FileNotFoundError(
            "Could not resolve a real filesystem path for the "
            "ctxr-fsm console script — sys.argv[0]="
            f"{argv0_raw!r} is not an existing file and "
            "shutil.which('ctxr-fsm') returned None. Refusing to "
            "persist an unreachable command into client MCP configs. "
            "Install ctxr-fsm so the console script is on PATH "
            "(e.g. `pipx install ctxr-fsm` or `uv tool install "
            "ctxr-fsm`) and retry."
        )

    return {
        "command": str(resolved),
        "args": ["mcp", "--transport", "stdio"],
        "env": {},
    }


# Module-level fallback kept for tests + back-compat with callers that
# diff against a known shape. Treated as a default only; production
# callers always go through :func:`_resolve_stdio_entry`.
_STDIO_ENTRY_JSON: dict[str, Any] = {
    "command": "ctxr-fsm",
    "args": ["mcp", "--transport", "stdio"],
    "env": {},
}


def _portable_repr(path: Path, *, base: Path) -> str:
    """Render ``path`` in the most-portable form for a persisted artefact.

    Rules (in priority order):

    1. If ``path`` lives under ``base`` (typically cwd) → project-relative
       form (e.g., ``.ctxr-fsm/fsm.db``). Persisted JSON envelopes /
       printed CLI output / committed config files use this shape so the
       artefact survives being pushed to git or moved between machines.
    2. Else if under the user's ``$HOME`` → ``~``-prefixed form. Matches
       how user-level configs (``~/.codex/config.toml``,
       ``~/.cursor/mcp.json``) are conventionally written.
    3. Else → absolute path. Caller explicitly pointed at a file outside
       both cwd and home; we surface that honestly.

    This helper exists so every place that emits a path into a JSON
    envelope, a stdout summary, or a persisted manifest uses ONE
    portability convention.
    """
    try:
        return str(path.relative_to(base))
    except ValueError:
        pass
    home = Path.home()
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Indent detection (JSON files)
# ---------------------------------------------------------------------------


def _detect_indent(text: str) -> str:
    """Return the indent unit used by the file's first nested line.

    The merge path preserves whatever style the user already had so
    a re-write does not balloon a diff with whitespace-only noise. We
    look at the first line that begins with whitespace and try to
    classify it:

    * Starts with a tab → ``"\\t"``.
    * Starts with N spaces (1 ≤ N ≤ 8) → ``"  "`` or ``"    "`` etc.
    * Anything else (empty file, single-line JSON, unknown form) →
      default to 2 spaces.

    We deliberately default to 2-space rather than ``json.dumps``'s
    default of "no indent / single line" so a fresh file we create
    is human-readable straight away.
    """
    for line in text.splitlines():
        if not line:
            continue
        if line[0] == "\t":
            return "\t"
        if line[0] == " ":
            count = 0
            for ch in line:
                if ch != " ":
                    break
                count += 1
            if 1 <= count <= 8:
                return " " * count
            return "  "
    return "  "


# ---------------------------------------------------------------------------
# Shared --check detail strings
# ---------------------------------------------------------------------------


def _check_detail_for(status: McpConfigStatus) -> str:
    """Return the human-readable ``detail`` for a ``--check`` outcome.

    Centralised so the JSON merger + the TOML splicer surface
    identical wording per status. ``McpConfigStatus.unchanged`` is not
    a valid ``--check`` outcome (it's the apply-path "already in
    sync"); the function still handles it defensively for the
    Open-Closed audit invariant — adding a new member here is a single
    edit rather than two.
    """
    match status:
        case McpConfigStatus.installed:
            return "entry matches desired stdio shape"
        case McpConfigStatus.missing:
            return "entry absent"
        case McpConfigStatus.out_of_date:
            return "entry present but differs from desired shape"
        case McpConfigStatus.unchanged:
            return "entry already matched; no write needed"
        case _ as never:
            raise AssertionError(
                f"unhandled McpConfigStatus member: {never!r}"
            )


# ---------------------------------------------------------------------------
# JSON read / merge / write
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Atomic write via tmp + rename, ensuring trailing newline.

    Same idiom as :func:`ctxr.fsm.cli.lifecycle.primitives._atomic_write_text`;
    duplicated here to avoid pulling the lifecycle module into the
    install path. Both files are stable enough that the drift cost is
    near zero.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_json_or_empty(path: Path) -> tuple[dict[str, Any], str | None]:
    """Return ``(dict, original_text)``; both empty when file is missing.

    A bare list / number / string at the JSON root is treated as a
    malformed file for our purposes (we own a key inside an object
    layer) and raises so the caller can surface a friendly error
    rather than silently overwriting.
    """
    if not path.exists():
        return {}, None
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return {}, text
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} exists but is not valid JSON: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise ValueError(
            f"{path} top-level must be a JSON object; got {type(loaded).__name__}"
        )
    return loaded, text


def _ensure_mcp_servers_block(
    doc: dict[str, Any], *, source_path: Path
) -> dict[str, Any]:
    """Return ``doc['mcpServers']``, creating it as an empty dict if absent.

    Refuses to overwrite a non-object ``mcpServers`` value: that
    indicates the file was written by a different tool with a
    different contract, and silently clobbering it would be a much
    worse failure mode than a loud error. The brief explicitly calls
    out this guard.
    """
    block = doc.get("mcpServers")
    if block is None:
        new_block: dict[str, Any] = {}
        doc["mcpServers"] = new_block
        return new_block
    if not isinstance(block, dict):
        raise ValueError(
            f"{source_path}: 'mcpServers' must be a JSON object; "
            f"got {type(block).__name__}. Aborting to avoid clobbering "
            "the existing config."
        )
    return block


def _merge_json_entry(
    *, path: Path, dry_run: bool, check: bool
) -> dict[str, Any]:
    """JSON merger shared by Claude .mcp.json / Cursor mcp.json.

    Returns a per-client outcome dict with keys:

    * ``path`` — the file we read/wrote.
    * ``action`` — one of ``applied`` | ``unchanged`` | ``would-apply`` |
      ``would-create`` | ``check:installed`` | ``check:missing`` |
      ``check:out-of-date``.
    * ``status`` (check mode only) — same as the suffix of ``action``.
    * ``detail`` — short human-readable note for diagnostics.
    """
    existing, original_text = _load_json_or_empty(path)
    indent = _detect_indent(original_text or "")

    existing_servers = (
        _ensure_mcp_servers_block(existing, source_path=path)
        if path.exists()
        else {}
    )
    current_entry = existing_servers.get(_ENTRY_NAME)
    desired_entry = _resolve_stdio_entry()
    is_installed = current_entry == desired_entry

    if check:
        if current_entry is None:
            status = McpConfigStatus.missing
        elif is_installed:
            status = McpConfigStatus.installed
        else:
            status = McpConfigStatus.out_of_date
        return {
            "path": _portable_repr(path, base=Path.cwd()),
            "action": f"check:{status.value}",
            "status": status.value,
            "detail": _check_detail_for(status),
        }

    if is_installed:
        return {
            "path": _portable_repr(path, base=Path.cwd()),
            "action": "unchanged",
            "detail": "ctxr-fsm entry already matches; no write needed",
        }

    # Build the patched document for both dry-run preview + write.
    patched = existing if path.exists() else {}
    servers = (
        _ensure_mcp_servers_block(patched, source_path=path)
        if path.exists()
        else {}
    )
    if not path.exists():
        # Fresh file: single block, single entry.
        patched = {"mcpServers": {_ENTRY_NAME: desired_entry}}
        servers = patched["mcpServers"]
    else:
        servers[_ENTRY_NAME] = desired_entry
        patched["mcpServers"] = servers

    new_text = json.dumps(patched, indent=indent, sort_keys=False) + "\n"

    if dry_run:
        return {
            "path": _portable_repr(path, base=Path.cwd()),
            "action": ("would-create" if not path.exists() else "would-apply"),
            "detail": (
                f"would write {len(new_text)} bytes "
                f"({'create' if not path.exists() else 'update'})"
            ),
            "preview": new_text,
        }

    _atomic_write(path, new_text)
    return {
        "path": _portable_repr(path, base=Path.cwd()),
        "action": "applied",
        "detail": "ctxr-fsm entry merged into mcpServers",
    }


# ---------------------------------------------------------------------------
# TOML read / merge / write (Codex)
# ---------------------------------------------------------------------------


def _emit_codex_toml_block() -> str:
    """Return the canonical ``[mcp_servers.ctxr-fsm]`` block as text.

    Hand-rolled (rather than via ``tomli-w``) because we only own a
    single, fixed-shape table with two string-array values; the cost
    of a third-party write-dependency for this one case is not worth
    it. The format we emit is the TOML canonical form (``key = ...``,
    arrays bracketed, strings double-quoted). The ``command`` /
    ``args`` shape comes from :func:`_resolve_stdio_entry` so the
    Codex TOML stays in lockstep with the JSON-merge surface.
    """
    entry = _resolve_stdio_entry()
    # ``json.dumps`` is the easiest way to emit a JSON-style string
    # array that also happens to be valid TOML for an array of
    # quoted strings.
    args_arr = json.dumps(entry["args"])
    command_str = json.dumps(entry["command"])
    return (
        "[mcp_servers.ctxr-fsm]\n"
        f"command = {command_str}\n"
        f"args = {args_arr}\n"
    )


def _read_codex_toml(path: Path) -> dict[str, Any] | None:
    """Return the parsed TOML doc, or ``None`` if absent.

    Malformed TOML raises (caller surfaces a friendly error) so we
    never silently overwrite hand-edited content we can't parse.
    """
    if not path.exists():
        return None
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _codex_current_entry(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract ``mcp_servers.ctxr-fsm`` from a parsed Codex doc."""
    if doc is None:
        return None
    block = doc.get("mcp_servers")
    if not isinstance(block, dict):
        return None
    entry = block.get(_ENTRY_NAME)
    if isinstance(entry, dict):
        return entry
    return None


def _codex_entry_matches_desired(entry: dict[str, Any] | None) -> bool:
    """Compare an existing Codex TOML entry to the desired stdio shape.

    We compare only the keys we own (``command`` and ``args``). Codex
    users sometimes add their own ``env`` / ``cwd`` keys; preserving
    those (rather than wiping them) is the polite default. The
    ``command`` / ``args`` shape comes from :func:`_resolve_stdio_entry`
    so a stale-shape entry (bare literal vs uv-run / absolute-path) is
    correctly flagged as out-of-date on the next ``--check``.
    """
    if entry is None:
        return False
    desired = _resolve_stdio_entry()
    matches: bool = (
        entry.get("command") == desired["command"]
        and entry.get("args") == desired["args"]
    )
    return matches


def _splice_codex_toml_block(*, original: str, new_block: str) -> str:
    """Replace ``[mcp_servers.ctxr-fsm]`` in ``original`` with ``new_block``.

    A minimal hand-rolled splicer that only touches our own table.
    Algorithm:

    1. Scan the file line by line.
    2. Locate the ``[mcp_servers.ctxr-fsm]`` header.
    3. Drop every line from that header up to (but not including) the
       NEXT line that begins with ``[`` (the next table header) — that
       is the slice of the document our table owns.
    4. Insert ``new_block`` at the header's start position.
    5. If the header was not present, append ``new_block`` to the END
       of the file (preceded by a blank line if the file is non-empty
       and doesn't already end with one).

    The result preserves every other table, comment, and blank line.
    """
    header = "[mcp_servers.ctxr-fsm]"
    lines = original.splitlines(keepends=True)
    start_idx: int | None = None
    end_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start_idx = i
            # Find the next table header (any ``[...]`` at column 0).
            for j in range(i + 1, len(lines)):
                stripped = lines[j].lstrip()
                if stripped.startswith("[") and not stripped.startswith("[["):
                    # New table header. Backtrack over any
                    # immediately-preceding blank lines so we don't
                    # leave a double-blank where the table used to be.
                    end_idx = j
                    while end_idx > start_idx + 1 and lines[end_idx - 1].strip() == "":
                        end_idx -= 1
                    break
            else:
                end_idx = len(lines)
            break

    if start_idx is None:
        # Append at the end. Pad with blank lines so the new table is
        # visually separated from whatever ended the file.
        prefix = original
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix.strip():
            prefix += "\n"
        return prefix + new_block

    # Splice: keep [0:start_idx) + new block + [end_idx:).
    head = "".join(lines[:start_idx])
    tail = "".join(lines[end_idx or start_idx + 1 :])
    return head + new_block + tail


def _can_use_codex_cli() -> bool:
    """``True`` iff the ``codex`` binary is on PATH.

    Probed via :func:`shutil.which` so we never spawn the binary just
    to see whether it exists. The CLI path is preferred because it
    keeps Codex's own validation in the loop (the CLI rejects bad
    args / writes the canonical TOML form).
    """
    return shutil.which("codex") is not None


def _invoke_codex_cli_add(*, dry_run: bool) -> dict[str, Any]:
    """Run ``codex mcp add ctxr-fsm -- <command> <args...>``.

    Returns the same outcome dict shape JSON merger returns so callers
    can treat both paths uniformly. ``dry_run`` short-circuits to a
    "would-invoke" record without spawning Codex. The trailing argv
    after ``--`` comes from :func:`_resolve_stdio_entry` so the Codex
    CLI surface matches what the JSON / TOML mergers write (the
    ``uv run`` shape under a uv-managed venv, the absolute path
    otherwise).
    """
    entry = _resolve_stdio_entry()
    cmd = [
        "codex",
        "mcp",
        "add",
        _ENTRY_NAME,
        "--",
        entry["command"],
        *entry["args"],
    ]
    if dry_run:
        return {
            "path": "~/.codex/config.toml",
            "action": "would-apply",
            "detail": f"would invoke: {' '.join(cmd)}",
        }
    res = subprocess.run(
        cmd, capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        return {
            "path": "~/.codex/config.toml",
            "action": "failed",
            "detail": (
                f"codex mcp add exited {res.returncode}: "
                f"stdout={res.stdout!r} stderr={res.stderr!r}"
            ),
        }
    return {
        "path": "~/.codex/config.toml",
        "action": "applied",
        "detail": "codex mcp add succeeded",
    }


def _merge_codex_toml_direct(
    *, path: Path, dry_run: bool, check: bool
) -> dict[str, Any]:
    """TOML merger fallback when the ``codex`` binary is absent."""
    try:
        existing_doc = _read_codex_toml(path)
    except tomllib.TOMLDecodeError as exc:
        return {
            "path": _portable_repr(path, base=Path.cwd()),
            "action": "failed",
            "detail": f"{path} exists but is not valid TOML: {exc}",
        }

    current_entry = _codex_current_entry(existing_doc)
    matches = _codex_entry_matches_desired(current_entry)

    if check:
        if current_entry is None:
            status = McpConfigStatus.missing
        elif matches:
            status = McpConfigStatus.installed
        else:
            status = McpConfigStatus.out_of_date
        return {
            "path": _portable_repr(path, base=Path.cwd()),
            "action": f"check:{status.value}",
            "status": status.value,
            "detail": _check_detail_for(status),
        }

    if matches:
        return {
            "path": _portable_repr(path, base=Path.cwd()),
            "action": "unchanged",
            "detail": "ctxr-fsm table already matches; no write needed",
        }

    original_text = path.read_text(encoding="utf-8") if path.exists() else ""
    patched_text = _splice_codex_toml_block(
        original=original_text, new_block=_emit_codex_toml_block()
    )

    if dry_run:
        return {
            "path": _portable_repr(path, base=Path.cwd()),
            "action": "would-create" if not path.exists() else "would-apply",
            "detail": f"would write {len(patched_text)} bytes",
            "preview": patched_text,
        }

    _atomic_write(path, patched_text)
    return {
        "path": _portable_repr(path, base=Path.cwd()),
        "action": "applied",
        "detail": "ctxr-fsm TOML table spliced",
    }


# ---------------------------------------------------------------------------
# Client detection + dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClientTask:
    """One concrete (client, target_path) pair the dispatcher will act on.

    ``client`` is a :class:`McpClient` member; ``auto`` and ``none``
    never appear here because the dispatcher fans those out before
    materialising tasks. We keep the typed enum (rather than
    ``Literal[McpClient.claude, McpClient.codex, McpClient.cursor]``)
    so the dispatch is uniform with the rest of the W14 surface.
    """

    client: McpClient
    path: Path
    # For Claude only: the workspace .mcp.json AND the settings.json
    # block can BOTH be present; the dispatcher emits one task per
    # path so the per-task report is still a single outcome dict.
    label: str = ""

    # Keep mypy happy with field()
    _: tuple[()] = field(default=())


def _claude_targets(target_dir: Path) -> list[Path]:
    """Return the Claude config paths to act on.

    Per the brief:

    * ``<target_dir>/.mcp.json`` is preferred when it already exists OR
      when ``target_dir`` looks like a workspace root (has ``.git/`` or
      a ``CLAUDE.md``).
    * ``<target_dir>/.claude/settings.json`` is also probed; if its
      ``mcpServers`` block exists we also touch it.

    Both are appended so a project that uses both surfaces stays in
    sync. If neither exists and the dir doesn't look like a workspace,
    we still default to ``.mcp.json`` (fresh-create) when the explicit
    client is ``claude`` — the brief is explicit about this contract
    for the "auto" path (workspace-root heuristic) and the "claude"
    explicit path (always emit).
    """
    out: list[Path] = []
    workspace_marker = (
        (target_dir / ".git").exists() or (target_dir / "CLAUDE.md").exists()
    )
    mcp_json = target_dir / ".mcp.json"
    if mcp_json.exists() or workspace_marker:
        out.append(mcp_json)
    settings_json = target_dir / ".claude" / "settings.json"
    if settings_json.exists():
        out.append(settings_json)
    if not out:
        # Caller asked us about Claude explicitly but neither file
        # exists and the dir isn't obviously a workspace; fall back to
        # the .mcp.json convention so a fresh-create still happens.
        out.append(mcp_json)
    return out


def _cursor_target() -> Path:
    """Return ``~/.cursor/mcp.json``.

    Cursor's MCP config lives in the user home, not per-project.
    """
    return Path.home() / ".cursor" / "mcp.json"


def _codex_target() -> Path:
    """Return ``~/.codex/config.toml`` (Codex user-level config)."""
    return Path.home() / ".codex" / "config.toml"


def _resolve_tasks(target_dir: Path, client: McpClient) -> list[_ClientTask]:
    """Resolve the ``--client`` value into concrete (client, path) tasks.

    ``McpClient.auto`` probes each supported client and includes those
    whose config file exists OR (for Claude) whose target dir is a
    workspace root. Explicit names always emit a task even when the
    file is absent (the merger will create it). ``McpClient.none`` is
    handled by the caller (short-circuit no-op) and never reaches
    here.
    """
    tasks: list[_ClientTask] = []
    auto_or = {McpClient.auto, McpClient.claude}

    if client in auto_or:
        # Auto includes Claude only when SOMETHING points at it
        # (existing file or workspace marker); explicit always emits.
        if client is McpClient.claude:
            for path in _claude_targets(target_dir):
                tasks.append(_ClientTask(client=McpClient.claude, path=path))
        else:
            workspace = (
                (target_dir / ".git").exists()
                or (target_dir / "CLAUDE.md").exists()
            )
            mcp_json = target_dir / ".mcp.json"
            settings_json = target_dir / ".claude" / "settings.json"
            if mcp_json.exists() or workspace:
                tasks.append(_ClientTask(client=McpClient.claude, path=mcp_json))
            if settings_json.exists():
                tasks.append(
                    _ClientTask(client=McpClient.claude, path=settings_json)
                )

    if client in {McpClient.auto, McpClient.codex}:
        codex_path = _codex_target()
        if client is McpClient.codex or codex_path.exists():
            tasks.append(_ClientTask(client=McpClient.codex, path=codex_path))

    if client in {McpClient.auto, McpClient.cursor}:
        cursor_path = _cursor_target()
        if client is McpClient.cursor or cursor_path.exists():
            tasks.append(_ClientTask(client=McpClient.cursor, path=cursor_path))

    return tasks


def _dispatch_one(
    task: _ClientTask, *, dry_run: bool, check: bool
) -> dict[str, Any]:
    """Run one task and return its outcome dict.

    Dispatches by client kind: Claude + Cursor share the JSON merger;
    Codex uses the CLI path when available and the TOML splicer
    otherwise.
    """
    json_clients = {McpClient.claude, McpClient.cursor}
    if task.client in json_clients:
        try:
            outcome = _merge_json_entry(
                path=task.path, dry_run=dry_run, check=check
            )
        except ValueError as exc:
            outcome = {
                "path": _portable_repr(task.path, base=Path.cwd()),
                "action": "failed",
                "detail": str(exc),
            }
        outcome["client"] = task.client.value
        return outcome

    # Codex.
    if not check and not dry_run and _can_use_codex_cli():
        outcome = _invoke_codex_cli_add(dry_run=False)
        outcome["client"] = McpClient.codex.value
        return outcome
    if dry_run and _can_use_codex_cli():
        outcome = _invoke_codex_cli_add(dry_run=True)
        outcome["client"] = McpClient.codex.value
        return outcome
    outcome = _merge_codex_toml_direct(
        path=task.path, dry_run=dry_run, check=check
    )
    outcome["client"] = McpClient.codex.value
    return outcome


# ---------------------------------------------------------------------------
# Public entry point — used by both Typer command and ``ctxr-fsm ensure``
# ---------------------------------------------------------------------------


def run_install_mcp(
    target_dir: Path,
    client: McpClient | str = McpClient.auto,
    dry_run: bool = False,
    check: bool = False,
) -> dict[str, Any]:
    """Apply (or probe) the ctxr-fsm stdio MCP entry across detected clients.

    Pure function: never prints, never raises typer.Exit — callers
    layer that on top. Returns a JSON-serialisable summary dict::

        {
          "target": "<target-dir>",
          "client": "auto",
          "dry_run": False,
          "check": False,
          "results": [
            {"client": "claude", "path": "<config-path>", "action": "applied", ...},
            ...
          ],
        }

    The ``target`` field is the directory the caller supplied (as a
    string of whatever shape the caller passed in — relative or absolute).
    Per-result ``path`` strings are project-relative when the config
    lives under ``target_dir``, ``~``-prefixed when under ``$HOME``, else
    absolute (the Cursor / Codex user-level configs are typical
    examples).

    ``client`` accepts either a :class:`McpClient` member or the bare
    string value (callers reaching the function through the Typer
    surface still pass strings; both paths normalise to the enum
    before dispatching).
    """
    try:
        client_enum = client if isinstance(client, McpClient) else McpClient(client)
    except ValueError as exc:
        raise ValueError(
            f"client must be one of {_CLIENT_CHOICES!r}; got {client!r}"
        ) from exc

    target_dir = target_dir.expanduser().resolve()

    summary: dict[str, Any] = {
        # Persisted into the JSON envelope the CLI prints, so use the
        # portable form (project-relative when target_dir is under cwd,
        # ``~``-prefixed when under HOME, absolute only as last resort).
        # Keeps the envelope safe to commit / share across machines.
        "target": _portable_repr(target_dir, base=Path.cwd()),
        "client": client_enum.value,
        "dry_run": dry_run,
        "check": check,
        "results": [],
    }

    if client_enum is McpClient.none:
        summary["results"] = []
        return summary

    tasks = _resolve_tasks(target_dir, client_enum)
    if not tasks:
        summary["results"] = []
        summary["detail"] = (
            "no client config files detected (looked for .mcp.json, "
            ".claude/settings.json, ~/.codex/config.toml, ~/.cursor/mcp.json)"
        )
        return summary

    results: list[dict[str, Any]] = []
    for task in tasks:
        results.append(_dispatch_one(task, dry_run=dry_run, check=check))
    summary["results"] = results
    return summary


# ---------------------------------------------------------------------------
# Typer entry point
# ---------------------------------------------------------------------------


def install_mcp(
    target: Path | None = typer.Option(  # noqa: B008 — typer sentinel
        None,
        "--target",
        help=(
            "Directory containing project-local MCP client config "
            "(.mcp.json, .claude/settings.json). Defaults to the "
            "current working directory."
        ),
        resolve_path=True,
    ),
    client: str = typer.Option(
        "auto",
        "--client",
        help=(
            "Which MCP client config to patch: 'auto' (detect), 'claude', "
            "'codex', 'cursor', or 'none' (no-op)."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print the patches that would be written without touching "
            "the filesystem."
        ),
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help=(
            "Read-only probe: report status per client without applying. "
            "Exits non-zero if any client is missing or out-of-date."
        ),
    ),
    json_mode: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of pretty-printed output.",
    ),
) -> None:
    """Register ctxr-fsm as a stdio MCP server in detected client configs.

    See the module docstring for the full per-client behaviour and
    idempotency guarantees.
    """
    if client not in _CLIENT_CHOICES:
        raise typer.BadParameter(
            f"--client must be one of {', '.join(_CLIENT_CHOICES)}, "
            f"got {client!r}"
        )

    resolved_target = (target if target is not None else Path.cwd()).resolve()

    try:
        # ``run_install_mcp`` accepts McpClient | str and normalises
        # at the boundary, so the Typer-supplied string passes through
        # without a cast.
        summary = run_install_mcp(
            target_dir=resolved_target,
            client=client,
            dry_run=dry_run,
            check=check,
        )
    except ValueError as exc:
        # _resolve_tasks doesn't raise ValueError but the JSON parser
        # underneath does on a malformed existing file. Surface that
        # as a non-zero exit with a friendly stderr message.
        sys.stderr.write(f"error: {exc}\n")
        raise typer.Exit(1) from exc

    json_or_pretty(summary, json_mode)

    # In --check mode: exit non-zero if any client is missing /
    # out-of-date so CI scripts can detect drift. Compare against the
    # enum's wire values (the same source the renderer used to build the
    # ``status`` field) rather than hardcoded literals. The W14i rename
    # of ``out-of-date`` to ``out_of_date`` silently broke a literal
    # comparison once already.
    if check:
        drifted = {McpConfigStatus.missing.value, McpConfigStatus.out_of_date.value}
        bad = [
            r for r in summary.get("results", [])
            if r.get("status") in drifted
        ]
        if bad:
            raise typer.Exit(1)
