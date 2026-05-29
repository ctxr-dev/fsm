// fsm-validate-static.test.js — drives the CLI against valid + invalid
// FSM YAML fixtures and asserts the JSON report shape and exit codes.

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
  "fsm-validate-static.mjs",
);

function runCli(args, opts = {}) {
  return spawnSync("node", [CLI, ...args], {
    encoding: "utf8",
    cwd: opts.cwd,
  });
}

function tmpDir() {
  return mkdtempSync(join(tmpdir(), "fsm-static-"));
}

function writeFsm(dir, contents) {
  const path = join(dir, "fsm.yaml");
  writeFileSync(path, contents);
  return path;
}

const VALID_FSM = `
fsm:
  id: minimal
  version: 1
  entry: a
  states:
    - id: a
      purpose: "Entry."
      preconditions: []
      outputs: ["x"]
      transitions:
        - to: b
          when: always
    - id: b
      purpose: "Terminal."
      preconditions: ["x exists"]
      outputs: []
      transitions: []
`;

test("exits non-zero when no path is given", () => {
  const r = runCli([]);
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /at least one FSM YAML path/);
});

test("happy-path FSM validates clean (exit 0, ok=true)", () => {
  const dir = tmpDir();
  try {
    writeFsm(dir, VALID_FSM);
    const r = runCli(["fsm.yaml"], { cwd: dir });
    assert.equal(r.status, 0, `stderr: ${r.stderr}\nstdout: ${r.stdout}`);
    const report = JSON.parse(r.stdout);
    assert.equal(report.ok, true);
    assert.equal(report.total_errors, 0);
    assert.equal(report.files[0].phase, "passed");
    assert.equal(report.files[0].states, 2);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("missing file returns error report (exit 1, file-not-found)", () => {
  const dir = tmpDir();
  try {
    const r = runCli(["does-not-exist.yaml"], { cwd: dir });
    assert.equal(r.status, 1);
    const report = JSON.parse(r.stdout);
    assert.equal(report.ok, false);
    assert.match(report.files[0].errors[0], /File not found/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("malformed YAML returns parse error", () => {
  const dir = tmpDir();
  try {
    writeFsm(dir, "this: is: not: valid: yaml:\n  - [malformed");
    const r = runCli(["fsm.yaml"], { cwd: dir });
    assert.equal(r.status, 1);
    const report = JSON.parse(r.stdout);
    assert.match(report.files[0].errors[0], /YAML parse failure/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("schema-invalid FSM returns errors with phase=schema", () => {
  const dir = tmpDir();
  try {
    writeFsm(
      dir,
      `
fsm:
  id: x
  version: 1
  entry: a
  states:
    - id: BadStateId
      purpose: ""
      preconditions: []
      outputs: []
      transitions: []
`,
    );
    const r = runCli(["fsm.yaml"], { cwd: dir });
    assert.equal(r.status, 1);
    const report = JSON.parse(r.stdout);
    assert.equal(report.ok, false);
    assert.equal(report.files[0].phase, "schema");
    assert.match(report.files[0].errors.join(" "), /snake_case/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("static-invalid FSM (unreachable state) returns phase=static", () => {
  const dir = tmpDir();
  try {
    writeFsm(
      dir,
      `
fsm:
  id: x
  version: 1
  entry: a
  states:
    - id: a
      purpose: "Entry."
      preconditions: []
      outputs: []
      transitions: []
    - id: orphan
      purpose: "Unreachable."
      preconditions: []
      outputs: []
      transitions: []
`,
    );
    const r = runCli(["fsm.yaml"], { cwd: dir });
    assert.equal(r.status, 1);
    const report = JSON.parse(r.stdout);
    assert.equal(report.files[0].phase, "static");
    assert.match(report.files[0].errors.join(" "), /unreachable/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("multiple files: one valid + one invalid → exit 1, both reported", () => {
  const dir = tmpDir();
  try {
    writeFsm(dir, VALID_FSM);
    writeFileSync(
      join(dir, "bad.yaml"),
      `
fsm:
  id: x
  version: 1
  entry: missing_state
  states:
    - id: a
      purpose: "Entry."
      preconditions: []
      outputs: []
      transitions: []
`,
    );
    const r = runCli(["fsm.yaml", "bad.yaml"], { cwd: dir });
    assert.equal(r.status, 1);
    const report = JSON.parse(r.stdout);
    assert.equal(report.files.length, 2);
    assert.equal(report.files[0].phase, "passed");
    assert.equal(report.files[1].phase, "static");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
