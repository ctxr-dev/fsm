#!/usr/bin/env node
// fsm-lint-runner -- advisory lint for orchestrator-shell skill runners.
//
// Background. The orchestrator-shell discipline (see
// docs/orchestrator-discipline.md) says a skill's runner is a SHELL
// around the @ctxr/fsm CLIs: it should not read the FSM YAML itself,
// it should not call LLM tools at the orchestrator level, and it
// should not compose worker prompts inline. The FSM engine reads YAML
// and validates outputs; workers receive prompts via the staged
// prompt-template files; the orchestrator only shells out and routes
// JSON.
//
// This linter is ADVISORY. It scans each given runner file with
// simple line-based heuristics and reports diagnostics in the form:
//
//   <file>:<line>: <rule>: <suggestion>
//
// Exit 0 if every file is clean; exit 1 if any diagnostic was
// emitted. The linter never modifies files. False positives can be
// suppressed by appending `// fsm-lint:ignore` on the offending line
// (or the line immediately before, for multi-line constructs).
//
// Heuristics:
//
//   no-direct-fsm-yaml-read
//     Pattern: any node:fs read API whose argument string contains
//     `.fsm.yaml`. Examples:
//       readFileSync("path/to/foo.fsm.yaml")
//       readFile("./bar.fsm.yaml", "utf8")
//       createReadStream("baz.fsm.yaml")
//     Why: the engine owns YAML parsing. A runner that reads the
//     YAML is duplicating logic the engine already does (and likely
//     making decisions the FSM should make).
//
//   no-orchestrator-llm-call
//     Pattern: a call expression whose callee name matches one of
//     the known LLM-tool dispatch verbs:
//       Anthropic SDK / clients: messages.create, completions.create,
//         anthropic.messages, client.messages.
//       Tool-name shaped invocations: Task(, Agent(, Skill(, WebFetch(,
//         WebSearch(, Bash( when followed by an LLM-shaped payload.
//     This is the loosest heuristic; comments in the runner that
//     explain WHY a Task( call is legitimately at orchestrator level
//     (e.g. spawning a worker via the harness's Agent tool, which IS
//     the intended pattern) can suppress with `// fsm-lint:ignore`.
//     Why: the orchestrator shells out to fsm-next / fsm-commit; the
//     LLM work happens inside the worker the FSM engine asks the
//     harness to dispatch.
//
//   no-inline-prompt-composition
//     Pattern: a multi-line template literal (backtick string spanning
//     2+ lines in the source) that contains BOTH of:
//       (a) a role/header marker -- one of "You are", "Role:",
//           "## Role", "## Brief", "Specialist:", "Output Contract".
//       (b) a JSON-schema-like marker -- one of "$schema",
//           '"type": "object"', '"required":', 'response_schema'.
//     Why: prompts should live in fsm/prompt-templates/*.md and be
//     composed via @ctxr/fsm/prompt-fragments. Inline composition
//     puts the contract in the runner where it cannot be reviewed
//     or reused.
//
// Limitations (honest list, repeated in docs/orchestrator-discipline.md):
//   - The LLM-call heuristic is name-based; a renamed import will
//     evade it. The inline-prompt heuristic uses string markers; a
//     prompt that omits all markers will evade it. Authors who want
//     to suppress a known-false positive use `// fsm-lint:ignore`.

