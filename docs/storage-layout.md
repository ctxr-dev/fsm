# `@ctxr/fsm` — Storage Layout

The package writes only to `<storage-root>/`. Consumers configure `storage_root` via `--storage-root` flags or `.fsmrc.json`. No other paths are touched.

## Layout

```text
<storage-root>/
  <yyyy>/                           ← year folder (UTC)
    <mm>/                           ← month folder
      <dd>/                         ← day folder
        <ab>/                       ← first 2 hex chars of run-id hash (256 shards)
          <rest>/                   ← remaining 5 hex chars of run-id
            manifest.json           ← run summary + status
            lock.json               ← present while a session holds the run lock
            fsm-trace/              ← state transition records
              0001-entry-<state>.yaml
              0002-exit-<state>.yaml
              ...
            workers/                ← consumer-managed worker artifacts (optional)
```

### Why each path component

- `<yyyy>/<mm>/<dd>/`: human navigation. `ls <storage-root>/2026/04/26/` shows everything from one day.
- `<ab>/<rest>/`: filesystem-perf shard within day. APFS / ext4 directory lookups degrade around 10k flat entries; 256 shards × ≤40 entries/day keeps lookups fast at any plausible volume.
- Per-run subdirectory: all artifacts colocated; trivial to archive, share, `git add -f`, or delete.

## Run-id format

```text
run-id = <YYYYMMDD>-<HHMMSS>-<hash7>
hash7  = first 7 hex chars of sha256(repo + base-sha + head-sha + iso-timestamp + random-nonce)
shard  = first 2 chars of hash7
```

The shard prefix derives from the hash, not the timestamp, so timestamp-clustered runs (a CI burst writing 50 PRs in 5 minutes) still distribute evenly across the 256 shards.

## Atomic write contract

Every write to the storage root uses `write-tmp + fsync + rename`:

1. Open a unique tmp file beside the target.
2. Write all bytes to the tmp file.
3. `fsync` the tmp file's file descriptor.
4. `rename` the tmp file to the target path. POSIX guarantees atomic rename within a filesystem.

A crash mid-write leaves either the old contents intact (rename never happened) or the new contents intact (rename completed). No partial writes are observable.

## Lock contract

`lock.json` is created with `O_EXCL` — atomic create-if-not-exists. The package writes:

```json
{
  "run_id": "<id>",
  "session_id": "<caller-supplied>",
  "pid": 12345,
  "acquired_at": "2026-04-26T...Z",
  "expires_at": "2026-04-26T...Z"
}
```

### Acquisition algorithm

1. Try `open(lock.json, O_CREAT | O_EXCL | O_WRONLY)`. On success, write contents, return `{ acquired: true, lock }`.
2. On `EEXIST`, read existing lock. If `expires_at < now`, treat as stale: delete and retry from step 1 once.
3. If stale-recovery succeeds: return `{ acquired: true, lock, stale_recovered: true, prior_lock }`.
4. If a third party raced and re-created during stale recovery: re-read and return `{ acquired: false, lock }`.
5. If lock is still active (`expires_at > now`): return `{ acquired: false, lock }`.

### Release

`releaseLock` removes `lock.json` only if the caller's `session_id` matches the lock's `session_id`. This prevents one session from accidentally releasing another's lock.

### TTL

Default: 1 hour. Configurable via `acquireLock(..., { ttlMs })`. The TTL is the safety net for crashed sessions; under normal flow the lock is released as soon as `fsm-commit` finishes.

## Manifest schema

See [`orchestration-design.md`](orchestration-design.md) "File schemas" for the full manifest. Key invariants:

- `run_id` matches the directory's run-id.
- `fsm_yaml_hash` is set at run-start and never modified — the package compares this against the current FSM YAML's hash on every `fsm-next --resume` and `fsm-commit`.
- `status` enum: `in_progress | paused | completed | faulted | abandoned | stale | superseded`.
- `current_state` is the last-entered state (the one the orchestrator is currently working on); `next_state` is null between commits.
- `transitions_count` increments on every `fsm-commit`.

## Trace records

Sequential `NNNN-{phase}-<state>.yaml` files. The number is zero-padded to 4 digits and monotonically increases per run. `phase` is one of:

- `entry` — written when `fsm-next` returns the brief for the state.
- `exit` — written when `fsm-commit` advances past the state.
- `fault` — written when validation fails or no transition matches; replaces the exit.

Each record has the structure:

```yaml
phase: <entry | exit | fault>
state: <state-id>
sequence: <integer>
timestamp: "<ISO 8601>"
# entry-only:
preconditions: [...]
inputs: { ... }
# exit-only:
outputs: { ... }
post_validations: [...]
transition_evaluation: [...]
transition: <chosen-state-id-or-null>
# fault-only:
reason: "<error-code>"
details: <any>
```

## Cross-run queries

`listRecentRuns({ daysBack, storageRoot, filter })` walks the date-sharded tree for the last N days, reads each manifest, and returns matching summaries.

The walk is bounded: it ignores days older than `daysBack` and skips empty shards / rests. At 1000 runs/year, the walk completes in well under a second.

## Config file

`.fsmrc.json` (or `fsm.config.json`) at the consumer's project root. The `fsms[]` array supports multiple named FSMs (e.g. one per agent or logical pipeline):

```json
{
  "fsms": [
    {
      "name": "<unique-name>",
      "fsm_path": "fsm/<name>.fsm.yaml",
      "storage_root": ".<consumer>/runs/<name>"
    }
  ]
}
```

Each entry accepts exactly three keys: `name`, `fsm_path`, `storage_root`. Unknown keys are rejected. The config is purely static — runtime concerns like `session_id` are passed via CLI flags or auto-generated.

CLIs select with `--fsm <name>`. With a single entry, `--fsm` is optional. With multiple entries, omitting `--fsm` errors with the list of available names.

Relative paths resolve against `process.cwd()`. `--fsm-path` + `--storage-root` flags bypass the config file entirely.

## Cleanup recommendations

Consumers can prune the storage tree as desired:

- **By age**: `find <storage-root> -type d -mtime +30 -name '<rest>'` finds run dirs older than 30 days.
- **By status**: walk via `listRecentRuns({ daysBack: 365, filter: m => m.status === 'completed' })` and `rm -rf` the matching `runDir`.
- **By verdict**: similar — filter on `m.verdict === 'GO'`.

The package itself never deletes anything; consumers own the lifecycle.
