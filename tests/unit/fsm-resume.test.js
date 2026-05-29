// fsm-resume.test.js: coverage for the resume/replay CLI.
//
// The fixture FSM has four states:
//   a (worker)  -> b (worker)  -> c (inline) -> d (terminal)
//
// 'a' produces x; 'b' produces y; 'c' is inline (no worker, no outputs).
// This lets us cover:
//   - resume from a worker state mid-run (b)
//   - resume from an inline state with no worker output (c)
//   - resume refusal on a state name not in the FSM definition
//   - resume refusal on a state that exists in the FSM but was never
//     entered in this run
//
// Each test sets up an isolated workdir with the FSM YAML, a worker
// prompt stub, and a fresh storage root, then drives the CLIs via
// spawnSync to mirror the orchestrator's contract.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
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

import {
  pruneTraceAfter,
  readManifest,
  readTrace,
  writeManifest,
} from "../../scripts/lib/fsm-storage.mjs";

// Resume refuses to take a non-stale lock from an in_progress run (so
// it cannot race a live writer that has not yet checked the lock).
// The tests do not actually run a concurrent writer; they just leave
// behind the in_progress manifest + lock from the drive-to-state
// preamble. Flip the status to "paused" so the lock takeover is
// allowed under the same code path a real operator would use after
// pausing the run.
function markPausedForResumeTest(runId, storageRoot) {
  const m = readManifest(runId, { storageRoot });
  writeManifest(
    runId,
    {
      ...m,
      status: "paused",
      paused_at: new Date().toISOString(),
      pause_reason: "test-fixture",
    },
    { storageRoot },
  );
}

const SCRIPT_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "scripts",
);

const RESUME_FSM = `
fsm:
  id: resume-fixture
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
      purpose: "Mid; produces y."
      preconditions: []
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
      purpose: "Inline checkpoint; no worker."
      preconditions: []
      outputs: []
      transitions:
        - to: d
          when: always
    - id: d
      purpose: "Terminal."
      preconditions: []
      outputs: []
      transitions: []
`;

function setupFixture() {
  const tmp = mkdtempSync(join(tmpdir(), "fsm-resume-"));
  writeFileSync(join(tmp, "fsm.yaml"), RESUME_FSM);
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

// Drives the run from a→b→c so the run state contains entry traces for
// all three of (a, b, c). Returns the run-id used.
function driveToInline(tmp, session) {
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
      "--outputs", JSON.stringify({ y: "done" }),
      "--session-id", session,
      ...commonArgs(tmp),
    ]),
  );
  return newRun.run_id;
}

// ─── pruneTraceAfter unit ───────────────────────────────────────────────

