#!/usr/bin/env bash
# Pre-tool-use hook (Bash shim).
#
# Claude Code hooks can be either an executable script or a shell
# command. Some consumer projects pin ``command: bash`` in their
# ``settings.json`` and prefer to keep the hook entry as a shell
# invocation — this shim is the canonical entry point for that style.
#
# It locates the sibling Python implementation, forwards stdin /
# stdout / stderr verbatim, and propagates the exit code unchanged so
# the block / allow contract is preserved.
#
# The Python binary is resolved in this order:
#   1. ``$CTXR_FSM_GUARD_PYTHON`` (operator override).
#   2. ``python3`` from ``$PATH``.
#   3. ``python`` from ``$PATH``.
#
# We do NOT use ``uv run`` here because the hook MUST be cheap to
# invoke on every tool call — paying the uv resolver cost per call
# would noticeably slow down a busy session. The Python script itself
# has zero non-stdlib dependencies.

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
hook_py="${script_dir}/pre-tool-use.fsm-guard.py"

if [[ ! -f "$hook_py" ]]; then
    echo "fsm-guard: missing sibling ${hook_py}; allowing tool call" >&2
    exit 0
fi

python_bin="${CTXR_FSM_GUARD_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        python_bin="python3"
    elif command -v python >/dev/null 2>&1; then
        python_bin="python"
    else
        echo "fsm-guard: no python3/python on PATH; allowing tool call" >&2
        exit 0
    fi
fi

exec "$python_bin" "$hook_py" "$@"
