// fsm-storage.mjs — filesystem I/O helpers for the FSM substrate.
//
// All functions take a `storageRoot` argument: the absolute or relative
// path to the directory under which run-id-keyed subdirectories are
// created. The package itself is consumer-agnostic; consumers choose the
// storage root to suit their project (e.g. `.skill-code-review/`,
// `.fsm-runs/`, an absolute path under XDG_DATA_HOME, etc.).
//
// Layout produced under storageRoot:
//   <yyyy>/<mm>/<dd>/<ab>/<rest>/
//     manifest.json           — run summary, atomic writes
//     lock.json               — per-run lock with TTL
//     fsm-trace/NNN-...yaml   — sequential transition records
//     workers/                — worker prompt + response artifacts
//
// Atomic writes: write tmp + fsync + rename.
// Locks: POSIX O_EXCL with embedded expires_at TTL; stale-lock recovery.
// Cross-run queries walk only recent date folders, bounded by --days-back.

import { createHash, randomBytes } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmdirSync,
  rmSync,
  writeFileSync,
  openSync,
  closeSync,
  fsyncSync,
  statSync,
} from "node:fs";
import { dirname, join, resolve, sep as pathSep } from "node:path";
import { stringify as stringifyYaml, parse as parseYaml } from "yaml";

const LOCK_TTL_MS_DEFAULT = 60 * 60 * 1000; // 1 hour
const JOURNAL_DIR_NAME = ".journal";

// ─── run-id ─────────────────────────────────────────────────────────────

