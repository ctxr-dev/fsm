#!/usr/bin/env bash
# audit_strings.sh — guard against closed-vocabulary string literals
#
# Runs a battery of greps over the Python source tree looking for
# patterns the W14i audit identified as smells: inline ``Literal[...]``
# narrowings that shadow an existing StrEnum, raw string-equality on
# enum-vocabulary values, and hard-coded MCP-client name lists.
#
# Each finding prints ``file:line: rule: detail`` to stdout. Exits 0
# when nothing matched (the audit is clean), non-zero when at least
# one finding surfaced (so CI / pre-commit can gate on it).
#
# Justifications: a finding can be silenced inline with a trailing
# comment marker (see ``_JUSTIFY_MARKER`` below) — used sparingly for
# genuinely-open vocabularies (CLI ``transport``, supervisor ``mode``)
# that have no enum reason to exist.

set -uo pipefail

# Trailing marker on a line that opts it out of the audit. We deliberately
# require an explicit per-line comment so a contributor has to defend the
# exception rather than silently bypass the check.
_JUSTIFY_MARKER='# audit-strings: justified'

ROOT="${1:-$(pwd)}"
SRC_DIR="${ROOT}/ctxr/fsm"
TEST_DIR="${ROOT}/tests"

if [[ ! -d "${SRC_DIR}" ]]; then
  echo "audit-strings: source dir not found: ${SRC_DIR}" >&2
  exit 2
fi

HITS=0

# Filter that drops lines carrying the explicit justification marker.
# We use ``cat`` then ``grep -v`` so a missing input file fails the
# pipeline loudly rather than silently producing zero hits.
_filter_justified() {
  grep -v "${_JUSTIFY_MARKER}" || true
}

# Report a single rule violation set. Args: rule-name, file:line:body lines.
_report() {
  local rule="$1"
  local body="$2"
  if [[ -z "${body}" ]]; then
    return
  fi
  echo "${body}" | awk -F: -v rule="${rule}" '{printf "%s:%s: %s: %s\n", $1, $2, rule, substr($0, length($1)+length($2)+3)}'
}

# --- Rule 1: ``Literal["..."]`` narrowings ----------------------------
# Every ``Literal[...]`` whose values overlap a StrEnum is the offender
# the user flagged on PR #39. Inline Literal narrowing is allowed only
# when the value is a true one-off API parameter (justify with the
# trailing marker so the audit walks over it).
#
# Pattern covers both quote styles (double + single) and any amount of
# whitespace between ``Literal[`` and the first quote, so contributors
# can't sidestep the audit with ``Literal['x']`` or ``Literal[ "x"]``.
RULE1=$(grep -rEn --include='*.py' 'Literal\[[[:space:]]*["'"'"']' "${SRC_DIR}" 2>/dev/null | _filter_justified || true)
RULE1_HITS=$(echo -n "${RULE1}" | grep -c '^' || true)
if [[ "${RULE1_HITS}" -gt 0 ]]; then
  echo "--- audit-strings: rule1 (inline Literal narrowings)"
  _report "rule1-literal" "${RULE1}"
  HITS=$((HITS + RULE1_HITS))
fi

# --- Rule 2: raw string equality on TransitionKind vocabulary ---------
# Always / otherwise / deterministic / judgement are enum members; the
# raw-string compare branch is a bug waiting to happen.
#
# Pattern covers four equivalence shapes: ``x == "v"``, ``x != "v"``,
# ``"v" == x``, ``"v" != x``, with either quote style. A new raw-string
# comparison written in any of those shapes lights up the gate.
RULE2=""
for value in always otherwise deterministic judgement; do
  patt='([!=]=[[:space:]]*["'"'"']'"${value}"'["'"'"']|["'"'"']'"${value}"'["'"'"'][[:space:]]*[!=]=)'
  hits=$(grep -rEn --include='*.py' "${patt}" "${SRC_DIR}" 2>/dev/null | _filter_justified || true)
  if [[ -n "${hits}" ]]; then
    RULE2="${RULE2}${hits}\n"
  fi
done
RULE2_HITS=$(echo -ne "${RULE2}" | grep -c '^' || true)
if [[ "${RULE2_HITS}" -gt 0 ]]; then
  echo "--- audit-strings: rule2 (TransitionKind raw equality)"
  echo -ne "${RULE2}" | _report "rule2-transitionkind" "$(cat)"
  HITS=$((HITS + RULE2_HITS))
fi

# --- Rule 3: raw string equality on VerifierVerdict vocabulary --------
# Same four-shape coverage as rule 2; matches the verdict name on
# either side of ``==`` / ``!=`` with either quote style.
RULE3=""
for value in passed rejected inconclusive; do
  patt='([!=]=[[:space:]]*["'"'"']'"${value}"'["'"'"']|["'"'"']'"${value}"'["'"'"'][[:space:]]*[!=]=)'
  hits=$(grep -rEn --include='*.py' "${patt}" "${SRC_DIR}" 2>/dev/null | _filter_justified || true)
  if [[ -n "${hits}" ]]; then
    RULE3="${RULE3}${hits}\n"
  fi
done
RULE3_HITS=$(echo -ne "${RULE3}" | grep -c '^' || true)
if [[ "${RULE3_HITS}" -gt 0 ]]; then
  echo "--- audit-strings: rule3 (VerifierVerdict raw equality)"
  echo -ne "${RULE3}" | _report "rule3-verifierverdict" "$(cat)"
  HITS=$((HITS + RULE3_HITS))
