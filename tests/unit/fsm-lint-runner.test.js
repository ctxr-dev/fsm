// fsm-lint-runner.test.js -- exercises the orchestrator-shell-only lint
// helper. Each test writes a fixture runner into a tmp directory, runs
// the linter against it via spawnSync, and asserts on (a) exit code,
// (b) stdout diagnostic shape, and (c) which rule fired.
//
// Covers: clean runner (exit 0), readFileSync(".fsm.yaml") trigger
// (exit 1), inline multi-line prompt literal trigger (exit 1), and the
// --help short-circuit (exit 0 + usage on stdout).

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const CLI = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "scripts",
  "fsm-lint-runner.mjs",
);

function runCli(args, opts = {}) {
  return spawnSync("node", [CLI, ...args], {
    encoding: "utf8",
    cwd: opts.cwd,
  });
}

function tmpDir() {
  return mkdtempSync(join(tmpdir(), "fsm-lint-"));
}

function write(dir, name, contents) {
  const path = join(dir, name);
  writeFileSync(path, contents);
  return path;
}

const CLEAN_RUNNER = `#!/usr/bin/env node
// Clean orchestrator-shell runner: only shells out to the FSM CLIs.

import { spawnSync } from "node:child_process";

const next = spawnSync("fsm-next", ["--run-id", process.argv[2]], {
  encoding: "utf8",
});
const brief = JSON.parse(next.stdout);
console.log(JSON.stringify({ brief }));
`;

const DIRECT_YAML_READ_RUNNER = `#!/usr/bin/env node
// Bad: reads the FSM YAML directly.

import { readFileSync } from "node:fs";

const raw = readFileSync("./fsm/explorer.fsm.yaml", "utf8");
console.log(raw.length);
`;

// A multi-line template literal with BOTH a role marker ("You are")
// and a schema marker ("$schema") -- the two conditions that trip
// no-inline-prompt-composition.
const INLINE_PROMPT_RUNNER = [
  "#!/usr/bin/env node",
  "// Bad: composes a worker prompt inline with role + schema markers.",
  "",
  "const prompt = `",
  "You are a codebase-explorer worker.",
  "",
  "Output Contract:",
  "  $schema: http://json-schema.org/draft-07/schema#",
  '  "type": "object"',
  '  "required": ["findings"]',
  "`;",
  "console.log(prompt.length);",
  "",
].join("\n");

test("--help prints usage to stdout and exits 0", () => {
  const r = runCli(["--help"]);
  assert.equal(r.status, 0, `stderr: ${r.stderr}`);
  assert.match(r.stdout, /Usage: fsm-lint-runner/);
  assert.equal(r.stderr, "");
});

test("clean runner produces no diagnostics and exits 0", () => {
  const dir = tmpDir();
  try {
    write(dir, "clean-runner.mjs", CLEAN_RUNNER);
    const r = runCli(["clean-runner.mjs"], { cwd: dir });
    assert.equal(r.status, 0, `stdout: ${r.stdout}\nstderr: ${r.stderr}`);
    assert.equal(r.stdout, "");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readFileSync(\".fsm.yaml\") fires no-direct-fsm-yaml-read and exits 1", () => {
  const dir = tmpDir();
  try {
    write(dir, "bad-yaml-read.mjs", DIRECT_YAML_READ_RUNNER);
    const r = runCli(["bad-yaml-read.mjs"], { cwd: dir });
    assert.equal(r.status, 1, `stdout: ${r.stdout}\nstderr: ${r.stderr}`);
    // Diagnostic format: <file>:<line>: <rule>: <message>
    assert.match(
      r.stdout,
      /bad-yaml-read\.mjs:\d+: no-direct-fsm-yaml-read: /,
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("inline multi-line prompt with role+schema markers fires no-inline-prompt-composition and exits 1", () => {
  const dir = tmpDir();
  try {
    write(dir, "bad-inline-prompt.mjs", INLINE_PROMPT_RUNNER);
    const r = runCli(["bad-inline-prompt.mjs"], { cwd: dir });
    assert.equal(r.status, 1, `stdout: ${r.stdout}\nstderr: ${r.stderr}`);
    assert.match(
      r.stdout,
      /bad-inline-prompt\.mjs:\d+: no-inline-prompt-composition: /,
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("missing file is reported on stderr and exits 1", () => {
  const dir = tmpDir();
  try {
    const r = runCli(["does-not-exist.mjs"], { cwd: dir });
    assert.equal(r.status, 1);
    assert.match(r.stderr, /file not found: does-not-exist\.mjs/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("no arguments and no --help exits 2 with usage on stderr", () => {
  const r = runCli([]);
  assert.equal(r.status, 2);
  assert.match(r.stderr, /at least one runner file path is required/);
  assert.match(r.stderr, /Usage: fsm-lint-runner/);
});

test("fsm-lint:ignore on the offending line suppresses the diagnostic", () => {
  const dir = tmpDir();
  try {
    const SUPPRESSED = [
      "#!/usr/bin/env node",
      "import { readFileSync } from \"node:fs\";",
      "// Legit reason: test fixture. // fsm-lint:ignore",
      "const raw = readFileSync(\"./fsm/explorer.fsm.yaml\", \"utf8\"); // fsm-lint:ignore",
      "console.log(raw.length);",
      "",
    ].join("\n");
    write(dir, "suppressed.mjs", SUPPRESSED);
    const r = runCli(["suppressed.mjs"], { cwd: dir });
    assert.equal(r.status, 0, `stdout: ${r.stdout}\nstderr: ${r.stderr}`);
    assert.equal(r.stdout, "");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