// runId format: <YYYYMMDD>-<HHMMSS>-<hash7>
// hash7 is the first 7 hex chars of sha256(repo + baseSha + headSha + timestamp + randomNonce).
// shard is the first 2 chars of hash7.
export function buildRunId({
  repo,
  baseSha = "",
  headSha = "",
  timestamp = new Date(),
}) {
  if (!repo) {
    throw new Error("buildRunId: repo is required");
  }
  const ts = timestamp instanceof Date ? timestamp : new Date(timestamp);
  if (Number.isNaN(ts.getTime())) {
    throw new Error("buildRunId: timestamp must be a valid Date");
  }
  const yyyy = ts.getUTCFullYear();
  const mm = String(ts.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(ts.getUTCDate()).padStart(2, "0");
  const hh = String(ts.getUTCHours()).padStart(2, "0");
  const mi = String(ts.getUTCMinutes()).padStart(2, "0");
  const ss = String(ts.getUTCSeconds()).padStart(2, "0");
  const seed = `${repo}|${baseSha}|${headSha}|${ts.toISOString()}|${randomBytes(4).toString("hex")}`;
  const hash = createHash("sha256").update(seed).digest("hex");
  const hash7 = hash.slice(0, 7);
  const stamp = `${yyyy}${mm}${dd}-${hh}${mi}${ss}`;
  return {
    runId: `${stamp}-${hash7}`,
    shard: hash7.slice(0, 2),
    yyyy: String(yyyy),
    mm,
    dd,
    timestamp: ts,
  };
}

// parseRunId extracts the date + shard + rest portions of a run-id.
// Throws if the format is wrong.
export function parseRunId(runId) {
  if (typeof runId !== "string") {
    throw new Error(`parseRunId: runId must be a string, got ${typeof runId}`);
  }
  const match = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-([0-9a-f]{7})$/.exec(
    runId,
  );
  if (!match) {
    throw new Error(
      `parseRunId: malformed run-id "${runId}" (expected YYYYMMDD-HHMMSS-<7 hex>)`,
    );
  }
  const [, yyyy, mm, dd, hh, mi, ss, hash7] = match;
  return {
    runId,
    yyyy,
    mm,
    dd,
    hh,
    mi,
    ss,
    hash7,
    shard: hash7.slice(0, 2),
    rest: hash7.slice(2),
  };
}

// runDirPath assembles the absolute path to a run's directory.
//   <storageRoot>/<yyyy>/<mm>/<dd>/<ab>/<rest>/
// where storageRoot is required and resolved against process.cwd() if relative.
export function runDirPath(runId, { storageRoot } = {}) {
  if (!storageRoot) {
    throw new Error("runDirPath: storageRoot is required");
  }
  const parsed = parseRunId(runId);
  return join(
    resolve(storageRoot),
    parsed.yyyy,
    parsed.mm,
    parsed.dd,
    parsed.shard,
    parsed.rest,
  );
}

// ensureRunDir creates the run-specific directory tree. Idempotent.
export function ensureRunDir(runId, opts = {}) {
  const dir = runDirPath(runId, opts);
  mkdirSync(dir, { recursive: true });
  mkdirSync(join(dir, "fsm-trace"), { recursive: true });
  mkdirSync(join(dir, "workers"), { recursive: true });
  return dir;
}

// ─── atomic writes ──────────────────────────────────────────────────────

// atomicWriteFile: write to <path>.tmp, fsync, rename to <path>.
// Caller is responsible for ensuring dirname(path) exists.
export function atomicWriteFile(path, contents) {
  const tmp = `${path}.tmp.${process.pid}.${Date.now()}.${randomBytes(2).toString("hex")}`;
  const fd = openSync(tmp, "w", 0o644);
  try {
    writeFileSync(fd, contents);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(tmp, path);
}

export function atomicWriteJson(path, data, { indent = 2 } = {}) {
  const json = JSON.stringify(data, null, indent) + "\n";
  atomicWriteFile(path, json);
}

export function atomicWriteYaml(path, data) {
  const yaml = stringifyYaml(data, { lineWidth: 0 });
  atomicWriteFile(path, yaml);
}

// ─── manifest.json ──────────────────────────────────────────────────────

export function readManifest(runId, opts = {}) {
  // When called inside a transaction, prefer the staged manifest if one
  // has already been written this turn so successive updateManifest
  // calls compose (read-merge-write) instead of clobbering each other.
  const txn = opts.transaction;
  if (txn && typeof txn.readStagedManifest === "function") {
    const staged = txn.readStagedManifest();
    if (staged) return staged;
  }
  const path = join(runDirPath(runId, opts), "manifest.json");
  if (!existsSync(path)) return null;
  return JSON.parse(readFileSync(path, "utf8"));
}

export function writeManifest(runId, data, opts = {}) {
  const txn = opts.transaction;
  if (txn) {
    const relPath = "manifest.json";
    const stagedPath = txn.stage(relPath);
    atomicWriteJson(stagedPath, data);
    if (!txn.staged.some((s) => s.relPath === relPath)) {
      txn.addStaged(relPath, stagedPath);
    }
    return;
  }
  const dir = ensureRunDir(runId, opts);
  atomicWriteJson(join(dir, "manifest.json"), data);
}

// ─── locks ──────────────────────────────────────────────────────────────

// Lock contract:
//   lock.json contains { run_id, session_id, pid, acquired_at, expires_at }.
//   acquireLock uses O_EXCL to create the file atomically. If creation
//   fails because the file exists, we read the existing lock — if its
//   expires_at is in the past, the holder crashed; we delete it and retry.
export function acquireLock(runId, { sessionId, ttlMs = LOCK_TTL_MS_DEFAULT, storageRoot, now = new Date() } = {}) {
  if (!sessionId) {
    throw new Error("acquireLock: sessionId is required");
  }
  const dir = ensureRunDir(runId, { storageRoot });
  const lockPath = join(dir, "lock.json");
  const acquiredAt = now instanceof Date ? now : new Date(now);
  const expiresAt = new Date(acquiredAt.getTime() + ttlMs);
  const payload = {
    run_id: runId,
    session_id: sessionId,
    pid: process.pid,
    acquired_at: acquiredAt.toISOString(),
    expires_at: expiresAt.toISOString(),
  };
  try {
    const fd = openSync(lockPath, "wx", 0o644);
    try {
      writeFileSync(fd, JSON.stringify(payload, null, 2) + "\n");
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    return { acquired: true, lock: payload };
  } catch (err) {
    if (err.code !== "EEXIST") throw err;
  }
  // Lock exists. Inspect it.
  const existing = JSON.parse(readFileSync(lockPath, "utf8"));
  const exp = new Date(existing.expires_at);
  if (Number.isNaN(exp.getTime()) || exp.getTime() < acquiredAt.getTime()) {
    // Stale: remove and retry once.
    rmSync(lockPath, { force: true });
    try {
      const fd = openSync(lockPath, "wx", 0o644);
      try {
        writeFileSync(fd, JSON.stringify(payload, null, 2) + "\n");
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      return { acquired: true, lock: payload, stale_recovered: true, prior_lock: existing };
    } catch (err2) {
      if (err2.code !== "EEXIST") throw err2;
      const reread = JSON.parse(readFileSync(lockPath, "utf8"));
      return { acquired: false, lock: reread };
    }
  }
  return { acquired: false, lock: existing };
}

// releaseLock removes the lock.json. Verifies the lock belongs to the
// caller's session before unlinking — refuses to release another
// session's lock.
export function releaseLock(runId, { sessionId, storageRoot } = {}) {
  if (!sessionId) {
    throw new Error("releaseLock: sessionId is required");
  }
  const lockPath = join(runDirPath(runId, { storageRoot }), "lock.json");
  if (!existsSync(lockPath)) return { released: false, reason: "no_lock" };
  const existing = JSON.parse(readFileSync(lockPath, "utf8"));
  if (existing.session_id !== sessionId) {
    return { released: false, reason: "not_owner", lock: existing };
  }
  rmSync(lockPath, { force: true });
  return { released: true };
}

export function readLock(runId, opts = {}) {
  const lockPath = join(runDirPath(runId, opts), "lock.json");
  if (!existsSync(lockPath)) return null;
  return JSON.parse(readFileSync(lockPath, "utf8"));
}

// ─── trace files ────────────────────────────────────────────────────────

// nextTraceSequence returns the next sequence integer (1-based) for a run's
// fsm-trace/ directory. Filenames are NNNN-{phase}-{state}.yaml where NNNN
// is zero-padded to 4 digits.
export function nextTraceSequence(runId, opts = {}) {
  const traceDir = join(runDirPath(runId, opts), "fsm-trace");
  if (!existsSync(traceDir)) return 1;
  const entries = readdirSync(traceDir).filter((n) => /^\d+-/.test(n));
  if (entries.length === 0) return 1;
  const seqs = entries.map((n) => Number.parseInt(n.split("-", 1)[0], 10));
  return Math.max(...seqs) + 1;
}

export function appendTraceFile(runId, { phase, state, data }, opts = {}) {
  if (!["entry", "exit", "fault", "iter"].includes(phase)) {
    throw new Error(`appendTraceFile: phase must be entry|exit|fault|iter, got "${phase}"`);
  }
  if (!state || typeof state !== "string") {
    throw new Error("appendTraceFile: state must be a non-empty string");
  }
  const txn = opts.transaction;
  const payload = (seq) => ({
    phase,
    state,
    sequence: seq,
    timestamp: new Date().toISOString(),
    ...data,
  });
  if (txn) {
    // Sequence accounting must include both on-disk traces and traces
    // already staged in this transaction so a single commit that
    // writes (entry-N, exit-N) gets sequential numbers even though
    // neither file has been moved into place yet.
    const seq = nextTraceSequence(runId, opts) + txn.stagedTraceCount();
    const seqStr = String(seq).padStart(4, "0");
    const fileName = `${seqStr}-${phase}-${state}.yaml`;
    const relPath = `fsm-trace/${fileName}`;
    const stagedPath = txn.stage(relPath);
    atomicWriteYaml(stagedPath, payload(seq));
    txn.addStaged(relPath, stagedPath);
    // `path` always points at the bytes on disk right now: inside a
    // transaction the file lives at the staged path until withJournal
    // finalises. `final_path` is what it will become; `staged` flags
    // the transition state so a caller can branch if needed.
    return {
      sequence: seq,
      fileName,
      path: stagedPath,
      final_path: join(txn.runDir, relPath),
      staged: true,
    };
  }
  const dir = ensureRunDir(runId, opts);
  const seq = nextTraceSequence(runId, opts);
  const seqStr = String(seq).padStart(4, "0");
  const fileName = `${seqStr}-${phase}-${state}.yaml`;
  const filePath = join(dir, "fsm-trace", fileName);
  atomicWriteYaml(filePath, payload(seq));
  return { sequence: seq, fileName, path: filePath };
}

export function readTrace(runId, opts = {}) {
  const traceDir = join(runDirPath(runId, opts), "fsm-trace");
  if (!existsSync(traceDir)) return [];
  return readdirSync(traceDir)
    .filter((n) => /^\d+-/.test(n))
    .sort()
    .map((n) => {
      const path = join(traceDir, n);
      const data = parseYaml(readFileSync(path, "utf8"));
      return { fileName: n, path, data };
    });
}

// pruneTraceAfter removes every trace file whose NNNN- sequence is strictly
// greater than the given `sequence`. The file at sequence itself is kept;
// everything past it is unlinked. Returns the count of files removed and
// the names of the files that were pruned (sorted ascending).
//
// Used by fsm-resume: after locating the target state's entry trace at
// sequence N, the caller prunes everything past N so the run resumes
// from a clean slate at that state.
export function pruneTraceAfter(runId, sequence, opts = {}) {
  if (!Number.isInteger(sequence) || sequence < 0) {
    throw new Error(
      `pruneTraceAfter: sequence must be a non-negative integer, got ${sequence}`,
    );
  }
  const traceDir = join(runDirPath(runId, opts), "fsm-trace");
  if (!existsSync(traceDir)) return { removed: 0, files: [] };
  const candidates = readdirSync(traceDir)
    .filter((n) => /^\d+-/.test(n))
    .map((n) => ({ name: n, seq: Number.parseInt(n.split("-", 1)[0], 10) }))
    .filter((entry) => entry.seq > sequence)
    .sort((a, b) => a.seq - b.seq);
  for (const entry of candidates) {
    rmSync(join(traceDir, entry.name), { force: true });
  }
  return { removed: candidates.length, files: candidates.map((c) => c.name) };
}

// ─── atomic-tx journal (A7) ─────────────────────────────────────────────
//
// withJournal(runDir, fn) is the journal-style transaction wrapper that
// makes a multi-file fsm-commit a single atomic step. Inside `fn`, callers
// stage writes to <runDir>/.journal/<txnId>/<mirror-of-relPath> instead of
// the final location. After `fn` returns successfully:
//   1. mark the journal manifest as "ready_to_finalise"
//   2. rename each staged file from its journal path to its final path
//   3. delete the journal directory
//
// If any step throws or the process is killed, the journal stays on disk
// and recovery is explicit:
//   - journalState(runDir) returns { hasJournal, status, txnId, staged }.
//   - discardJournal(runDir, txnId) removes the journal (rollback).
//   - replayJournal(runDir, txnId) idempotently finalises a journal whose
//     status is "ready_to_finalise" — safe to call repeatedly.
//
// fsm-next refuses to advance while a journal is present; fsm-resume
// exposes `--journal discard|replay` as the recovery path.

export function journalRoot(runDir) {
  return join(runDir, JOURNAL_DIR_NAME);
}

// assertSafeJournalRelPath rejects any relPath that could escape the
// journal directory when joined against it. Same constraints we apply to
// loop iteration_outputs_dir in fsm-engine: no absolute paths, no
// backslashes, no ".." or "." segments, no ":" (Windows drive letters or
// colon-bearing segments). This is the validation gate for the public
// `transaction.stage()` API surface AND for relPath values read back
// from a journal manifest during replayJournal — a crafted or corrupted
// journal.json must not be able to rename staged files outside the run
// directory.
// assertWithin verifies that `targetDir`, after symlink resolution,
// lies inside `parentDir`. Catches the symlink-escape class:
// `assertSafeJournalRelPath` validates relPath segments string-side,
// but a crafted journal directory containing a symlinked subdir (e.g.
// `<txnDir>/fsm-trace -> /etc`) could still make `join(txnDir, relPath)`
// resolve outside the journal root and let renameSync touch arbitrary
// paths. We resolve real paths just before the rename and refuse if
// the target escapes its parent.
//
// targetDir is created with mkdirSync(..., { recursive: true }) by the
// caller before this check, so realpathSync will succeed on it.
// parentDir is always either runDir or txnDir; both are known to exist
// at this point.
function assertWithin(targetDir, parentDir, context) {
  const realTarget = realpathSync(targetDir);
  const realParent = realpathSync(parentDir);
  // Equal real paths are fine (target IS parent). Otherwise, the real
  // target must start with `realParent + path.sep` so a sibling like
  // "/runs/abc" does not satisfy a containment check for "/runs/ab".
  if (
    realTarget !== realParent &&
    !realTarget.startsWith(realParent + pathSep)
  ) {
    throw new Error(
      `${context}: refusing to operate on path "${targetDir}" — resolves to "${realTarget}" which is outside "${realParent}"`,
    );
  }
}

// assertSafeTxnId rejects any txnId that contains path separators,
// traversal segments, colons, or backslashes, so a caller-supplied
// value can never escape <journalRoot>. discardJournal and
// replayJournal each rmSync / renameSync against a path computed as
// `join(journalRoot(runDir), txnId)`; without this guard, a crafted
// txnId like "../../" would let those functions touch arbitrary paths
// on disk. Internally-generated txn ids look like
// "2026-05-29T18-30-45-123Z-abc123" — a single safe filesystem
// segment.
function assertSafeTxnId(txnId, context) {
  if (typeof txnId !== "string" || txnId.length === 0) {
    throw new Error(`${context}: txnId must be a non-empty string`);
  }
  if (
    txnId === "." ||
    txnId === ".." ||
    txnId.includes("/") ||
    txnId.includes("\\") ||
    txnId.includes(":")
  ) {
    throw new Error(
      `${context}: txnId "${txnId}" must be a single safe filesystem segment ` +
        "(no '/', '\\\\', ':', '.' or '..')",
    );
  }
}

function assertSafeJournalRelPath(relPath, context) {
  if (typeof relPath !== "string" || relPath.length === 0) {
    throw new Error(`${context}: relPath must be a non-empty string`);
  }
  if (relPath.includes("\\")) {
    throw new Error(
      `${context}: relPath "${relPath}" must not contain backslashes (use forward slashes)`,
    );
  }
  if (relPath.startsWith("/")) {
    throw new Error(
      `${context}: relPath "${relPath}" must be relative; absolute paths are not allowed`,
    );
  }
  if (relPath.includes(":")) {
    throw new Error(
      `${context}: relPath "${relPath}" must not contain ":" (Windows drive letters or colon segments could escape the journal)`,
    );
  }
  const parts = relPath.split("/");
  for (const part of parts) {
    if (part === "" || part === "." || part === "..") {
      throw new Error(
        `${context}: relPath "${relPath}" contains an invalid segment ("${part}")`,
      );
    }
  }
}

// publicJournalProjection returns the stable error-payload shape that
// fsm-commit / fsm-next / fsm-inspect emit when reporting a journal.
// Filters out internal fields (txnDir, runDir, all[]) so the CLI surface
// is the same shape regardless of whether the journal was observed via
// journalState() up front or surfaced through a withJournal
// JOURNAL_PRESENT error.
export function publicJournalProjection(jstate) {
  if (!jstate || !jstate.hasJournal) return { present: false };
  const stagedNormalised = (jstate.staged || []).map((s) =>
    typeof s === "string" ? s : s.relPath,
  );
  return {
    txn_id: jstate.txnId,
    status: jstate.status,
    staged: stagedNormalised,
  };
}

function readJournalManifestSync(txnDir) {
  const path = join(txnDir, "journal.json");
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function writeJournalManifestSync(txnDir, data) {
  atomicWriteJson(join(txnDir, "journal.json"), data);
}

// journalState inspects <runDir>/.journal/ and returns the most recent
// transaction if any. Returns { hasJournal:false } when the directory is
// absent or empty.
export function journalState(runDir) {
  const root = journalRoot(runDir);
  if (!existsSync(root)) return { hasJournal: false };
  const entries = readdirSync(root).filter((n) => {
    const p = join(root, n);
    try {
      return statSync(p).isDirectory();
    } catch {
      return false;
    }
  });
  if (entries.length === 0) return { hasJournal: false };
  // Most recent by name (txnId is timestamp-prefixed) is the active one.
  const txnIds = entries.sort();
  const all = txnIds.map((id) => {
    const txnDir = join(root, id);
    const manifest = readJournalManifestSync(txnDir);
    return { txnId: id, txnDir, manifest };
  });
  const active = all[all.length - 1];
  return {
    hasJournal: true,
    txnId: active.txnId,
    txnDir: active.txnDir,
    status: active.manifest ? active.manifest.status : "unknown",
    staged: active.manifest ? active.manifest.staged_files || [] : [],
    runDir: active.manifest ? active.manifest.run_dir : runDir,
    all,
  };
}

// discardJournal removes a specific journal transaction directory. If the
// transaction is "ready_to_finalise", callers should usually replay instead
// — discarding loses the work — but the explicit choice is the user's.
export function discardJournal(runDir, txnId) {
  assertSafeTxnId(txnId, "discardJournal");
  const txnDir = join(journalRoot(runDir), txnId);
  if (!existsSync(txnDir)) {
    return { discarded: false, reason: "not_found" };
  }
  rmSync(txnDir, { recursive: true, force: true });
  // Clean up empty journal root.
  const root = journalRoot(runDir);
  if (existsSync(root) && readdirSync(root).length === 0) {
    try {
      rmdirSync(root);
    } catch {
      // Race on cleanup is harmless.
    }
  }
  return { discarded: true, txnId };
}

// replayJournal finalises a "ready_to_finalise" journal by renaming each
// staged file to its final location. Safe to re-run: a missing staged
// source is treated as already-finalised.
export function replayJournal(runDir, txnId) {
  assertSafeTxnId(txnId, "replayJournal");
  const txnDir = join(journalRoot(runDir), txnId);
  const manifest = readJournalManifestSync(txnDir);
  if (!manifest) {
    return { replayed: false, reason: "no_manifest" };
  }
  if (manifest.status !== "ready_to_finalise") {
    return { replayed: false, reason: `status_${manifest.status}` };
  }
  const finalised = [];
  for (const entry of manifest.staged_files || []) {
    // Defensive: the journal manifest is a file on disk and can be
    // corrupted, hand-edited, or maliciously crafted between the crash
    // and recovery. Reject any relPath that could escape the run dir
    // via absolute path, ".." traversal, backslashes, or drive letters
    // before letting it into the rename loop.
    assertSafeJournalRelPath(entry.relPath, "replayJournal");
    const stagedPath = join(txnDir, entry.relPath);
    const finalPath = join(runDir, entry.relPath);
    if (!existsSync(stagedPath)) {
      finalised.push({ relPath: entry.relPath, already: true });
      continue;
    }
    // Symlink-escape guard: relPath segments are valid strings, but a
    // symlinked subdir along the resolved path (e.g.
    // `<txnDir>/fsm-trace -> /etc`) could still make join() resolve
    // outside the journal root or run root. Verify the real path of
    // both the staged source dir AND the final destination dir stays
    // inside their respective parents before any rename.
    assertWithin(dirname(stagedPath), txnDir, "replayJournal (stagedPath)");
    mkdirSync(dirname(finalPath), { recursive: true });
    assertWithin(dirname(finalPath), runDir, "replayJournal (finalPath)");
    renameSync(stagedPath, finalPath);
    finalised.push({ relPath: entry.relPath, already: false });
  }
  rmSync(txnDir, { recursive: true, force: true });
  const root = journalRoot(runDir);
  if (existsSync(root) && readdirSync(root).length === 0) {
    try {
      rmdirSync(root);
    } catch {
      // Empty-dir cleanup race is harmless.
    }
  }
  return { replayed: true, txnId, finalised };
}

// withJournal(runDir, fn): runs `fn(txn)` inside a journal transaction.
// `fn` MUST route its file writes through the storage helpers with
// `opts.transaction = txn` so writes land in the journal. After fn returns,
// the journal is marked ready_to_finalise and the rename loop runs.
// On any throw, the journal is left in place for explicit recovery.
//
// FSM_TEST_PAUSE_BEFORE_FINALISE=<ms> (env): synchronously sleep before
// the rename loop. Lets integration tests SIGKILL the process at that
// instant to simulate a crash between "fn returned" and "finalisation".
export function withJournal(runDir, fn) {
  if (!runDir || typeof runDir !== "string") {
    throw new Error("withJournal: runDir is required");
  }
  if (typeof fn !== "function") {
    throw new Error("withJournal: fn must be a function");
  }
  // Refuse to start a new transaction while an unrecovered journal exists.
  const existing = journalState(runDir);
  if (existing.hasJournal) {
    const err = new Error(
      `withJournal: refusing to start — existing journal txn=${existing.txnId} status=${existing.status}; recover via fsm-resume --journal {discard|replay}`,
    );
    err.code = "JOURNAL_PRESENT";
    err.journal = existing;
    throw err;
  }

  const txnId = `${new Date().toISOString().replace(/[:.]/g, "-")}-${randomBytes(3).toString("hex")}`;
  const root = journalRoot(runDir);
  const txnDir = join(root, txnId);
  mkdirSync(txnDir, { recursive: true });

  const stagedFiles = [];

  const txn = {
    txnId,
    runDir,
    txnDir,
    staged: stagedFiles,
    stage(relPath) {
      // stage() is the public surface for staging a write inside a
      // transaction. Reject any relPath that could escape the txn dir
      // here, so callers (including third-party storage helpers) cannot
      // accidentally write outside <txnDir> by passing an absolute path
      // or a ".."-bearing string.
      assertSafeJournalRelPath(relPath, "transaction.stage");
      const stagedPath = join(txnDir, relPath);
      mkdirSync(dirname(stagedPath), { recursive: true });
      return stagedPath;
    },
    addStaged(relPath, stagedPath) {
      assertSafeJournalRelPath(relPath, "transaction.addStaged");
      stagedFiles.push({ relPath, stagedPath });
    },
    stagedTraceCount() {
      return stagedFiles.filter((s) => s.relPath.startsWith("fsm-trace/"))
        .length;
    },
    readStagedManifest() {
      const entry = stagedFiles.find((s) => s.relPath === "manifest.json");
      if (!entry) return null;
      try {
        return JSON.parse(readFileSync(entry.stagedPath, "utf8"));
      } catch {
        return null;
      }
    },
  };

  // Write the pending manifest first so a crash during fn still leaves a
  // discoverable journal.
  writeJournalManifestSync(txnDir, {
    txn_id: txnId,
    status: "pending",
    run_dir: runDir,
    started_at: new Date().toISOString(),
    staged_files: [],
  });

  let result;
  try {
    result = fn(txn);
  } catch (err) {
    // Leave journal on disk for inspection / discard.
    throw err;
  }

  // Reject async / thenable functions. withJournal finalises
  // synchronously the instant fn returns; if fn is async, the awaited
  // writes are still in-flight when the rename loop runs and the
  // atomicity contract breaks. Caught here (after fn has already
  // returned the Promise) the journal is still on disk, so the user
  // can discard it; failing fast is much better than producing a
  // partial commit.
  if (result && typeof result.then === "function") {
    const err = new Error(
      "withJournal: fn must be a synchronous function — got a thenable / Promise. " +
        "Stage all writes synchronously inside fn; the journal would otherwise finalise " +
        "before async writes complete and break atomicity.",
    );
    err.code = "JOURNAL_FN_ASYNC";
    throw err;
  }

  // Mark ready_to_finalise BEFORE the rename loop so a crash mid-rename
  // can be safely replayed (idempotent).
  writeJournalManifestSync(txnDir, {
    txn_id: txnId,
    status: "ready_to_finalise",
    run_dir: runDir,
    started_at: new Date().toISOString(),
    ready_at: new Date().toISOString(),
    staged_files: stagedFiles.map((s) => ({ relPath: s.relPath })),
  });

  const pauseMs = Number.parseInt(
    process.env.FSM_TEST_PAUSE_BEFORE_FINALISE || "0",
    10,
  );
  if (Number.isFinite(pauseMs) && pauseMs > 0) {
    // Synchronous block so a SIGKILL during this window simulates a
    // crash between "ready_to_finalise" and the rename loop. Uses
    // Atomics.wait on a private SharedArrayBuffer for a real sleep
    // (no CPU burn) instead of `while (Date.now() < deadline)`, which
    // pegged a core for the duration whenever the env var was set —
    // including accidentally in prod or CI. The wait condition is
    // permanently false (we never store to view[0]) so the call
    // returns "timed-out" after pauseMs every time.
    const sab = new SharedArrayBuffer(4);
    const view = new Int32Array(sab);
    Atomics.wait(view, 0, 0, pauseMs);
  }

  for (const entry of stagedFiles) {
    const finalPath = join(runDir, entry.relPath);
    mkdirSync(dirname(finalPath), { recursive: true });
    // Symlink-escape guard: if a subdir under runDir was replaced by a
    // symlink between fn() returning and the rename loop (or by a
    // malicious actor with write access), renameSync could write
    // outside the run directory. Verify the resolved real path of
    // dirname(finalPath) stays within realpathSync(runDir) before
    // moving the staged file into place.
    assertWithin(dirname(finalPath), runDir, "withJournal (finalPath)");
    renameSync(entry.stagedPath, finalPath);
  }
  rmSync(txnDir, { recursive: true, force: true });
  if (existsSync(root) && readdirSync(root).length === 0) {
    try {
      rmdirSync(root);
    } catch {
      // Empty-dir cleanup race is harmless.
    }
  }

  return { result, txnId, staged: stagedFiles.map((s) => s.relPath) };
}

// ─── cross-run queries ──────────────────────────────────────────────────

// listRecentRuns walks the date-sharded directory tree for the last
// `daysBack` days, reading each run's manifest.json and returning summaries.
export function listRecentRuns({ daysBack = 30, now = new Date(), storageRoot, filter } = {}) {
  if (!storageRoot) {
    throw new Error("listRecentRuns: storageRoot is required");
  }
  const root = resolve(storageRoot);
  if (!existsSync(root)) return [];
  const cutoff = new Date(now.getTime() - daysBack * 24 * 60 * 60 * 1000);
  const out = [];
  const years = readdirSync(root).filter((n) => /^\d{4}$/.test(n));
  for (const yyyy of years) {
    const yearPath = join(root, yyyy);
    if (!statSync(yearPath).isDirectory()) continue;
    const months = readdirSync(yearPath).filter((n) => /^\d{2}$/.test(n));
    for (const mm of months) {
      const monthPath = join(yearPath, mm);
      if (!statSync(monthPath).isDirectory()) continue;
      const days = readdirSync(monthPath).filter((n) => /^\d{2}$/.test(n));
      for (const dd of days) {
        const dayPath = join(monthPath, dd);
        if (!statSync(dayPath).isDirectory()) continue;
        const dayDate = new Date(`${yyyy}-${mm}-${dd}T00:00:00.000Z`);
        if (dayDate < cutoff) continue;
        const shards = readdirSync(dayPath).filter((n) => /^[0-9a-f]{2}$/.test(n));
        for (const shard of shards) {
          const shardPath = join(dayPath, shard);
          if (!statSync(shardPath).isDirectory()) continue;
          const rests = readdirSync(shardPath).filter((n) => /^[0-9a-f]{5}$/.test(n));
          for (const rest of rests) {
            const runDir = join(shardPath, rest);
            const manifestPath = join(runDir, "manifest.json");
            if (!existsSync(manifestPath)) continue;
            try {
              const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
              if (filter && !filter(manifest)) continue;
              out.push({ runDir, manifest });
            } catch {
              // Skip malformed manifests; not load-bearing for cross-run queries.
            }
          }
        }
      }
    }
  }
  return out;
}
