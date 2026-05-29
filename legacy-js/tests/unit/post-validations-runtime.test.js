// post-validations-runtime.test.js: runtime evaluation of
// post_validations[] predicates (Workstream A5).
//
// Covers:
//   - unit: runPostValidations() with single pass, single fail, AND-composed
//     multi-predicate, malformed expression.
//   - integration: fsm-commit CLI fails a state whose post_validations
//     evaluates to false, writes a fault trace, sets manifest faulted,
//     emits structured stdout, exits 1.

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

import { runPostValidations } from "../../scripts/lib/fsm-engine.mjs";
import { readTrace } from "../../scripts/lib/fsm-storage.mjs";

const SCRIPT_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "scripts",
);

// ─── Unit: runPostValidations() ────────────────────────────────────────

test("runPostValidations: no checks → valid with empty results", () => {
  const state = { post_validations: [] };
  const result = runPostValidations(state, { findings: [] });
  assert.equal(result.valid, true);
  assert.deepEqual(result.results, []);
});

test("runPostValidations: single predicate that evaluates true", () => {
  const state = { post_validations: ["len(findings) > 0"] };
  const result = runPostValidations(state, { findings: [{ id: 1 }] });
  assert.equal(result.valid, true);
  assert.equal(result.results.length, 1);
  assert.equal(result.results[0].check, "len(findings) > 0");
  assert.equal(result.results[0].expression, "len(findings) > 0");
  assert.equal(result.results[0].result, true);
  assert.equal(result.results[0].error, undefined);
});

test("runPostValidations: single predicate that evaluates false", () => {
  const state = { post_validations: ["len(findings) > 0"] };
  const result = runPostValidations(state, { findings: [] });
  assert.equal(result.valid, false);
  assert.equal(result.results.length, 1);
  assert.equal(result.results[0].result, false);
  assert.equal(result.results[0].error, undefined);
});

test("runPostValidations: multiple predicates AND-composed (true + true is valid)", () => {
  const state = {
    post_validations: [
      "len(findings) > 0",
      "verdict == 'PASS'",
    ],
  };
  const result = runPostValidations(state, {
    findings: [{ id: 1 }, { id: 2 }],
    verdict: "PASS",
  });
  assert.equal(result.valid, true);
  assert.equal(result.results.length, 2);
  assert.equal(result.results[0].result, true);
  assert.equal(result.results[1].result, true);
});

test("runPostValidations: multiple predicates AND-composed (true + false is invalid)", () => {
  const state = {
    post_validations: [
      "len(findings) > 0",
      "verdict == 'PASS'",
    ],
  };
  const result = runPostValidations(state, {
    findings: [{ id: 1 }],
    verdict: "FAIL",
  });
  assert.equal(result.valid, false);
  assert.equal(result.results.length, 2);
  assert.equal(result.results[0].result, true);
  assert.equal(result.results[1].result, false);
});

test("runPostValidations: malformed expression returns valid=false with error captured", () => {
  const state = { post_validations: ["len(findings >"] };
  const result = runPostValidations(state, { findings: [1, 2, 3] });
  assert.equal(result.valid, false);
  assert.equal(result.results.length, 1);
  assert.equal(result.results[0].result, false);
  assert.ok(
    typeof result.results[0].error === "string" && result.results[0].error.length > 0,
    "expected malformed expression to capture an error message",
  );
});

test("runPostValidations: missing post_validations array defaults to valid=true", () => {
  const state = {};
  const result = runPostValidations(state, { findings: [] });
  assert.equal(result.valid, true);
  assert.deepEqual(result.results, []);
});

// ─── Integration: fsm-commit CLI fails on violated predicate ───────────

