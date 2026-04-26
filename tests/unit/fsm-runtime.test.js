// fsm-runtime.test.js — integration tests for fsm-next + fsm-commit +
// fsm-inspect over a fixture FSM. Drives the full new-run → multi-state
// advance → terminal cycle in an isolated temp directory.
//
// Each test creates an isolated workdir (which holds the FSM YAML, worker
// stub, and storage root). The CLIs are invoked with explicit
// --fsm-path and --storage-root flags so no .fsmrc.json discovery interferes.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "scripts",
);

const MINIMAL_FSM = `
fsm:
  id: smoke
  version: 1
  entry: a
  states:
    - id: a
      purpose: "Entry; produces x."
      preconditions: []
      worker:
        role: stub
        prompt_template: workers/stub.md
        inputs: ["args"]
        response_schema:
          type: object
          required: [x]
          properties:
            x: { type: integer, minimum: 0 }
      outputs: ["x"]
      transitions:
        - to: b
          when:
            kind: deterministic
            expression: "x > 0"
        - to: c
          when:
            kind: deterministic
            expression: "x == 0"
    - id: b
      purpose: "Mid; produces y."
      preconditions: ["x exists"]
      worker:
        role: stub
        prompt_template: workers/stub.md
        inputs: ["x"]
        response_schema:
          type: object
          required: [y]
          properties:
            y: { type: string, minLength: 1 }
      outputs: ["y"]
      transitions:
        - to: c
          when: always
    - id: c
      purpose: "Terminal."
      preconditions: []
      outputs: []
      transitions: []
`;

function setupFixture() {
  const tmp = mkdtempSync(join(tmpdir(), "fsm-runtime-"));
  // Workdir layout:
  //   tmp/fsm.yaml       — the FSM definition
  //   tmp/workers/stub.md — worker prompt referenced by the FSM
  //   tmp/store/         — storage root (run dirs land under here)
  writeFileSync(join(tmp, "fsm.yaml"), MINIMAL_FSM);
  mkdirSync(join(tmp, "workers"));
  writeFileSync(join(tmp, "workers", "stub.md"), "# stub worker\n");
  mkdirSync(join(tmp, "store"));
  return tmp;
}

function runScript(name, args, opts = {}) {
  return spawnSync("node", [join(SCRIPT_DIR, name), ...args], {
    encoding: "utf8",
    cwd: opts.cwd ?? process.cwd(),
  });
}

function parseJsonStdout(result) {
  if (result.status !== 0) {
    throw new Error(
      `script exited ${result.status}; stderr: ${result.stderr}; stdout: ${result.stdout}`,
    );
  }
  return JSON.parse(result.stdout);
}

function commonArgs(tmp) {
  return ["--fsm-path", join(tmp, "fsm.yaml"), "--storage-root", join(tmp, "store")];
}

// ─── fsm-next --new-run ────────────────────────────────────────────────

