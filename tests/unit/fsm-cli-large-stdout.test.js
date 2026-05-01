// fsm-cli-large-stdout.test.js — regression for issue #12.
//
// The three FSM CLIs (fsm-next, fsm-commit, fsm-inspect) used to emit JSON
// via `process.stdout.write(...)` followed immediately by `process.exit(...)`.
// On macOS the kernel pipe buffer caps at ~64KB; payloads larger than that
// were queued in Node's userspace stream buffer and dropped at exit before
// they could drain. spawnSync parents observed truncation at exactly 65536
// bytes and JSON.parse(stdout) threw "Unterminated string in JSON".
//
// emit() now uses fs.writeSync(1, ...) which is a blocking POSIX write to
// fd 1 — no userspace queue, no truncation under process.exit. This test
// drives a payload >> 64KB through fsm-commit and asserts the parent
// receives the full JSON byte-complete.

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
    const newRun = JSON.parse(
      runScript("fsm-next.mjs", [
        "--new-run", "--repo", "bigtest", "--base-sha", "aaa", "--head-sha", "bbb",
        "--session-id", session, "--args", "{}",
        ...commonArgs(tmp),
      ]).stdout,
    );

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