import { readFileSync, existsSync, realpathSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const USAGE = `Usage: fsm-lint-runner [--help] <runner-file> [<runner-file> ...]

Advisory lint for orchestrator-shell skill runners. Reports any of:
  - direct reads of *.fsm.yaml from the runner
  - LLM-tool calls at the orchestrator level
  - inline multi-line prompt composition (role + schema markers)

Diagnostics print to stdout as:
  <file>:<line>: <rule>: <suggestion>

Exit 0 when every file is clean; exit 1 when any diagnostic was emitted.
Suppress a known false positive by appending '// fsm-lint:ignore' to the
offending line (or the line immediately before, for multi-line
constructs).
`;

// Public entry points are exported so callers (other tools, or future
// in-process tests) can drive the lint pipeline directly; the current
// test suite still uses spawnSync against the CLI to also exercise the
// argv / exit-code path that real consumers go through.

export const RULES = {
  NO_DIRECT_FSM_YAML_READ: "no-direct-fsm-yaml-read",
  NO_ORCHESTRATOR_LLM_CALL: "no-orchestrator-llm-call",
  NO_INLINE_PROMPT_COMPOSITION: "no-inline-prompt-composition",
};

const IGNORE_MARKER = "fsm-lint:ignore";

// Read-API names whose first string argument may legitimately be a
// path. We match the function name; the regex below ensures the
// argument string contains ".fsm.yaml".
const READ_APIS = [
  "readFileSync",
  "readFile",
  "createReadStream",
  "open",
  "openSync",
];

// LLM-tool dispatch verbs / call shapes. The first group covers
// Anthropic SDK shapes; the second covers Claude Code harness tools
// commonly invoked from JS (rare but possible).
const LLM_CALL_PATTERNS = [
  /\bmessages\s*\.\s*create\s*\(/,
  /\bcompletions\s*\.\s*create\s*\(/,
  /\banthropic\s*\.\s*messages\b/,
  /\bclient\s*\.\s*messages\b/,
  /\bTask\s*\(/,
  /\bAgent\s*\(/,
  /\bSkill\s*\(/,
  /\bBash\s*\(/,
  /\bWebFetch\s*\(/,
  /\bWebSearch\s*\(/,
];

// Role / contract markers for the inline-prompt heuristic.
const ROLE_MARKERS = [
  /You are\b/i,
  /\bRole:\s/,
  /^##\s+Role\b/m,
  /^##\s+Brief\b/m,
  /\bSpecialist:\s/,
  /Output Contract/i,
];

// Schema markers for the inline-prompt heuristic.
const SCHEMA_MARKERS = [
  /\$schema/,
  /"type"\s*:\s*"object"/,
  /"required"\s*:\s*\[/,
  /response_schema/,
];

export function lintFile(filePath, source) {
  const diagnostics = [];
  const lines = source.split(/\r?\n/);

  const readApiAlternation = READ_APIS.join("|");
  // Require the .fsm.yaml occurrence to be inside a quoted string
  // literal so block comments or commented-out args (e.g.
  // `readFileSync(/* ".fsm.yaml" */ x)`) do not fire. The argument
  // expression up to the `.fsm.yaml` occurrence is matched lazily,
  // then the suffix must end inside the SAME quote that opened it.
  const readApiRegex = new RegExp(
    `\\b(?:${readApiAlternation})\\s*\\([^)\\n]*?(['"\`])[^'"\`\\n]*\\.fsm\\.yaml[^'"\`\\n]*\\1`,
  );

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const lineNumber = i + 1;
    // The fsm-lint:ignore marker is intentionally per-line for the
    // single-line rules (no-direct-fsm-yaml-read and
    // no-orchestrator-llm-call); previous-line suppression is
    // reserved for the multi-line template-literal rule, where the
    // marker can legitimately precede the literal it suppresses.
    // Applying previous-line suppression to per-line rules silently
    // hid the diagnostic immediately after any other ignored line.
    if (line.includes(IGNORE_MARKER)) {
      continue;
    }

    // Skip lines that are clearly source comments -- both the read-API
    // and the LLM-call rules are about real code, not prose in the
    // header banner. We detect a comment by the first non-space chars
    // being `//`, `*`, or `#` (shebangs / shell). Block-comment middle
    // lines start with ` *` which is also covered.
    const trimmed = line.trimStart();
    const isComment =
      trimmed.startsWith("//") ||
      trimmed.startsWith("*") ||
      trimmed.startsWith("#");
    if (isComment) continue;

    // Rule: no-direct-fsm-yaml-read. Match a known read API name
    // followed by `(`, with `.fsm.yaml` appearing somewhere on the
    // same line inside a string literal.
    if (readApiRegex.test(line)) {
      diagnostics.push({
        file: filePath,
        line: lineNumber,
        rule: RULES.NO_DIRECT_FSM_YAML_READ,
        message:
          "runner reads *.fsm.yaml directly; let the FSM engine parse the YAML via fsm-next / fsm-commit",
      });
    }

    // Rule: no-orchestrator-llm-call.
    for (const pattern of LLM_CALL_PATTERNS) {
      if (pattern.test(line)) {
        diagnostics.push({
          file: filePath,
          line: lineNumber,
          rule: RULES.NO_ORCHESTRATOR_LLM_CALL,
          message:
            "runner appears to call an LLM tool directly; dispatch workers via the FSM engine's worker contract instead",
        });
        break;
      }
    }
  }

  // Rule: no-inline-prompt-composition. Scan for template literals
  // (backtick strings) spanning two or more source lines and check
  // whether the captured body contains both a role marker and a
  // schema marker. We walk the source character-by-character so
  // multi-line literals are reconstructed reliably.
  const templates = collectTemplateLiterals(source);
  for (const tmpl of templates) {
    const span = tmpl.endLine - tmpl.startLine;
    if (span < 1) continue; // single-line literal: not "inline composition"
    const hasRole = ROLE_MARKERS.some((re) => re.test(tmpl.body));
    const hasSchema = SCHEMA_MARKERS.some((re) => re.test(tmpl.body));
    if (!(hasRole && hasSchema)) continue;
    // Check ignore marker on the literal's first line or the line
    // immediately before it.
    const startLine = tmpl.startLine;
    const startIdx = startLine - 1;
    const prevIdx = startIdx - 1;
    const startLineText = lines[startIdx] ?? "";
    const prevLineText = prevIdx >= 0 ? lines[prevIdx] : "";
    if (
      startLineText.includes(IGNORE_MARKER) ||
      prevLineText.includes(IGNORE_MARKER)
    ) {
      continue;
    }
    diagnostics.push({
      file: filePath,
      line: startLine,
      rule: RULES.NO_INLINE_PROMPT_COMPOSITION,
      message:
        "inline multi-line prompt with role + schema markers; move to fsm/prompt-templates/*.md and compose via @ctxr/fsm/prompt-fragments",
    });
  }

  return diagnostics;
}

// Walk the source and capture every backtick template literal as
// { startLine, endLine, body }. Skips template literals that appear
// inside comments. This is intentionally simple: it does not parse
// nested `${ ... }` expressions, which is fine for the role+schema
// heuristic (the markers we look for are not inside expression
// blocks in practice).
function collectTemplateLiterals(source) {
  const out = [];
  let i = 0;
  let line = 1;
  const len = source.length;

  while (i < len) {
    const ch = source[i];

    // Skip line comments.
    if (ch === "/" && source[i + 1] === "/") {
      while (i < len && source[i] !== "\n") i++;
      continue;
    }
    // Skip block comments.
    if (ch === "/" && source[i + 1] === "*") {
      i += 2;
      while (i < len && !(source[i] === "*" && source[i + 1] === "/")) {
        if (source[i] === "\n") line++;
        i++;
      }
      i += 2; // skip closing */
      continue;
    }
    // Skip single-quoted strings.
    if (ch === "'") {
      i++;
      while (i < len && source[i] !== "'") {
        if (source[i] === "\\") i++;
        if (source[i] === "\n") line++;
        i++;
      }
      i++;
      continue;
    }
    // Skip double-quoted strings.
    if (ch === '"') {
      i++;
      while (i < len && source[i] !== '"') {
        if (source[i] === "\\") i++;
        if (source[i] === "\n") line++;
        i++;
      }
      i++;
      continue;
    }
    // Capture a template literal.
    if (ch === "`") {
      const startLine = line;
      const bodyStart = i + 1;
      i++;
      while (i < len && source[i] !== "`") {
        if (source[i] === "\\") {
          i += 2;
          continue;
        }
        if (source[i] === "\n") line++;
        i++;
      }
      const endLine = line;
      const body = source.slice(bodyStart, i);
      i++; // skip closing backtick
      out.push({ startLine, endLine, body });
      continue;
    }
    if (ch === "\n") line++;
    i++;
  }

  return out;
}

export function formatDiagnostic(d) {
  return `${d.file}:${d.line}: ${d.rule}: ${d.message}`;
}

export function lintPaths(paths) {
  const allDiagnostics = [];
  const missing = [];
  const unreadable = [];
  for (const rawPath of paths) {
    const abs = resolve(process.cwd(), rawPath);
    if (!existsSync(abs)) {
      missing.push(rawPath);
      continue;
    }
    let source;
    try {
      source = readFileSync(abs, "utf8");
    } catch (err) {
      // `existsSync` only checks `stat`; a directory, an unreadable
      // file (EISDIR / EPERM / etc.) or a broken symlink will still
      // throw here. Surface it as a structured "unreadable" entry
      // rather than crashing the CLI with a stack trace; the lint
      // pass is advisory and should continue scanning siblings.
      unreadable.push({ path: rawPath, error: err.code ?? err.message });
      continue;
    }
    const diags = lintFile(rawPath, source);
    allDiagnostics.push(...diags);
  }
  return { diagnostics: allDiagnostics, missing, unreadable };
}

function isDirectInvocation() {
  // True when this module is the program's entry point.
  if (!process.argv[1]) return false;
  try {
    const entry = resolve(process.argv[1]);
    // fileURLToPath decodes URL escapes (e.g. %20 for spaces) and
    // produces an OS-native path on Windows, where `new URL(...).pathname`
    // gives a leading-slash POSIX-style string that does not match
    // `resolve(process.argv[1])`. Decode first; compare second.
    const self = fileURLToPath(import.meta.url);
    if (entry === self) return true;
    // npm installs a bin entry by symlinking node_modules/.bin/<name> to
    // the actual script. When the CLI is launched via the symlink,
    // process.argv[1] is the symlink path while import.meta.url is the
    // resolved script path, so a literal string compare misses. Resolve
    // both sides through realpath to handle this case (silently fall
    // back to the literal compare if realpath fails for either side).
    let entryReal = entry;
    let selfReal = self;
    try { entryReal = realpathSync(entry); } catch { /* keep literal */ }
    try { selfReal = realpathSync(self); } catch { /* keep literal */ }
    return entryReal === selfReal;
  } catch {
    return false;
  }
}

if (isDirectInvocation()) {
  const args = process.argv.slice(2);
  if (args.includes("--help") || args.includes("-h")) {
    process.stdout.write(USAGE);
    process.exit(0);
  }
  if (args.length === 0) {
    process.stderr.write(
      "fsm-lint-runner: at least one runner file path is required\n",
    );
    process.stderr.write(USAGE);
    process.exit(2);
  }

  const { diagnostics, missing, unreadable } = lintPaths(args);

  for (const m of missing) {
    process.stderr.write(`fsm-lint-runner: file not found: ${m}\n`);
  }
  for (const u of unreadable) {
    process.stderr.write(
      `fsm-lint-runner: unreadable: ${u.path} (${u.error})\n`,
    );
  }

  for (const d of diagnostics) {
    process.stdout.write(`${formatDiagnostic(d)}\n`);
  }

  const failed =
    diagnostics.length > 0 || missing.length > 0 || unreadable.length > 0;
  process.exit(failed ? 1 : 0);
}