test("pruneTraceAfter: removes only files with sequence > given value", () => {
  const tmp = setupFixture();
  try {
    const session = "prune-1";
    const runId = driveToInline(tmp, session);
    markPausedForResumeTest(runId, join(tmp, "store"));
    const before = readTrace(runId, { storageRoot: join(tmp, "store") });
    // Sanity: we expect ≥ 5 trace files (entry-a, exit-a, entry-b, exit-b, entry-c).
    assert.ok(before.length >= 5, `expected ≥5 trace files, got ${before.length}`);
    const result = pruneTraceAfter(runId, 2, { storageRoot: join(tmp, "store") });
    const after = readTrace(runId, { storageRoot: join(tmp, "store") });
    assert.equal(after.length, 2);
    assert.equal(result.removed, before.length - 2);
    for (const r of after) {
      assert.ok(r.data.sequence <= 2);
    }
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("pruneTraceAfter: rejects non-integer or negative sequence", () => {
  const tmp = setupFixture();
  try {
    assert.throws(
      () => pruneTraceAfter("20260101-000000-1234567", -1, { storageRoot: join(tmp, "store") }),
      /non-negative integer/,
    );
    assert.throws(
      () => pruneTraceAfter("20260101-000000-1234567", 1.5, { storageRoot: join(tmp, "store") }),
      /non-negative integer/,
    );
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── resume_history defaults ───────────────────────────────────────────

test("initialiseManifest writes resume_history: [] by default", () => {
  const tmp = setupFixture();
  try {
    const session = "init-1";
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
    const manifest = readManifest(newRun.run_id, { storageRoot: join(tmp, "store") });
    assert.ok(Array.isArray(manifest.resume_history));
    assert.equal(manifest.resume_history.length, 0);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── resume from a worker state ────────────────────────────────────────

test("fsm-resume: resumes from a worker state (b); prunes later traces; annotates manifest", () => {
  const tmp = setupFixture();
  try {
    const session = "resume-worker";
    const runId = driveToInline(tmp, session);
    markPausedForResumeTest(runId, join(tmp, "store"));
    const beforeTrace = readTrace(runId, { storageRoot: join(tmp, "store") });
    // Locate b's entry sequence so we can assert the prune cut-off.
    const entryB = beforeTrace.find(
      (r) => r.data?.phase === "entry" && r.data?.state === "b",
    );
    assert.ok(entryB, "expected entry trace for state b");
    const entrySeq = entryB.data.sequence;

    const resume = parseJsonStdout(
      runScript("fsm-resume.mjs", [
        "--run-id", runId,
        "--from-state", "b",
        "--session-id", "resume-session-1",
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(resume.ok, true);
    assert.equal(resume.resumed, true);
    assert.equal(resume.resumed_from_state, "b");
    assert.equal(resume.state, "b");
    assert.equal(resume.has_worker, true);
    assert.equal(resume.worker.role, "stub");
    // The brief should expose b's input (x), recovered from the surviving exit trace of a.
    assert.deepEqual(resume.inputs, { x: 5 });
    assert.ok(resume.pruned_traces_count > 0, "expected some traces pruned past b's entry");

    const afterTrace = readTrace(runId, { storageRoot: join(tmp, "store") });
    for (const r of afterTrace) {
      assert.ok(
        r.data.sequence <= entrySeq,
        `expected no traces past sequence ${entrySeq}, found ${r.fileName}`,
      );
    }

    const manifest = readManifest(runId, { storageRoot: join(tmp, "store") });
    assert.equal(manifest.status, "in_progress");
    assert.equal(manifest.current_state, "b");
    assert.equal(manifest.ended_at, null);
    assert.equal(manifest.resume_history.length, 1);
    const annotation = manifest.resume_history[0];
    assert.equal(annotation.from_state, "b");
    assert.equal(annotation.session_id, "resume-session-1");
    assert.equal(annotation.pruned_traces_count, resume.pruned_traces_count);
    assert.match(annotation.timestamp, /^\d{4}-\d{2}-\d{2}T/);

    // A fresh lock should be held by the resuming session.
    const lockFiles = readdirSync(join(
      join(tmp, "store"),
      manifest.run_id.slice(0, 4),
      manifest.run_id.slice(4, 6),
      manifest.run_id.slice(6, 8),
      manifest.run_id.slice(16, 18),
      manifest.run_id.slice(18),
    )).filter((n) => n === "lock.json");
    assert.equal(lockFiles.length, 1);

    // A sidecar RESUMED-* annotation file should exist in fsm-trace/.
    const traceDir = join(
      join(tmp, "store"),
      manifest.run_id.slice(0, 4),
      manifest.run_id.slice(4, 6),
      manifest.run_id.slice(6, 8),
      manifest.run_id.slice(16, 18),
      manifest.run_id.slice(18),
      "fsm-trace",
    );
    const resumedSidecar = readdirSync(traceDir).find((n) => n.startsWith("RESUMED-from-b-at-"));
    assert.ok(resumedSidecar, "expected a RESUMED-from-b-at-* sidecar in fsm-trace/");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── resume from an inline state ───────────────────────────────────────

test("fsm-resume: resumes from an inline state (c) with no worker; brief reflects no worker", () => {
  const tmp = setupFixture();
  try {
    const session = "resume-inline";
    const runId = driveToInline(tmp, session);
    markPausedForResumeTest(runId, join(tmp, "store"));
    const resume = parseJsonStdout(
      runScript("fsm-resume.mjs", [
        "--run-id", runId,
        "--from-state", "c",
        "--session-id", "resume-session-2",
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(resume.ok, true);
    assert.equal(resume.resumed_from_state, "c");
    assert.equal(resume.state, "c");
    assert.equal(resume.has_worker, false);
    assert.equal(resume.worker, undefined);
    assert.deepEqual(resume.outputs_expected, []);
    assert.deepEqual(resume.inputs, {});

    const manifest = readManifest(runId, { storageRoot: join(tmp, "store") });
    assert.equal(manifest.current_state, "c");
    assert.equal(manifest.resume_history.length, 1);
    assert.equal(manifest.resume_history[0].from_state, "c");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── refusal: unknown state ────────────────────────────────────────────

test("fsm-resume: refuses on a state name that is not in the FSM at all (unknown_state)", () => {
  const tmp = setupFixture();
  try {
    const session = "resume-unknown";
    const runId = driveToInline(tmp, session);
    markPausedForResumeTest(runId, join(tmp, "store"));
    const result = runScript("fsm-resume.mjs", [
      "--run-id", runId,
      "--from-state", "does-not-exist",
      "--session-id", "resume-session-3",
      ...commonArgs(tmp),
    ]);
    assert.notEqual(result.status, 0);
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.error, "unknown_state");
    assert.equal(payload.from_state, "does-not-exist");

    // Manifest must be untouched: no resume_history entry, no current_state change.
    const manifest = readManifest(runId, { storageRoot: join(tmp, "store") });
    assert.equal(manifest.resume_history.length, 0);
    assert.equal(manifest.current_state, "c");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── refusal: state exists in FSM but never entered in this run ────────

test("fsm-resume: refuses on a state that exists in the FSM but was never entered (state_not_entered_in_run)", () => {
  const tmp = setupFixture();
  try {
    const session = "resume-not-entered";
    // Only drive to b's entry, NOT past it, so 'c' has never been entered.
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
    parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", newRun.run_id,
        "--outputs", JSON.stringify({ x: 5 }),
        "--session-id", session,
        ...commonArgs(tmp),
      ]),
    );
    // The run is now at b (entered). c has NOT been entered.
    const result = runScript("fsm-resume.mjs", [
      "--run-id", newRun.run_id,
      "--from-state", "c",
      "--session-id", "resume-session-4",
      ...commonArgs(tmp),
    ]);
    assert.notEqual(result.status, 0);
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.error, "state_not_entered_in_run");
    assert.equal(payload.from_state, "c");
    assert.ok(Array.isArray(payload.entered_states));
    assert.ok(payload.entered_states.includes("a"));
    assert.ok(payload.entered_states.includes("b"));
    assert.ok(!payload.entered_states.includes("c"));

    // Manifest unchanged.
    const manifest = readManifest(newRun.run_id, { storageRoot: join(tmp, "store") });
    assert.equal(manifest.resume_history.length, 0);
    assert.equal(manifest.current_state, "b");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── refusal: run not found ────────────────────────────────────────────

test("fsm-resume: refuses on an unknown run-id (run_not_found)", () => {
  const tmp = setupFixture();
  try {
    const result = runScript("fsm-resume.mjs", [
      "--run-id", "20260101-000000-fffffff",
      "--from-state", "a",
      "--session-id", "x",
      ...commonArgs(tmp),
    ]);
    assert.notEqual(result.status, 0);
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.error, "run_not_found");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── resume + commit cycle works after resume ──────────────────────────

test("fsm-resume: after resume from b, a fresh commit advances normally (b→c)", () => {
  const tmp = setupFixture();
  try {
    const session = "resume-then-commit";
    const runId = driveToInline(tmp, session);
    markPausedForResumeTest(runId, join(tmp, "store"));
    const resumeSession = "resume-session-5";
    parseJsonStdout(
      runScript("fsm-resume.mjs", [
        "--run-id", runId,
        "--from-state", "b",
        "--session-id", resumeSession,
        ...commonArgs(tmp),
      ]),
    );
    // Fresh commit under the new session.
    const commit = parseJsonStdout(
      runScript("fsm-commit.mjs", [
        "--run-id", runId,
        "--outputs", JSON.stringify({ y: "replayed" }),
        "--session-id", resumeSession,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(commit.ok, true);
    assert.equal(commit.advanced_from, "b");
    assert.equal(commit.state, "c");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── lock takeover ─────────────────────────────────────────────────────

test("fsm-resume: takes over a prior lock held by a different session", () => {
  const tmp = setupFixture();
  try {
    const originalSession = "original";
    const runId = driveToInline(tmp, originalSession);
    markPausedForResumeTest(runId, join(tmp, "store"));
    // The original session still holds the lock. Resume with a fresh session.
    const newSession = "resume-takeover";
    const resume = parseJsonStdout(
      runScript("fsm-resume.mjs", [
        "--run-id", runId,
        "--from-state", "b",
        "--session-id", newSession,
        ...commonArgs(tmp),
      ]),
    );
    assert.equal(resume.ok, true);

    // Original session can no longer commit: lock now belongs to new session.
    const blocked = runScript("fsm-commit.mjs", [
      "--run-id", runId,
      "--outputs", JSON.stringify({ y: "should-be-blocked" }),
      "--session-id", originalSession,
      ...commonArgs(tmp),
    ]);
    assert.notEqual(blocked.status, 0);
    const payload = JSON.parse(blocked.stdout);
    assert.equal(payload.error, "lock_not_held");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-resume: refuses takeover of a non-stale lock on an in_progress run", () => {
  const tmp = setupFixture();
  try {
    const originalSession = "live-writer";
    const runId = driveToInline(tmp, originalSession);
    // Intentionally DO NOT call markPausedForResumeTest here: the run
    // is still status=in_progress with the original session's lock,
    // which is exactly the live-writer scenario the new takeover
    // policy must refuse. A regression here would corrupt the run.

    const result = runScript("fsm-resume.mjs", [
      "--run-id", runId,
      "--from-state", "b",
      "--session-id", "resume-attempt",
      ...commonArgs(tmp),
    ]);
    assert.notEqual(result.status, 0);
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.error, "run_locked");
    assert.equal(payload.run_status, "in_progress");
    assert.ok(payload.lock, "expected the lock payload to be surfaced");
    assert.match(
      payload.hint || "",
      /pause the run or wait for the writer to release/i,
    );

    // Manifest must NOT have been mutated: status, current_state and
    // resume_history remain whatever drive-to-state left behind.
    const m = readManifest(runId, { storageRoot: join(tmp, "store") });
    assert.equal(m.status, "in_progress");
    assert.equal((m.resume_history ?? []).length, 0);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── arg validation ────────────────────────────────────────────────────

test("fsm-resume: rejects missing --run-id", () => {
  const tmp = setupFixture();
  try {
    const result = runScript("fsm-resume.mjs", [
      "--from-state", "a",
      ...commonArgs(tmp),
    ]);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /--run-id is required/);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-resume: rejects missing --from-state and missing --journal", () => {
  const tmp = setupFixture();
  try {
    const result = runScript("fsm-resume.mjs", [
      "--run-id", "20260101-000000-1234567",
      ...commonArgs(tmp),
    ]);
    assert.notEqual(result.status, 0);
    // Post-A7, callers must pass either --from-state (rewind to a prior
    // entered state) or --journal {discard|replay} (recover an incomplete
    // commit). Either is acceptable; missing both is the error.
    assert.match(
      result.stderr,
      /either --from-state or --journal <discard\|replay> is required/,
    );
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("fsm-resume: rejects unknown argument", () => {
  const result = runScript("fsm-resume.mjs", ["--bogus"]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unknown argument/);
});

test("fsm-resume: rejects --journal without a value", () => {
  // Edge case: `fsm-resume --journal` with no following action used to
  // silently parse `journalAction = undefined` and then fall through to
  // the generic "either --from-state or --journal ..." error, which
  // misleads the user (they did pass --journal). The parser now
  // requires a value for every flag.
  const result = runScript("fsm-resume.mjs", [
    "--run-id", "20260101-000000-1234567",
    "--journal",
  ]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /--journal requires a value/);
});

test("fsm-resume: rejects --journal followed by another flag", () => {
  // Same shape as the previous: if the value happens to look like
  // another flag, treat it as a missing value rather than silently
  // consuming it as the action.
  const result = runScript("fsm-resume.mjs", [
    "--run-id", "20260101-000000-1234567",
    "--journal", "--from-state", "a",
  ]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /--journal requires a value/);
});

// ─── final sanity: the runner file is present in scripts/.
test("fsm-resume.mjs file is present in scripts/", () => {
  // Sanity: file exists at the canonical path.
  assert.ok(existsSync(join(SCRIPT_DIR, "fsm-resume.mjs")));
});
