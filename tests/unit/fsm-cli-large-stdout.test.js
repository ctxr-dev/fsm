// fsm-cli-large-stdout.test.js — regressions for issue #12.
//
// All four FSM CLIs (fsm-next, fsm-commit, fsm-inspect, fsm-validate-static)
// used to emit JSON via `process.stdout.write(...)` followed immediately by
// `process.exit(...)`. On macOS the kernel pipe buffer caps at ~64KB;
// payloads larger than that were queued in Node's userspace stream buffer
// and dropped at exit before they could drain. spawnSync parents observed
// truncation at exactly 65536 bytes and JSON.parse(stdout) threw
// "Unterminated string in JSON".
//
// emit() now delegates to scripts/lib/emit.mjs::emitJson which loops
// fs.writeSync(1, ...) until the full buffer is delivered. Three tests
// drive byte-complete-stdout assertions through the three end-user CLIs
// (fsm-commit, fsm-next, fsm-validate-static); fsm-inspect uses the same
// shared helper and therefore inherits the fix.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "scripts",
);

// FSM with a deeply-nested response_schema that produces a multi-hundred-KB
// brief on the next state. The size driver is `inputs[*]` carrying a long
// string that buildBrief threads into the next-state brief.
function makeBigPayloadFsm() {
  return `
fsm:
  id: bigpayload
  version: 1
  entry: a
  states:
    - id: a
      purpose: "Entry; accepts a large blob output."
      preconditions: []
      worker:
        role: stub
        prompt_template: workers/stub.md
        inputs: ["args"]
        response_schema:
          type: object
          required: [blob]
          properties:
            blob: { type: string, minLength: 1 }
      outputs: ["blob"]
      transitions:
        - to: b
          when: always
    - id: b
      purpose: "Mid; consumes the blob."
      preconditions: ["blob exists"]
      worker:
        role: stub
        prompt_template: workers/stub.md
        inputs: ["blob"]
        response_schema:
          type: object
          required: [done]
          properties:
            done: { type: boolean }
      outputs: ["done"]
      transitions: []
`;
}

function setupFixture(yamlText) {
  const tmp = mkdtempSync(join(tmpdir(), "fsm-bigpayload-"));
  writeFileSync(join(tmp, "fsm.yaml"), yamlText);
  mkdirSync(join(tmp, "workers"));
  writeFileSync(join(tmp, "workers", "stub.md"), "# stub worker\n");
  mkdirSync(join(tmp, "store"));
  return tmp;
}

function runScript(name, args) {
  return spawnSync("node", [join(SCRIPT_DIR, name), ...args], { encoding: "utf8" });
}

function commonArgs(tmp) {
  return ["--fsm-path", join(tmp, "fsm.yaml"), "--storage-root", join(tmp, "store")];
}