const POSTVAL_FSM = `
fsm:
  id: postval
  version: 1
  entry: scan
  states:
    - id: scan
      purpose: "Entry; produces findings array."
      preconditions: []
      worker:
        role: stub
        prompt_template: workers/stub.md
        inputs: ["args"]
        response_schema:
          type: object
          required: [findings]
          properties:
            findings:
              type: array
              items: { type: object }
      outputs: ["findings"]
      post_validations:
        - "len(findings) > 0"
      transitions:
        - to: terminal
          when: always
    - id: terminal
      purpose: "Terminal."
      preconditions: []
      outputs: []
      transitions: []
`;

function setupPostvalFixture() {
  const tmp = mkdtempSync(join(tmpdir(), "fsm-postval-"));
  writeFileSync(join(tmp, "fsm.yaml"), POSTVAL_FSM);
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

test("fsm-commit: post_validation_failed when predicate violated (findings empty)", () => {
  const tmp = setupPostvalFixture();
  try {
    const session = "postval-fail-session";
    const newRun = parseJsonStdout(
      runScript("fsm-next.mjs", [
        "--new-run",
        "--repo", "testrepo",
        "--base-sha", "aaa",
        "--head-sha", "bbb",
        "--session-id", session,
        "--args", "{}",
        ...commonArgs(tmp),
      ]),
    );
    const commit = runScript("fsm-commit.mjs", [
      "--run-id", newRun.run_id,
      "--outputs", JSON.stringify({ findings: [] }),
      "--session-id", session,
      ...commonArgs(tmp),
    ]);
    assert.notEqual(commit.status, 0);
    const payload = JSON.parse(commit.stdout);
    assert.equal(payload.error, "post_validation_failed");
    assert.equal(payload.state, "scan");
    assert.ok(Array.isArray(payload.post_validations));
    assert.equal(payload.post_validations.length, 1);
    assert.equal(payload.post_validations[0].check, "len(findings) > 0");
    assert.equal(payload.post_validations[0].result, false);

    // Manifest should be faulted, lock should be released, fault trace recorded.
    const inspect = parseJsonStdout(
      runScript("fsm-inspect.mjs", [
        "--run-id", newRun.run_id,
        "--storage-root", join(tmp, "store"),
      ]),
    );
    assert.equal(inspect.manifest.status, "faulted");
    assert.equal(inspect.lock, null);
    const trace = readTrace(newRun.run_id, { storageRoot: join(tmp, "store") });
    const faultTrace = trace.find((r) => r.data.phase === "fault");
    assert.ok(faultTrace, "expected a fault trace record");
    assert.equal(faultTrace.data.state, "scan");
    assert.equal(faultTrace.data.reason, "post_validation_failed");
    assert.ok(faultTrace.data.details, "expected fault details");
    assert.equal(
      faultTrace.data.details.post_validations[0].check,
      "len(findings) > 0",
    );
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-commit: passes post_validations when predicate satisfied (findings non-empty)", () => {
  const tmp = setupPostvalFixture();
  try {
    const session = "postval-pass-session";
    const newRun = parseJsonStdout(
      runScript("fsm-next.mjs", [
        "--new-run",
        "--repo", "testrepo",
        "--base-sha", "aaa",
        "--head-sha", "bbb",
        "--session-id", session,
        "--args", "{}",
        ...commonArgs(tmp),
      ]),
    );
    const commit = parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({ findings: [{ id: 1 }] }),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(commit.ok, true);
    assert.equal(commit.advanced_from, "scan");
    assert.equal(commit.state, "terminal");

    // Trace must include the exit record with the predicate result.
    const trace = readTrace(newRun.run_id, { storageRoot: join(tmp, "store") });
    const exitTrace = trace.find(
      (r) => r.data.phase === "exit" && r.data.state === "scan",
    );
    assert.ok(exitTrace, "expected exit trace for the scan state");
    assert.ok(Array.isArray(exitTrace.data.post_validations));
    assert.equal(exitTrace.data.post_validations.length, 1);
    assert.equal(exitTrace.data.post_validations[0].result, true);
    assert.equal(
      exitTrace.data.post_validations[0].check,
      "len(findings) > 0",
    );
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});
