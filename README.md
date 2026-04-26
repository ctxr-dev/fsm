# `@ctxr/fsm`

Generic finite-state-machine substrate for deterministic LLM-orchestrated workflows.

Consumers (skills, agents, CI workflows) declare a YAML state machine + worker prompt templates and call this package's CLIs to drive a run. The CLIs handle:

- Atomic state writes (POSIX `O_EXCL` lock files with TTL; write-tmp+fsync+rename).
- Date-sharded filesystem layout for any volume of runs (256 shards under each day).
- JSON-Schema-validated worker outputs at every state boundary.
- Safe deterministic-predicate DSL (no `eval`, no string interpolation).
- Trace files capturing every transition with inputs / outputs / predicate evaluations.

The package is consumer-agnostic — no project-specific paths, no hardcoded state names. Configure via `--storage-root` / `--fsm-path` flags or a `.fsmrc.json` config file at the consumer's project root.

## Status

**v0.1 — foundations.** Stable interface for the four core CLIs. v0.2 adds lifecycle CLIs (resume, pause, abandon, pivot, stale-cleanup, validate-trace). See [`docs/orchestration-design.md`](docs/orchestration-design.md) for the full roadmap.

## Install

While the package is in early development, consumers reference it via a `file://` path to the sibling repo:

```json
{
  "dependencies": {
    "@ctxr/fsm": "file:../fsm"
  }
}
```

After publication to npm, consumers will use the standard semver form.

## Quick start

1. **Author an FSM YAML** at e.g. `fsm/my-orchestrator.fsm.yaml`. See [`docs/state-yaml-reference.md`](docs/state-yaml-reference.md) for the schema.

2. **Author worker prompt templates** for each state with a `worker:` block. See [`docs/worker-contract.md`](docs/worker-contract.md).

3. **Add a `.fsmrc.json`** at your project root:

   ```json
   {
     "fsm_path": "fsm/my-orchestrator.fsm.yaml",
     "storage_root": ".my-app/runs"
   }
   ```

4. **Validate the FSM**:

   ```bash
   npx fsm-validate-static fsm/my-orchestrator.fsm.yaml
   ```

5. **Drive a run** from your orchestrator:

   ```bash
   # Start a new run
   npx fsm-next --new-run --repo my-app --base-sha aaa --head-sha bbb --args '{"some":"input"}'

   # ...orchestrator dispatches workers, collects JSON outputs...

   # Commit each state's output and advance
   npx fsm-commit --run-id <run-id> --outputs '{"x":42}'

   # Inspect at any time
   npx fsm-inspect --run-id <run-id>
   ```

The CLIs all return JSON to stdout. Exit codes: `0` success, `1` runtime error (lock conflict, schema violation, etc.), `2` argument error.

## Documentation

- [`docs/orchestration-design.md`](docs/orchestration-design.md) — design substrate; failure-mode analysis; architecture; on-disk schemas; roadmap.
- [`docs/cli-reference.md`](docs/cli-reference.md) — exhaustive CLI reference for `fsm-next`, `fsm-commit`, `fsm-inspect`, `fsm-validate-static`.
- [`docs/state-yaml-reference.md`](docs/state-yaml-reference.md) — FSM YAML schema with examples.
- [`docs/worker-contract.md`](docs/worker-contract.md) — worker prompt template conventions and JSON Schema response contract.
- [`docs/storage-layout.md`](docs/storage-layout.md) — disk layout, lock semantics, manifest schema.

## Programmatic API

The package's [`scripts/lib/index.mjs`](scripts/lib/index.mjs) re-exports the engine and helpers for consumers who want to embed the engine directly. The CLIs are the recommended interface — they handle the structured-emit protocol, atomic writes, and lock management for free.

```js
import { evaluatePredicate, loadFsm, runEnv, resolveTransition } from "@ctxr/fsm";

const fsm = loadFsm({ fsmPath: "fsm/my-orchestrator.fsm.yaml" });
const env = runEnv("20260426-001512-a3f7c9b", { storageRoot: ".my-app/runs" });
const { transition } = resolveTransition(stateById(fsm.doc, "my_state"), env);
```

## Tests

```bash
npm install
npm test
```

The test suite covers the storage layer, predicate DSL, schema validators, static FSM validation, and the CLI runtime end-to-end.

## License

MIT.
