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
//     Pattern: a token-shape scan that fires when the line contains a
//     known LLM-dispatch token. Two token shapes are matched:
//       - bare property paths like `anthropic.messages` /
//         `client.messages` (no trailing `(` required), so accessing
//         the LLM client surface also counts.
//       - call shapes like `messages.create(`, `Task(`, `Agent(`,
//         `Skill(`, `Bash(`, `WebFetch(`, `WebSearch(`.
//     This is NOT a call-expression detector: a function declaration
//     (`function Task() { ... }`), object method definition
//     (`{ Task() { ... } }`), or destructure (`{ Task } = ...`)
//     followed by `Task(` on the same line will also match. Authors
//     suppress legitimate occurrences with `// fsm-lint:ignore`.
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
  // Match the documented `"required":` marker on its own (object or
  // array RHS, or even with the RHS missing in a partial fixture).
  // Previously the regex required `[` immediately after the colon,
  // contradicting the header comment and producing surprising
  // false negatives.
  /"required"\s*:/,
  /response_schema/,
];

// Suppression is documented as `// fsm-lint:ignore`. Returning true only
// when the marker lives in a `//` line comment prevents bypassing
// diagnostics by embedding the literal string inside source like
// `console.log("fsm-lint:ignore")`. The scan walks the line tracking
// string-literal state so a `//` inside a string (e.g. a URL like
// "http://x") is NOT treated as a comment start.
// Characters that, as the previous non-whitespace token, indicate the
// next `/` opens a regex literal rather than a division operator.
// Conservative set: punctuation that cannot end an expression, plus
// the empty prefix (regex at start of line). Distinguishing regex
// vs division in JavaScript is in general context-sensitive (e.g.
// `a /b/g` vs `a / b / g`), but for the purpose of a lint heuristic
// this set covers the common cases without ever mis-treating a real
// `//` line comment as inside a regex.
const REGEX_PREFIX_OPS = new Set([
  "", "(", "[", "{", "}", ",", ";", ":", "?", "!", "&", "|", "=",
  "+", "-", "*", "/", "^", "~", "%", "<", ">",
]);

function lineCommentHasIgnoreMarker(line) {
  if (typeof line !== "string" || !line.includes(IGNORE_MARKER)) return false;
  let i = 0;
  const len = line.length;
  let inString = null; // null | "'" | '"' | "`"
  let inRegex = false;
  let lastNonWs = "";
  while (i < len) {
    const ch = line[i];
    if (inString) {
      if (ch === "\\") {
        i += 2;
        continue;
      }
      if (ch === inString) {
        inString = null;
      }
      i++;
      continue;
    }
    if (inRegex) {
      if (ch === "\\") {
        i += 2;
        continue;
      }
      if (ch === "[") {
        // Walk a regex character class so that `/` inside `[...]` does
        // not terminate the literal.
        i++;
        while (i < len && line[i] !== "]") {
          if (line[i] === "\\") i++;
          i++;
        }
        i++;
        continue;
      }
      if (ch === "/") {
        inRegex = false;
        i++;
        continue;
      }
      i++;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === "`") {
      inString = ch;
      lastNonWs = ch;
      i++;
      continue;
    }
    if (ch === "/" && line[i + 1] === "/") {
      // Real line comment starts here.
      return line.indexOf(IGNORE_MARKER, i) >= 0;
    }
    if (ch === "/" && REGEX_PREFIX_OPS.has(lastNonWs)) {
      // Treat as regex literal start; consume until matching `/`.
      inRegex = true;
      i++;
      continue;
    }
    if (ch !== " " && ch !== "\t") lastNonWs = ch;
    i++;
  }
  return false;
}