test("fsm-commit: large brief (>64KB) is byte-complete on stdout (issue #12)", () => {
  const tmp = setupFixture(makeBigPayloadFsm());
  try {
    const session = "test-bigpayload";
    const newRunResult = runScript("fsm-next.mjs", [
      "--new-run", "--repo", "bigtest", "--base-sha", "aaa", "--head-sha", "bbb",
      "--session-id", session, "--args", "{}",
      ...commonArgs(tmp),
    ]);
    assert.equal(
      newRunResult.status, 0,
      `fsm-next --new-run failed; stderr: ${newRunResult.stderr}`,
    );
    const newRun = JSON.parse(newRunResult.stdout);

    // Commit the entry state with a multi-hundred-KB blob. fsm-commit must
    // emit the next state's brief (which threads the blob into env.blob)
    // without truncation. 200KB chosen so the brief reliably exceeds the
    // 64KB pipe buffer cap that the previous emit() shape clipped at.
    const blob = "x".repeat(200_000);
    const commitResult = runScript("fsm-commit.mjs", [
      "--run-id", newRun.run_id,
      "--outputs", JSON.stringify({ blob }),
      "--session-id", session,
      ...commonArgs(tmp),
    ]);

    assert.equal(commitResult.status, 0, `fsm-commit exit status; stderr: ${commitResult.stderr}`);
    // The full stdout must be present and parseable. With the bug, this
    // would truncate near 65536 bytes and JSON.parse would throw
    // "Unterminated string in JSON".
    assert.ok(
      commitResult.stdout.length > 200_000,
      `expected stdout > 200KB, got ${commitResult.stdout.length}`,
    );
    const brief = JSON.parse(commitResult.stdout); // would throw under the bug
    assert.equal(brief.ok, true);
    assert.equal(brief.advanced_from, "a");
    assert.equal(brief.state, "b");
    // The blob round-tripped through fsm-commit's brief output for state b.
    assert.equal(brief.inputs.blob.length, blob.length);
    assert.equal(brief.inputs.blob, blob);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-next --new-run: large args echoed in brief (>64KB) is byte-complete (issue #12)", () => {
  // Sibling of the fsm-commit test above. fsm-next --new-run emits the
  // entry-state brief which carries `inputs: { args: <args> }`. Pass a
  // 200KB args bag so the brief reliably exceeds the 64KB pipe-buffer
  // cap. Asserts the parent receives the full JSON byte-complete.
  const tmp = setupFixture(makeBigPayloadFsm());
  try {
    const session = "test-bignewrun";
    const blob = "x".repeat(200_000);
    const argsJson = JSON.stringify({ huge: blob });
    // 200KB JSON arg passed via --args-file (some shells choke on long
    // --args strings on the command line).
    writeFileSync(join(tmp, "args.json"), argsJson);

    const newRunResult = runScript("fsm-next.mjs", [
      "--new-run", "--repo", "bigtest", "--base-sha", "aaa", "--head-sha", "bbb",
      "--session-id", session, "--args-file", join(tmp, "args.json"),
      ...commonArgs(tmp),
    ]);
    assert.equal(
      newRunResult.status, 0,
      `fsm-next --new-run failed; stderr: ${newRunResult.stderr}`,
    );
    assert.ok(
      newRunResult.stdout.length > 200_000,
      `expected new-run stdout > 200KB, got ${newRunResult.stdout.length}`,
    );
    const brief = JSON.parse(newRunResult.stdout); // would throw under the bug
    assert.equal(brief.ok, true);
    assert.equal(brief.state, "a");
    assert.equal(brief.inputs.args.huge.length, blob.length);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-validate-static: large summary is byte-complete on stdout (issue #12)", () => {
  // fsm-validate-static reports per-file results. To exercise the
  // large-stdout path, write enough invalid FSM files that the cumulative
  // JSON summary exceeds the 64KB pipe-buffer cap.
  const tmp = mkdtempSync(join(tmpdir(), "fsm-static-large-"));
  try {
    // Each invalid FSM contributes ~150 bytes to the JSON report. 600
    // files cross the 64KB cap with margin (~90KB).
    const filenames = [];
    for (let i = 0; i < 600; i++) {
      const name = `bad-${String(i).padStart(4, "0")}.yaml`;
      writeFileSync(
        join(tmp, name),
        `fsm:\n  id: x\n  version: 1\n  entry: missing_state_${i}\n  states:\n    - id: a\n      purpose: "."\n      preconditions: []\n      outputs: []\n      transitions: []\n`,
      );
      filenames.push(name);
    }

    const result = spawnSync(
      "node",
      [join(SCRIPT_DIR, "fsm-validate-static.mjs"), ...filenames],
      { encoding: "utf8", cwd: tmp },
    );

    // Exit 1 expected (every file is invalid). Stdout must still be
    // a complete JSON document.
    assert.equal(result.status, 1, `expected exit 1; stderr: ${result.stderr}`);
    assert.ok(
      result.stdout.length > 64 * 1024,
      `expected stdout > 64KB to exercise the truncation path; got ${result.stdout.length}`,
    );
    const summary = JSON.parse(result.stdout); // would throw under the bug
    assert.equal(summary.ok, false);
    assert.equal(summary.files.length, 600);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});