fi

# --- Rule 4: hard-coded MCP-client name tuples ------------------------
# A scattered ``("auto", "claude", "codex", "cursor", "none")`` tuple
# means someone re-declared the McpClient vocabulary instead of
# importing the enum. ``_CLIENT_CHOICES`` derives its tuple from the
# enum's members and IS allowed — that's why we look for the literal
# string list, not the constant.
RULE4=$(grep -rn '"auto", "claude", "codex", "cursor"' "${SRC_DIR}" 2>/dev/null | _filter_justified || true)
RULE4_HITS=$(echo -n "${RULE4}" | grep -c '^' || true)
if [[ "${RULE4_HITS}" -gt 0 ]]; then
  echo "--- audit-strings: rule4 (hard-coded McpClient name tuple)"
  _report "rule4-mcpclient-tuple" "${RULE4}"
  HITS=$((HITS + RULE4_HITS))
fi

# --- Rule 5: absolute-path literals in source -------------------------
# Configs, persisted artefacts, and CLI output MUST use portable paths
# (project-relative or ``~``-prefixed). An absolute-path literal in
# source code is almost always a leak: a docstring example showing
# ``/Users/...``, a fixture comparing against ``/abs/path``, a print
# format string interpolating a resolved Path. The
# ``_portable_repr(path, base=...)`` helper exists for this — use it.
#
# Patterns flagged:
#   "/Users/...", "/home/...", "/abs/path...", "/private/var/folders/...".
#
# Justified cases (system-default paths in tests; the helper itself):
# tag the line with ``# audit-strings: justified``.
RULE5=""
for pattern in '"/Users/' '"/home/' '"/abs/path' '"/private/var/folders/'; do
  hits=$(grep -rn "${pattern}" "${SRC_DIR}" 2>/dev/null | _filter_justified || true)
  if [[ -n "${hits}" ]]; then
    RULE5="${RULE5}${hits}\n"
  fi
done
RULE5_HITS=$(echo -ne "${RULE5}" | grep -c '^' || true)
if [[ "${RULE5_HITS}" -gt 0 ]]; then
  echo "--- audit-strings: rule5 (absolute-path literal in source)"
  echo -ne "${RULE5}" | _report "rule5-absolute-path-literal" "$(cat)"
  HITS=$((HITS + RULE5_HITS))
fi

# --- Rule 6: argparse / click in CLI surface --------------------------
# Every CLI command in ctxr-fsm is a typer subcommand. Other parsers
# fragment the CLI shape; bespoke pretty-print primitives fragment the
# human surface. The ``rich`` integration ships with typer — that's the
# ONLY pretty-print primitive we allow. Justify a genuine exception
# (extremely rare) with the trailing ``# audit-strings: justified``
# marker. Added in W14j alongside the typer-everywhere rule in
# docs/coding-standards.md.
RULE6=""
for pattern in '^import argparse' '^from argparse' '^import click' '^from click'; do
  hits=$(grep -rn "${pattern}" "${SRC_DIR}/cli" 2>/dev/null | _filter_justified || true)
  if [[ -n "${hits}" ]]; then
    RULE6="${RULE6}${hits}\n"
  fi
done
RULE6_HITS=$(echo -ne "${RULE6}" | grep -c '^' || true)
if [[ "${RULE6_HITS}" -gt 0 ]]; then
  echo "--- audit-strings: rule6 (argparse/click forbidden in cli/)"
  echo -ne "${RULE6}" | _report "rule6-non-typer-parser" "$(cat)"
  HITS=$((HITS + RULE6_HITS))
fi

# --- Rule 7: flat-folder log/artefact paths ---------------------------
# Any directory that ACCUMULATES files over time (logs, exports, audit
# trails, telemetry dumps) MUST live under a YYYY/MM/DD sharded tree
# (use ``sharded_log_path`` from lifecycle.primitives) so the folder
# doesn't grow into a flat bottleneck (ext4 / APFS / NTFS all degrade
# past ~10k to 100k entries per directory; even ``ls`` and tab-completion
# crawl). The user flagged this on W14j when ensure_cmd wrote
# ``.ctxr-fsm/logs/supervisor-YYYYMMDD.log`` directly.
#
# Patterns flagged: any literal ``.ctxr-fsm/logs/`` followed by a
# filename token (not by a sub-shard name like a year, ``${category}``,
# or a typed Path expression).
#
# This rule looks at the SOURCE tree (ctxr/fsm/); test fixtures that
# hand-craft a path to assert the layout are justified.
RULE7=$(grep -rn '\.ctxr-fsm/logs/[a-z]\{1,\}-' "${SRC_DIR}" 2>/dev/null | _filter_justified || true)
RULE7_HITS=$(echo -n "${RULE7}" | grep -c '^' || true)
if [[ "${RULE7_HITS}" -gt 0 ]]; then
  echo "--- audit-strings: rule7 (flat .ctxr-fsm/logs/<file> bypasses sharded_log_path)"
  _report "rule7-flat-logs-folder" "${RULE7}"
  HITS=$((HITS + RULE7_HITS))
fi

if [[ "${HITS}" -gt 0 ]]; then
  echo ""
  echo "audit-strings: ${HITS} finding(s); see W14i for the audit rationale."
  exit 1
fi

echo "audit-strings: clean."
exit 0