export function lintFile(filePath, source) {
  const diagnostics = [];
  const lines = source.split(/\r?\n/);

  const readApiAlternation = READ_APIS.join("|");
  // Require the .fsm.yaml occurrence to be inside a quoted string
  // literal: the argument expression up to the `.fsm.yaml` occurrence
  // is matched lazily, then the suffix must end inside the SAME quote
  // that opened it. Inline block comments inside the call
  // (`readFileSync(/* ".fsm.yaml" */ x)`) ARE stripped before the
  // regex runs (see scrub-inline-block-comments below), so a quoted
  // `.fsm.yaml` literal that lives only inside a `/* ... */` segment
  // is not matched.
  const readApiRegex = new RegExp(
    `\\b(?:${readApiAlternation})\\s*\\([^)\\n]*?(['"\`])[^'"\`\\n]*\\.fsm\\.yaml[^'"\`\\n]*\\1`,
  );
  // Strip inline `/* ... */` segments from a single line for the
  // per-line regexes. Stops at the first newline (block comments that
  // span lines are handled by the inBlockComment state machine
  // below).
  const stripInlineBlockComments = (s) => s.replace(/\/\*[^\n]*?\*\//g, "");

  // Pre-compute the set of line numbers that fall STRICTLY INSIDE a
  // multi-line template literal so the per-line rules can skip them
  // (those bodies are owned by no-inline-prompt-composition, and the
  // YAML-read / LLM-call regexes should not fire on prompt-fixture
  // prose). The start and end lines are intentionally NOT included:
  // executable code can legitimately share a line with the opening
  // backtick (`foo(` ... `)`) or follow the closing backtick on the
  // same line (`` `...`; readFileSync("x.fsm.yaml") ``), and the
  // per-line rules must still catch violations there.
  const templateLineSet = new Set();
  for (const tmpl of collectTemplateLiterals(source)) {
    if (tmpl.endLine > tmpl.startLine + 1) {
      for (let n = tmpl.startLine + 1; n < tmpl.endLine; n++) {
        templateLineSet.add(n);
      }
    }
  }

  let inBlockComment = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const lineNumber = i + 1;
    // Track multi-line block-comment state. To avoid false positives
    // from `/*` or `*/` substrings that appear inside string literals
    // (e.g. `const s = "/*";` or `console.log("*/")`), only flip the
    // state on lines that ACTUALLY START with the delimiter (after
    // leading whitespace). This is a conservative heuristic that may
    // miss exotic shapes but never wrongly skips real code.
    const trimmedForBc = line.trimStart();
    const isBlockClosingLine = inBlockComment && trimmedForBc.startsWith("*/");
    if (inBlockComment) {
      // Close on a `*/`-led line. The closing line itself is treated
      // as comment for the per-line rules via the isComment check
      // below; we keep `inBlockComment` true through this iteration
      // and flip it off at the end so subsequent iterations resume.
      if (isBlockClosingLine) inBlockComment = false;
    } else if (
      trimmedForBc.startsWith("/*") &&
      !trimmedForBc.includes("*/")
    ) {
      // Opens a multi-line block on this line.
      inBlockComment = true;
    }
    // `inBlockComment` reflects the state AFTER this line; the
    // current line is fully comment if it WAS in a block (started on
    // a prior line and continues / closes here) or opens a new
    // multi-line block. A SINGLE-LINE block comment that closes on
    // the same line (`/* x */ readFileSync("x.fsm.yaml")`) is NOT
    // wholly comment; real code can follow `*/` on the same line and
    // must still be analysed by the per-line rules.
    const isSingleLineBlockComment =
      trimmedForBc.startsWith("/*") && trimmedForBc.includes("*/");
    const lineIsBlockComment =
      (inBlockComment || isBlockClosingLine || trimmedForBc.startsWith("/*")) &&
      !isSingleLineBlockComment;
    // The fsm-lint:ignore marker is intentionally per-line for the
    // single-line rules (no-direct-fsm-yaml-read and
    // no-orchestrator-llm-call); previous-line suppression is
    // reserved for the multi-line template-literal rule, where the
    // marker can legitimately precede the literal it suppresses.
    // Applying previous-line suppression to per-line rules silently
    // hid the diagnostic immediately after any other ignored line.
    // Additionally: require the marker to live in a `//` line comment
    // so the substring inside a string literal (e.g.
    // `console.log("fsm-lint:ignore")`) cannot bypass diagnostics.
    if (lineCommentHasIgnoreMarker(line)) {
      continue;
    }

    // Skip lines that are clearly source comments -- both the read-API
    // and the LLM-call rules are about real code, not prose in the
    // header banner. Narrow to actual comment shapes: `//`, `/*`, and
    // `#!` (shebang). Lines inside a multi-line `/* ... */` block are
    // skipped via `inBlockComment` above, which avoids the false
    // positive where `* foo()` (a generator method definition OUTSIDE
    // a block comment) used to be treated as comment text.
    const trimmed = line.trimStart();
    const isComment =
      lineIsBlockComment ||
      trimmed.startsWith("//") ||
      trimmed.startsWith("#!");
    if (isComment) continue;

    // Skip lines that fall inside a multi-line template literal: the
    // per-line YAML-read and LLM-call rules look at literal text, not
    // executable code, and a multi-line prompt fixture can legitimately
    // contain shapes like `readFileSync("x.fsm.yaml")` or `Task(` as
    // documentation. The no-inline-prompt-composition rule already
    // owns the template-literal case.
    if (templateLineSet.has(lineNumber)) continue;

    // Strip any inline /* ... */ from the current line before the
    // per-line regexes look at it, so a `.fsm.yaml` string literal
    // sitting only inside a commented-out arg (`readFileSync(/* "x.fsm.yaml" */ y)`)
    // is not matched. The inBlockComment state above handles the
    // multi-line case; this handles a single-line inline block.
    const codeOnly = stripInlineBlockComments(line);

    // Rule: no-direct-fsm-yaml-read. Match a known read API name
    // followed by `(`, with `.fsm.yaml` appearing somewhere on the
    // same line inside a string literal.
    if (readApiRegex.test(codeOnly)) {
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
      if (pattern.test(codeOnly)) {
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
      lineCommentHasIgnoreMarker(startLineText) ||
      lineCommentHasIgnoreMarker(prevLineText)
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
        if (source[i] === "\\") {
          // Account for the escaped character: when it is a newline
          // (line-continuation), the line counter must still tick.
          if (source[i + 1] === "\n") line++;
          i += 2;
          continue;
        }
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
        if (source[i] === "\\") {
          if (source[i + 1] === "\n") line++;
          i += 2;
          continue;
        }
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
          // Account for backslash-newline (line-continuation) inside a
          // template body: stepping past the pair without ticking
          // `line` shifted endLine in the diagnostic, blaming the
          // wrong source line. Tick when we step over a literal
          // newline as part of the escape pair.
          if (source[i + 1] === "\n") line++;
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
      // `existsSync` uses an access check, so a broken symlink is
      // usually already reported as "file not found" above and
      // never reaches this catch. What can still throw here is a
      // path that exists but is unreadable (a directory -> EISDIR,
      // restrictive permissions -> EPERM/EACCES, or a transient
      // filesystem error). Surface it as a structured "unreadable"
      // entry rather than crashing the CLI with a stack trace; the
      // lint pass is advisory and should continue scanning siblings.
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
  // Set process.exitCode and let Node exit naturally after the
  // stdout/stderr streams flush, rather than calling process.exit()
  // immediately after the last write. process.exit() can truncate a
  // multi-KB diagnostics dump on piped stdio (same failure mode that
  // motivated scripts/lib/emit.mjs in this repo).
  //
  // Also swallow EPIPE on both streams: when the linter is piped to a
  // consumer that closes early (e.g. `fsm-lint-runner ... | head`)
  // Node would otherwise crash with `Error: write EPIPE`. The
  // diagnostics are advisory; a downstream reader closing the pipe
  // mid-stream is a graceful exit, but it must NOT mask a non-zero
  // verdict, so we forward process.exitCode (which the
  // computeExitCode-then-emit pattern below has already set) rather
  // than always exiting 0.
  process.stdout.on("error", (err) => {
    if (err.code === "EPIPE") process.exit(process.exitCode ?? 0);
    throw err;
  });
  process.stderr.on("error", (err) => {
    if (err.code === "EPIPE") process.exit(process.exitCode ?? 0);
    throw err;
  });
  const args = process.argv.slice(2);
  if (args.includes("--help") || args.includes("-h")) {
    process.exitCode = 0;
    process.stdout.write(USAGE);
  } else if (args.length === 0) {
    process.exitCode = 2;
    process.stderr.write(
      "fsm-lint-runner: at least one runner file path is required\n",
    );
    process.stderr.write(USAGE);
  } else {
    const { diagnostics, missing, unreadable } = lintPaths(args);

    // Compute and stamp process.exitCode BEFORE any write. If a
    // downstream pipe closes mid-write (EPIPE) the EPIPE handler reads
    // the already-set exitCode, so a verdict computed as "failed"
    // does not silently become success.
    const failed =
      diagnostics.length > 0 || missing.length > 0 || unreadable.length > 0;
    process.exitCode = failed ? 1 : 0;

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
  }
}
