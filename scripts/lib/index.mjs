// @ctxr/fsm — generic finite-state-machine substrate.
//
// Programmatic API for consumers who want to embed the engine directly
// instead of going through the CLIs. The CLIs (fsm-next, fsm-commit,
// fsm-inspect, fsm-validate-static) are the recommended interface — they
// give you the structured-emit protocol, atomic state writes, and lock
// management for free. Use this module's exports only when wrapping the
// engine inside a higher-level orchestration layer.

export {
  acquireLock,
  appendTraceFile,
  atomicWriteFile,
  atomicWriteJson,
  atomicWriteYaml,
  buildRunId,
  ensureRunDir,
  listRecentRuns,
  parseRunId,
  readLock,
  readManifest,
  readTrace,
  releaseLock,
  runDirPath,
  writeManifest,
} from "./fsm-storage.mjs";

export {
  evaluatePredicate,
  parsePredicate,
} from "./fsm-predicates.mjs";

export {
  hashFsmYaml,
  validateFsmSchema,
  validateFsmStatic,
  validateWorkerResponse,
} from "./fsm-schema.mjs";

export {
  buildBrief,
  initialiseManifest,
  loadFsm,
  resolveTransition,
  runEnv,
  runPostValidations,
  stateById,
  updateManifest,
  validateOutputs,
  writeEntryTrace,
  writeExitTrace,
  writeFaultTrace,
} from "./fsm-engine.mjs";

export { loadConfig, resolveSettings } from "./fsm-config.mjs";