test("fsm-next --new-run: creates run, returns entry-state brief, holds lock", () => {
  const tmp = setupFixture();
  try {
    const result = runScript(
      "fsm-next.mjs",
      [
        "--new-run",
        "--repo", "testrepo",
        "--base-sha", "aaa",
        "--head-sha", "bbb",
        "--session-id", "test-session",
        "--args", JSON.stringify({ scope: "all" }),
        ...commonArgs(tmp),
      ],
    );
    const brief = parseJsonStdout(result);
    assert.equal(brief.ok, true);
    assert.match(brief.run_id, /^\d{8}-\d{6}-[0-9a-f]{7}$/);
    assert.equal(brief.fsm_id, "smoke");
    assert.equal(brief.state, "a");
    assert.equal(brief.has_worker, true);
    assert.equal(brief.worker.role, "stub");
    assert.deepEqual(brief.transitions.map((t) => t.to), ["b", "c"]);
    assert.deepEqual(brief.inputs, { args: { scope: "all" } });
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-next --new-run rejects unknown args", () => {
  const result = runScript("fsm-next.mjs", ["--bogus"]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unknown argument/);
});

// ─── fsm-commit: deterministic predicate routing ───────────────────────

test("fsm-commit: deterministic predicate routes a→b when x>0", () => {
  const tmp = setupFixture();
  try {
    const session = "test-session-2";
    const newRun = parseJsonStdout(
      runScript("fsm-next.mjs", [
        "--new-run", "--repo", "testrepo", "--base-sha", "aaa", "--head-sha", "bbb",
        "--session-id", session, "--args", "{}",
        ...commonArgs(tmp),
      ]),
    );
    const commit = parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({ x: 5 }),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(commit.ok, true);
    assert.equal(commit.advanced_from, "a");
    assert.equal(commit.state, "b");
    assert.deepEqual(commit.inputs, { x: 5 });
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-commit: deterministic predicate routes a→c when x==0 (skipping b)", () => {
  const tmp = setupFixture();
  try {
    const session = "test-session-3";
    const newRun = parseJsonStdout(
      runScript("fsm-next.mjs", [
        "--new-run", "--repo", "testrepo", "--base-sha", "aaa", "--head-sha", "bbb",
        "--session-id", session, "--args", "{}",
        ...commonArgs(tmp),
      ]),
    );
    const commit = parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({ x: 0 }),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(commit.ok, true);
    assert.equal(commit.advanced_from, "a");
    assert.equal(commit.state, "c");
    assert.equal(commit.transitions.length, 0);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── full cycle to terminal ────────────────────────────────────────────

test("fsm-commit: reaches terminal after full a→b→c cycle", () => {
  const tmp = setupFixture();
  try {
    const session = "test-session-4";
    const newRun = parseJsonStdout(
      runScript("fsm-next.mjs", [
        "--new-run", "--repo", "testrepo", "--base-sha", "aaa", "--head-sha", "bbb",
        "--session-id", session, "--args", "{}",
        ...commonArgs(tmp),
      ]),
    );
    parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({ x: 5 }),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({ y: "all-good" }),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    const final = parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({}),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(final.ok, true);
    assert.equal(final.status, "terminal");
    assert.equal(final.state, "c");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── schema-violation fault path ───────────────────────────────────────

test("fsm-commit: invalid worker output triggers fault + manifest faulted", () => {
  const tmp = setupFixture();
  try {
    const session = "test-session-5";
    const newRun = parseJsonStdout(
      runScript("fsm-next.mjs", [
        "--new-run", "--repo", "testrepo", "--base-sha", "aaa", "--head-sha", "bbb",
        "--session-id", session, "--args", "{}",
        ...commonArgs(tmp),
      ]),
    );
    const result = runScript("fsm-commit.mjs", [
      "--run-id", newRun.run_id,
      "--outputs", JSON.stringify({ x: -1 }),
      "--session-id", session,
      ...commonArgs(tmp),
    ]);
    assert.notEqual(result.status, 0);
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.error, "output_schema_violation");
    assert.match(payload.errors.join(" "), />=/);
    const inspect = parseJsonStdout(
      runScript("fsm-inspect.mjs", [
        "--run-id", newRun.run_id,
        "--storage-root", join(tmp, "store"),
      ]),
    );
    assert.equal(inspect.manifest.status, "faulted");
    assert.equal(inspect.lock, null);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── lock conflict ─────────────────────────────────────────────────────

test("fsm-commit: refuses when caller does not hold the lock", () => {
  const tmp = setupFixture();
  try {
    const session = "test-session-6";
    const newRun = parseJsonStdout(
      runScript("fsm-next.mjs", [
        "--new-run", "--repo", "testrepo", "--base-sha", "aaa", "--head-sha", "bbb",
        "--session-id", session, "--args", "{}",
        ...commonArgs(tmp),
      ]),
    );
    const result = runScript("fsm-commit.mjs", [
      "--run-id", newRun.run_id,
      "--outputs", JSON.stringify({ x: 5 }),
      "--session-id", "different-session",
      ...commonArgs(tmp),
    ]);
    assert.notEqual(result.status, 0);
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.error, "lock_not_held");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── resume after pause ────────────────────────────────────────────────

test("fsm-next --resume: rejects completed runs with run_not_resumable", () => {
  const tmp = setupFixture();
  try {
    const session = "test-session-7";
    const newRun = parseJsonStdout(
      runScript("fsm-next.mjs", [
        "--new-run", "--repo", "testrepo", "--base-sha", "aaa", "--head-sha", "bbb",
        "--session-id", session, "--args", "{}",
        ...commonArgs(tmp),
      ]),
    );
    parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({ x: 5 }),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({ y: "complete" }),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({}),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    const rejected = runScript("fsm-next.mjs", [
      "--resume", newRun.run_id,
      "--session-id", session,
      ...commonArgs(tmp),
    ]);
    assert.notEqual(rejected.status, 0);
    const payload = JSON.parse(rejected.stdout);
    assert.equal(payload.error, "run_not_resumable");
    assert.equal(payload.status, "completed");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── fsm-inspect ───────────────────────────────────────────────────────

test("fsm-inspect: returns manifest + lock + ordered trace records", () => {
  const tmp = setupFixture();
  try {
    const session = "test-session-8";
    const newRun = parseJsonStdout(
      runScript("fsm-next.mjs", [
        "--new-run", "--repo", "testrepo", "--base-sha", "aaa", "--head-sha", "bbb",
        "--session-id", session, "--args", "{}",
        ...commonArgs(tmp),
      ]),
    );
    const result = parseJsonStdout(
      runScript("fsm-inspect.mjs", [
        "--run-id", newRun.run_id,
        "--storage-root", join(tmp, "store"),
      ]),
    );
    assert.equal(result.ok, true);
    assert.equal(result.run_id, newRun.run_id);
    assert.equal(result.manifest.fsm_id, "smoke");
    assert.equal(result.manifest.status, "in_progress");
    assert.ok(result.lock);
    assert.equal(result.lock.session_id, session);
    assert.equal(result.trace_count, 1);
    assert.equal(result.trace[0].phase, "entry");
    assert.equal(result.trace[0].state, "a");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-inspect: returns error for unknown run", () => {
  const tmp = setupFixture();
  try {
    const result = runScript("fsm-inspect.mjs", [
      "--run-id", "20260101-000000-fffffff",
      "--storage-root", join(tmp, "store"),
    ]);
    assert.notEqual(result.status, 0);
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.error, "run_not_found");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── config-file fallback ──────────────────────────────────────────────

test("fsm-next: reads legacy single-FSM .fsmrc.json when --fsm-path / --storage-root are omitted", () => {
  const tmp = setupFixture();
  try {
    writeFileSync(
      join(tmp, ".fsmrc.json"),
      JSON.stringify({
        fsm_path: "fsm.yaml",
        storage_root: "store",
      }),
    );
    const result = runScript(
      "fsm-next.mjs",
      [
        "--new-run",
        "--repo", "testrepo",
        "--base-sha", "aaa",
        "--head-sha", "bbb",
        "--session-id", "config-session",
        "--args", "{}",
      ],
      { cwd: tmp },
    );
    const brief = parseJsonStdout(result);
    assert.equal(brief.ok, true);
    assert.equal(brief.state, "a");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-next: reads multi-FSM .fsmrc.json with --fsm <name> selector", () => {
  const tmp = setupFixture();
  try {
    // Add a second FSM definition to make the multi-FSM scenario real.
    writeFileSync(
      join(tmp, "other.fsm.yaml"),
      `
fsm:
  id: other
  version: 1
  entry: only
  states:
    - id: only
      purpose: "Singleton."
      preconditions: []
      outputs: []
      transitions: []
`,
    );
    writeFileSync(
      join(tmp, ".fsmrc.json"),
      JSON.stringify({
        fsms: [
          { name: "primary", fsm_path: "fsm.yaml", storage_root: "store" },
          { name: "other", fsm_path: "other.fsm.yaml", storage_root: "other-store" },
        ],
      }),
    );
    const result = runScript(
      "fsm-next.mjs",
      [
        "--fsm", "other",
        "--new-run",
        "--repo", "testrepo",
        "--base-sha", "aaa", "--head-sha", "bbb",
        "--session-id", "multi-config",
        "--args", "{}",
      ],
      { cwd: tmp },
    );
    const brief = parseJsonStdout(result);
    assert.equal(brief.ok, true);
    assert.equal(brief.fsm_id, "other");
    assert.equal(brief.state, "only");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-next: errors when multiple FSMs configured and --fsm not given", () => {
  const tmp = setupFixture();
  try {
    writeFileSync(
      join(tmp, "other.fsm.yaml"),
      `
fsm:
  id: other
  version: 1
  entry: only
  states:
    - id: only
      purpose: "Singleton."
      preconditions: []
      outputs: []
      transitions: []
`,
    );
    writeFileSync(
      join(tmp, ".fsmrc.json"),
      JSON.stringify({
        fsms: [
          { name: "primary", fsm_path: "fsm.yaml", storage_root: "store" },
          { name: "other", fsm_path: "other.fsm.yaml", storage_root: "other-store" },
        ],
      }),
    );
    const result = runScript(
      "fsm-next.mjs",
      ["--new-run", "--repo", "testrepo", "--base-sha", "a", "--head-sha", "b", "--args", "{}"],
      { cwd: tmp },
    );
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /multiple FSMs configured/);
    assert.match(result.stderr, /primary, other/);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-next: errors when --fsm <name> not found in config", () => {
  const tmp = setupFixture();
  try {
    writeFileSync(
      join(tmp, ".fsmrc.json"),
      JSON.stringify({
        fsms: [
          { name: "primary", fsm_path: "fsm.yaml", storage_root: "store" },
        ],
      }),
    );
    const result = runScript(
      "fsm-next.mjs",
      ["--fsm", "ghost", "--new-run", "--repo", "x", "--base-sha", "a", "--head-sha", "b", "--args", "{}"],
      { cwd: tmp },
    );
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /not found/);
    assert.match(result.stderr, /Available: primary/);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-next: errors with a helpful message when there is no config and no flags", () => {
  const tmp = setupFixture();
  try {
    const result = runScript(
      "fsm-next.mjs",
      ["--new-run", "--repo", "x", "--base-sha", "a", "--head-sha", "b", "--args", "{}"],
      { cwd: tmp },
    );
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /no FSM configured/);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});
