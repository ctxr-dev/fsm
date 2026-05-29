// fsm-journal.test.js — unit coverage for the A7 atomic-tx journal:
// withJournal happy path + crash simulation, journalState inspection,
// discardJournal rollback, replayJournal idempotent finalisation,
// refuse-on-existing-journal guard.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { symlinkSync } from "node:fs";

import {
  appendTraceFile,
  buildRunId,
  discardJournal,
  ensureRunDir,
  journalRoot,
  journalState,
  readManifest,
  replayJournal,
  runDirPath,
  withJournal,
  writeManifest,
} from "../../scripts/lib/fsm-storage.mjs";

function tmpStore() {
  return mkdtempSync(join(tmpdir(), "fsm-journal-"));
}

function makeRun(store) {
  const runId = buildRunId({ repo: "ctxr-dev/fsm", baseSha: "b", headSha: "h" }).runId;
  const runDir = ensureRunDir(runId, { storageRoot: store });
  writeManifest(
    runId,
    { run_id: runId, status: "in_progress", current_state: "a" },
    { storageRoot: store },
  );
  return { runId, runDir, opts: { storageRoot: store } };
}

// ─── withJournal happy path ─────────────────────────────────────────────

test("withJournal: stages writes then atomically finalises and removes the journal", () => {
  const store = tmpStore();
  try {
    const { runId, runDir, opts } = makeRun(store);
    const initialTrace = readdirSync(join(runDir, "fsm-trace"));
    assert.equal(initialTrace.length, 0);

    const { txnId, staged } = withJournal(runDir, (txn) => {
      writeManifest(
        runId,
        { run_id: runId, status: "in_progress", current_state: "b" },
        { ...opts, transaction: txn },
      );
      appendTraceFile(
        runId,
        { phase: "exit", state: "a", data: { outputs: { x: 1 } } },
        { ...opts, transaction: txn },
      );
      appendTraceFile(
        runId,
        { phase: "entry", state: "b", data: { inputs: { x: 1 } } },
        { ...opts, transaction: txn },
      );
    });

    assert.equal(typeof txnId, "string");
    assert.deepEqual(
      staged.sort(),
      ["fsm-trace/0001-exit-a.yaml", "fsm-trace/0002-entry-b.yaml", "manifest.json"].sort(),
    );
    // Journal directory is gone after a successful commit.
    assert.equal(existsSync(journalRoot(runDir)), false);
    // Final files exist and carry the new state.
    const manifest = readManifest(runId, opts);
    assert.equal(manifest.current_state, "b");
    const traceFiles = readdirSync(join(runDir, "fsm-trace")).sort();
    assert.deepEqual(traceFiles, ["0001-exit-a.yaml", "0002-entry-b.yaml"]);
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("withJournal: sequence accounting includes both staged and on-disk traces", () => {
  const store = tmpStore();
  try {
    const { runId, runDir, opts } = makeRun(store);
    // One pre-existing on-disk trace at seq=1.
    appendTraceFile(
      runId,
      { phase: "entry", state: "a", data: {} },
      opts,
    );
    withJournal(runDir, (txn) => {
      const txOpts = { ...opts, transaction: txn };
      const r1 = appendTraceFile(runId, { phase: "exit", state: "a", data: {} }, txOpts);
      const r2 = appendTraceFile(runId, { phase: "entry", state: "b", data: {} }, txOpts);
      assert.equal(r1.sequence, 2);
      assert.equal(r2.sequence, 3);
    });
    const files = readdirSync(join(runDir, "fsm-trace")).sort();
    assert.deepEqual(files, [
      "0001-entry-a.yaml",
      "0002-exit-a.yaml",
      "0003-entry-b.yaml",
    ]);
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

// ─── crash simulation ───────────────────────────────────────────────────

test("withJournal: throw inside fn leaves a pending journal on disk", () => {
  const store = tmpStore();
  try {
    const { runId, runDir, opts } = makeRun(store);
    assert.throws(
      () =>
        withJournal(runDir, (txn) => {
          writeManifest(
            runId,
            { run_id: runId, status: "in_progress", current_state: "b" },
            { ...opts, transaction: txn },
          );
          throw new Error("simulated worker fault");
        }),
      /simulated worker fault/,
    );
    const j = journalState(runDir);
    assert.equal(j.hasJournal, true);
    assert.equal(j.status, "pending");
    // Final manifest still shows the OLD state.
    const manifest = readManifest(runId, opts);
    assert.equal(manifest.current_state, "a");
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("withJournal: refuses to start while an unrecovered journal exists", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    // Plant a journal manually.
    mkdirSync(join(runDir, ".journal", "txn-001"), { recursive: true });
    writeFileSync(
      join(runDir, ".journal", "txn-001", "journal.json"),
      JSON.stringify({ txn_id: "txn-001", status: "pending", staged_files: [] }),
    );
    assert.throws(
      () => withJournal(runDir, () => {}),
      /existing journal/,
    );
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

// ─── journalState ───────────────────────────────────────────────────────

test("journalState: returns hasJournal=false when no journal dir", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    assert.deepEqual(journalState(runDir), { hasJournal: false });
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("journalState: returns hasJournal=false when journal dir exists but empty", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    mkdirSync(journalRoot(runDir), { recursive: true });
    assert.deepEqual(journalState(runDir), { hasJournal: false });
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

// ─── discardJournal ─────────────────────────────────────────────────────

test("discardJournal: removes the txn and cleans the empty journal root", () => {
  const store = tmpStore();
  try {
    const { runId, runDir, opts } = makeRun(store);
    try {
      withJournal(runDir, (txn) => {
        writeManifest(
          runId,
          { run_id: runId, status: "in_progress", current_state: "b" },
          { ...opts, transaction: txn },
        );
        throw new Error("crash");
      });
    } catch {
      // expected
    }
    const before = journalState(runDir);
    assert.equal(before.hasJournal, true);
    const result = discardJournal(runDir, before.txnId);
    assert.equal(result.discarded, true);
    assert.equal(result.txnId, before.txnId);
    assert.equal(existsSync(journalRoot(runDir)), false);
    // Original manifest preserved.
    const manifest = readManifest(runId, opts);
    assert.equal(manifest.current_state, "a");
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("discardJournal: returns not_found for an unknown txnId", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    const result = discardJournal(runDir, "nope-txn");
    assert.deepEqual(result, { discarded: false, reason: "not_found" });
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

// ─── replayJournal ──────────────────────────────────────────────────────

function plantReadyJournal(runDir, runId) {
  // Simulate a crash that landed after "ready_to_finalise" but before any
  // rename. We stage one manifest.json + one trace file into the journal
  // dir, write the ready manifest, and leave the final files in their
  // pre-crash state.
  const txnId = "test-txn-001";
  const txnDir = join(runDir, ".journal", txnId);
  mkdirSync(join(txnDir, "fsm-trace"), { recursive: true });
  const stagedManifest = {
    run_id: runId,
    status: "in_progress",
    current_state: "replayed-state",
  };
  writeFileSync(join(txnDir, "manifest.json"), JSON.stringify(stagedManifest, null, 2));
  writeFileSync(
    join(txnDir, "fsm-trace", "0001-exit-a.yaml"),
    "phase: exit\nstate: a\nsequence: 1\n",
  );
  writeFileSync(
    join(txnDir, "journal.json"),
    JSON.stringify(
      {
        txn_id: txnId,
        status: "ready_to_finalise",
        run_dir: runDir,
        staged_files: [
          { relPath: "manifest.json" },
          { relPath: "fsm-trace/0001-exit-a.yaml" },
        ],
      },
      null,
      2,
    ),
  );
  return txnId;
}

test("replayJournal: finalises a ready_to_finalise journal idempotently", () => {
  const store = tmpStore();
  try {
    const { runId, runDir, opts } = makeRun(store);
    const txnId = plantReadyJournal(runDir, runId);
    const first = replayJournal(runDir, txnId);
    assert.equal(first.replayed, true);
    assert.equal(first.finalised.length, 2);
    const manifest = readManifest(runId, opts);
    assert.equal(manifest.current_state, "replayed-state");
    assert.ok(existsSync(join(runDir, "fsm-trace", "0001-exit-a.yaml")));
    // Journal dir cleaned.
    assert.equal(existsSync(journalRoot(runDir)), false);
    // Second replay finds nothing to do and reports cleanly.
    const second = replayJournal(runDir, txnId);
    assert.equal(second.replayed, false);
    assert.equal(second.reason, "no_manifest");
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

// ─── path-traversal guards (security) ───────────────────────────────────

test("transaction.stage: rejects absolute paths", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    assert.throws(
      () =>
        withJournal(runDir, (txn) => {
          txn.stage("/etc/passwd");
        }),
      /must be relative/,
    );
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("transaction.stage: rejects '..' segments", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    assert.throws(
      () =>
        withJournal(runDir, (txn) => {
          txn.stage("../escape.json");
        }),
      /invalid segment/,
    );
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("transaction.stage: rejects backslashes (Windows traversal)", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    assert.throws(
      () =>
        withJournal(runDir, (txn) => {
          txn.stage("foo\\..\\bar");
        }),
      /must not contain backslashes/,
    );
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("discardJournal: rejects a txnId containing path separators", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    assert.throws(() => discardJournal(runDir, "../../oops"), /single safe filesystem segment/);
    assert.throws(() => discardJournal(runDir, "foo/bar"), /single safe filesystem segment/);
    assert.throws(() => discardJournal(runDir, ".."), /single safe filesystem segment/);
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("replayJournal: rejects a txnId containing path separators", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    assert.throws(() => replayJournal(runDir, "../../escape"), /single safe filesystem segment/);
    assert.throws(() => replayJournal(runDir, "foo\\bar"), /single safe filesystem segment/);
    assert.throws(() => replayJournal(runDir, "C:foo"), /single safe filesystem segment/);
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("replayJournal: rejects relPaths with traversal from a crafted manifest", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    const txnId = "crafted-txn-001";
    const txnDir = join(runDir, ".journal", txnId);
    mkdirSync(txnDir, { recursive: true });
    writeFileSync(
      join(txnDir, "journal.json"),
      JSON.stringify({
        txn_id: txnId,
        status: "ready_to_finalise",
        staged_files: [{ relPath: "../../escape.json" }],
      }),
    );
    assert.throws(() => replayJournal(runDir, txnId), /invalid segment/);
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

// ─── thenable rejection (atomicity) ─────────────────────────────────────

test("withJournal: rejects async fn (thenable return)", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    assert.throws(
      () => withJournal(runDir, async () => {}),
      /must be a synchronous function/,
    );
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("withJournal: rejects fn returning a manual Promise", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    assert.throws(
      () => withJournal(runDir, () => Promise.resolve("oops")),
      /must be a synchronous function/,
    );
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

// ─── appendTraceFile in-transaction return shape ────────────────────────

test("appendTraceFile (transaction): returned path points at the staged file, final_path at its destination", () => {
  const store = tmpStore();
  try {
    const { runId, runDir, opts } = makeRun(store);
    let recorded;
    withJournal(runDir, (txn) => {
      recorded = appendTraceFile(
        runId,
        { phase: "exit", state: "a", data: { outputs: {} } },
        { ...opts, transaction: txn },
      );
      // While the transaction is open, the file lives at staged path.
      assert.equal(recorded.staged, true);
      assert.ok(existsSync(recorded.path), "staged file should exist on disk");
      assert.equal(existsSync(recorded.final_path), false, "final not in place yet");
      assert.notEqual(recorded.path, recorded.final_path);
    });
    // After the journal finalises, the final path exists and the
    // staged path is gone.
    assert.equal(existsSync(recorded.path), false);
    assert.ok(existsSync(recorded.final_path));
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

// ─── symlink-escape guards ──────────────────────────────────────────────

test("withJournal: refuses to rename into a symlinked subdir that escapes runDir", () => {
  const store = tmpStore();
  // The escape target lives OUTSIDE the storage root so a successful
  // rename would land bytes in `escape/`. With the guard, the rename is
  // refused and the journal stays on disk.
  const escapeRoot = mkdtempSync(join(tmpdir(), "fsm-journal-escape-"));
  try {
    const { runId, runDir, opts } = makeRun(store);
    // Replace runDir/workers with a symlink pointing outside the run
    // dir BEFORE the journal finalises.
    rmSync(join(runDir, "workers"), { recursive: true, force: true });
    mkdirSync(join(escapeRoot, "out"), { recursive: true });
    symlinkSync(join(escapeRoot, "out"), join(runDir, "workers"));
    assert.throws(
      () =>
        withJournal(runDir, (txn) => {
          const stagedPath = txn.stage("workers/payload.json");
          writeFileSync(stagedPath, "{}");
          txn.addStaged("workers/payload.json", stagedPath);
        }),
      /resolves to .* which is outside/,
    );
    // The escape target should be empty (rename refused).
    assert.deepEqual(readdirSync(join(escapeRoot, "out")), []);
    // Journal stays on disk for inspection.
    assert.equal(journalState(runDir).hasJournal, true);
  } finally {
    rmSync(store, { recursive: true, force: true });
    rmSync(escapeRoot, { recursive: true, force: true });
  }
});

test("replayJournal: refuses to rename into a symlinked subdir that escapes runDir", () => {
  const store = tmpStore();
  const escapeRoot = mkdtempSync(join(tmpdir(), "fsm-journal-escape-"));
  try {
    const { runId, runDir, opts } = makeRun(store);
    // Plant a ready_to_finalise journal with one staged file in
    // workers/. Then replace runDir/workers with a symlink to outside
    // before calling replay.
    const txnId = "symlink-escape-001";
    const txnDir = join(runDir, ".journal", txnId);
    mkdirSync(join(txnDir, "workers"), { recursive: true });
    writeFileSync(join(txnDir, "workers", "payload.json"), "{}");
    writeFileSync(
      join(txnDir, "journal.json"),
      JSON.stringify({
        txn_id: txnId,
        status: "ready_to_finalise",
        staged_files: [{ relPath: "workers/payload.json" }],
      }),
    );
    rmSync(join(runDir, "workers"), { recursive: true, force: true });
    mkdirSync(join(escapeRoot, "out"), { recursive: true });
    symlinkSync(join(escapeRoot, "out"), join(runDir, "workers"));
    assert.throws(
      () => replayJournal(runDir, txnId),
      /resolves to .* which is outside/,
    );
    assert.deepEqual(readdirSync(join(escapeRoot, "out")), []);
  } finally {
    rmSync(store, { recursive: true, force: true });
    rmSync(escapeRoot, { recursive: true, force: true });
  }
});

test("replayJournal: refuses to finalise a pending journal", () => {
  const store = tmpStore();
  try {
    const { runDir } = makeRun(store);
    const txnId = "pending-txn-001";
    const txnDir = join(runDir, ".journal", txnId);
    mkdirSync(txnDir, { recursive: true });
    writeFileSync(
      join(txnDir, "journal.json"),
      JSON.stringify({ txn_id: txnId, status: "pending", staged_files: [] }),
    );
    const result = replayJournal(runDir, txnId);
    assert.equal(result.replayed, false);
    assert.equal(result.reason, "status_pending");
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});
