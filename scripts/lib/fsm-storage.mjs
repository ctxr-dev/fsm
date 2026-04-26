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
  renameSync,
  rmSync,
  writeFileSync,
  openSync,
  closeSync,
  fsyncSync,
  statSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { stringify as stringifyYaml, parse as parseYaml } from "yaml";

const LOCK_TTL_MS_DEFAULT = 60 * 60 * 1000; // 1 hour

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
  const path = join(runDirPath(runId, opts), "manifest.json");
  if (!existsSync(path)) return null;
  return JSON.parse(readFileSync(path, "utf8"));
}

export function writeManifest(runId, data, opts = {}) {
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
  if (!["entry", "exit", "fault"].includes(phase)) {
    throw new Error(`appendTraceFile: phase must be entry|exit|fault, got "${phase}"`);
  }
  if (!state || typeof state !== "string") {
    throw new Error("appendTraceFile: state must be a non-empty string");
  }
  const dir = ensureRunDir(runId, opts);
  const seq = nextTraceSequence(runId, opts);
  const seqStr = String(seq).padStart(4, "0");
  const fileName = `${seqStr}-${phase}-${state}.yaml`;
  const filePath = join(dir, "fsm-trace", fileName);
  const payload = {
    phase,
    state,
    sequence: seq,
    timestamp: new Date().toISOString(),
    ...data,
  };
  atomicWriteYaml(filePath, payload);
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
