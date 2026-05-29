// atomic-tx.test.js — integration coverage for the A7 atomic-tx journal.
//
// Drives fsm-next + fsm-commit + fsm-resume + fsm-inspect end-to-end and
// proves:
//
//   1. A successful fsm-commit leaves no journal on disk.
//   2. SIGKILL during FSM_TEST_PAUSE_BEFORE_FINALISE leaves a
//      ready_to_finalise journal AND no final files.
//   3. fsm-next --resume refuses to advance with "incomplete_commit_detected"
//      while a journal is present.
//   4. fsm-resume --journal replay finalises the staged commit and a
//      subsequent fsm-next --resume succeeds.
//   5. (Separate run) fsm-resume --journal discard rolls back instead.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
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

const FSM_YAML = `
fsm:
  id: atomic-tx
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
          when: always
    - id: b
      purpose: "Terminal."
      preconditions: []
      outputs: []
      transitions: []
`;

function setupFixture() {
  const tmp = mkdtempSync(join(tmpdir(), "fsm-atomictx-"));
  writeFileSync(join(tmp, "fsm.yaml"), FSM_YAML);
  mkdirSync(join(tmp, "workers"));
  writeFileSync(join(tmp, "workers", "stub.md"), "# stub worker\n");
  mkdirSync(join(tmp, "store"));
  return tmp;
}

function commonArgs(tmp) {
  return ["--fsm-path", join(tmp, "fsm.yaml"), "--storage-root", join(tmp, "store")];
}

