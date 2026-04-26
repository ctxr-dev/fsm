// fsm-config.mjs — load consumer config (.fsmrc.json or fsm.config.json)
// from the current working directory. CLI args override config file values.
//
// Schema:
//   {
//     "fsm_path":     "fsm/code-reviewer.fsm.yaml",
//     "storage_root": ".skill-code-review",
//     "session_id":   "..."   // optional; falls back to PID-based default
//   }

import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const CANDIDATE_FILES = [".fsmrc.json", "fsm.config.json"];

export function loadConfig(cwd = process.cwd()) {
  for (const name of CANDIDATE_FILES) {
    const path = join(cwd, name);
    if (existsSync(path)) {
      try {
        const raw = JSON.parse(readFileSync(path, "utf8"));
        return { ...raw, _config_path: path };
      } catch (err) {
        throw new Error(`fsm config at ${path} is malformed JSON: ${err.message}`);
      }
    }
  }
  return {};
}

// resolveSettings merges CLI args over config-file values, applies
// defaults, and returns a normalised settings object.
//
// `cliArgs` — flat object with possible keys: fsmPath, storageRoot, sessionId.
// `cwd` — used to resolve relative paths and to load the config file.
export function resolveSettings(cliArgs = {}, cwd = process.cwd()) {
  const cfg = loadConfig(cwd);
  const fsmPath = cliArgs.fsmPath ?? cfg.fsm_path;
  const storageRoot = cliArgs.storageRoot ?? cfg.storage_root;
  const sessionId = cliArgs.sessionId
    ?? cfg.session_id
    ?? `session-${process.pid}-${Date.now()}`;
  if (!fsmPath) {
    throw new Error(
      "fsm: fsmPath is required (pass --fsm-path or set fsm_path in .fsmrc.json / fsm.config.json)",
    );
  }
  if (!storageRoot) {
    throw new Error(
      "fsm: storageRoot is required (pass --storage-root or set storage_root in .fsmrc.json / fsm.config.json)",
    );
  }
  return {
    fsmPath: resolve(cwd, fsmPath),
    storageRoot: resolve(cwd, storageRoot),
    sessionId,
    configPath: cfg._config_path ?? null,
  };
}