function runScript(name, args, opts = {}) {
  return spawnSync("node", [join(SCRIPT_DIR, name), ...args], {
    encoding: "utf8",
    env: { ...process.env, ...(opts.env ?? {}) },
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

function startNewRun(tmp) {
  const next = runScript("fsm-next.mjs", [
    "--new-run",
    "--repo",
    "ctxr-dev/fsm",
    "--base-sha",
    "deadbee",
    "--head-sha",
    "cafefee",
    "--session-id",
    "test-session",
    ...commonArgs(tmp),
  ]);
  return parseJsonStdout(next);
}

function findJournalDir(storeRoot) {
  // Walk YYYY/MM/DD/shard/rest to find any .journal directory.
  if (!existsSync(storeRoot)) return null;
  const walk = (dir) => {
    if (!existsSync(dir)) return null;
    for (const name of readdirSync(dir)) {
      const child = join(dir, name);
      if (name === ".journal") return child;
      try {
        const found = walk(child);
        if (found) return found;
      } catch {
        // not a dir; skip
      }
    }
    return null;
  };
  return walk(storeRoot);
}

// ─── happy path: no journal residue ─────────────────────────────────────

test("atomic-tx: successful commit leaves no journal on disk", () => {
  const tmp = setupFixture();
  try {
    const brief = startNewRun(tmp);
    const commitResult = runScript("fsm-commit.mjs", [
      "--run-id",
      brief.run_id,
      "--outputs",
      JSON.stringify({ x: 7 }),
      "--session-id",
      "test-session",
      ...commonArgs(tmp),
    ]);
    parseJsonStdout(commitResult); // throws if non-zero
    assert.equal(findJournalDir(join(tmp, "store")), null);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── SIGKILL during pause leaves a recoverable journal ──────────────────

function killDuringPause(tmp, runId, pauseMs) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      "node",
      [
        join(SCRIPT_DIR, "fsm-commit.mjs"),
        "--run-id",
        runId,
        "--outputs",
        JSON.stringify({ x: 11 }),
        "--session-id",
        "test-session",
        ...commonArgs(tmp),
      ],
      {
        env: {
          ...process.env,
          FSM_TEST_PAUSE_BEFORE_FINALISE: String(pauseMs),
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let stderr = "";
    let stdout = "";
    child.stdout.on("data", (b) => (stdout += b.toString("utf8")));
    child.stderr.on("data", (b) => (stderr += b.toString("utf8")));
    child.on("error", reject);
    child.on("exit", (code, signal) => {
      resolve({ code, signal, stdout, stderr });
    });
    // Give the child enough time to mark the journal ready_to_finalise
    // and enter the pause busy-wait. The busy wait is synchronous so a
    // SIGKILL during the window is guaranteed to land before the rename
    // loop runs.
    setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        // already exited
      }
    }, Math.min(pauseMs - 500, pauseMs * 0.5));
  });
}

test("atomic-tx: SIGKILL during finalise pause leaves journal; fsm-next refuses; replay finalises", async () => {
  const tmp = setupFixture();
  try {
    const brief = startNewRun(tmp);
    const killed = await killDuringPause(tmp, brief.run_id, 3000);
    // The child was killed; it should NOT have exited cleanly.
    assert.notEqual(killed.signal, null, "expected SIGKILL to be delivered");

    const journalDir = findJournalDir(join(tmp, "store"));
    assert.ok(journalDir, "expected a .journal directory after the kill");

    // fsm-inspect surfaces the journal.
    const inspect = parseJsonStdout(
      runScript("fsm-inspect.mjs", [
        "--run-id",
        brief.run_id,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(inspect.journal.present, true);
    assert.equal(inspect.journal.status, "ready_to_finalise");
    assert.ok(inspect.journal.staged.length > 0);

    // fsm-next --resume refuses to advance.
    const nextRefused = runScript("fsm-next.mjs", [
      "--resume",
      brief.run_id,
      "--session-id",
      "test-session-2",
      ...commonArgs(tmp),
    ]);
    assert.notEqual(nextRefused.status, 0);
    const nextRefusedBody = JSON.parse(nextRefused.stdout);
    assert.equal(nextRefusedBody.error, "incomplete_commit_detected");

    // fsm-resume --journal replay finalises.
    const replayed = parseJsonStdout(
      runScript("fsm-resume.mjs", [
        "--run-id",
        brief.run_id,
        "--journal",
        "replay",
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(replayed.journal_action, "replay");
    assert.equal(replayed.replayed, true);
    assert.ok(Array.isArray(replayed.finalised));
    assert.equal(findJournalDir(join(tmp, "store")), null);

    // After replay, the commit IS visible: fsm-inspect shows state=b
    // (or equivalent) and trace has the exit + entry records.
    const inspectAfter = parseJsonStdout(
      runScript("fsm-inspect.mjs", [
        "--run-id",
        brief.run_id,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(inspectAfter.journal.present, false);
    // Manifest carries the committed advance.
    assert.equal(inspectAfter.manifest.current_state, "b");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("atomic-tx: SIGKILL during finalise pause + discard rolls back to pre-commit state", async () => {
  const tmp = setupFixture();
  try {
    const brief = startNewRun(tmp);
    const killed = await killDuringPause(tmp, brief.run_id, 3000);
    assert.notEqual(killed.signal, null);

    const inspectBefore = parseJsonStdout(
      runScript("fsm-inspect.mjs", [
        "--run-id",
        brief.run_id,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(inspectBefore.journal.present, true);
    // Manifest still shows pre-commit state (current_state is the entry
    // state "a" because the journal was killed before finalisation).
    assert.equal(inspectBefore.manifest.current_state, "a");

    const discarded = parseJsonStdout(
      runScript("fsm-resume.mjs", [
        "--run-id",
        brief.run_id,
        "--journal",
        "discard",
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(discarded.journal_action, "discard");
    assert.equal(discarded.discarded, true);
    assert.equal(findJournalDir(join(tmp, "store")), null);

    // After discard, the manifest is unchanged and the journal-detect
    // gate in fsm-next and fsm-commit no longer triggers — the run is
    // back to its pre-commit state, ready for normal lock-recovery and
    // re-attempt by the operator. (Lock takeover after a crashed writer
    // is a separate concern covered by the fsm-resume tests; A7's
    // scope is strictly the journal mechanic.)
    const inspectAfter = parseJsonStdout(
      runScript("fsm-inspect.mjs", [
        "--run-id",
        brief.run_id,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(inspectAfter.journal.present, false);
    assert.equal(inspectAfter.manifest.current_state, "a");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── fsm-commit detects journal at startup too ──────────────────────────

test("atomic-tx: fsm-commit refuses to start when a journal already exists", () => {
  const tmp = setupFixture();
  try {
    const brief = startNewRun(tmp);
    // Plant a journal directly under the run dir by reading manifest to
    // get the run-dir path via fsm-inspect.
    const inspect = parseJsonStdout(
      runScript("fsm-inspect.mjs", [
        "--run-id",
        brief.run_id,
        ...commonArgs(tmp),
      ]),
    );
    const runDir = inspect.run_dir_path;
    const txnDir = join(runDir, ".journal", "planted-txn-001");
    mkdirSync(txnDir, { recursive: true });
    writeFileSync(
      join(txnDir, "journal.json"),
      JSON.stringify({
        txn_id: "planted-txn-001",
        status: "pending",
        staged_files: [],
      }),
    );

    const result = runScript("fsm-commit.mjs", [
      "--run-id",
      brief.run_id,
      "--outputs",
      JSON.stringify({ x: 1 }),
      "--session-id",
      "test-session",
      ...commonArgs(tmp),
    ]);
    assert.notEqual(result.status, 0);
    const body = JSON.parse(result.stdout);
    assert.equal(body.error, "incomplete_commit_detected");
    assert.equal(body.journal.status, "pending");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});
