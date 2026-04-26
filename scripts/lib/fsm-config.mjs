// fsm-config.mjs — load consumer config (.fsmrc.json or fsm.config.json)
// from the current working directory. CLI args override config file values.
//
// The config file is STATIC. It only describes project-level setup:
// where each FSM YAML lives and where its run dirs go. Runtime concerns
// (session_id, run-id, process state) are never persisted in the config.
//
// Canonical schema:
//
//   {
//     "fsms": [
//       {
//         "name": "<unique-name>",
//         "fsm_path": "<path>",
//         "storage_root": "<path>"
//       },
//       ...
//     ]
//   }
//
// `fsms[]` is the only accepted shape. There is no top-level
// `fsm_path` / `storage_root` form. There is no map shape. One canonical
// schema, no migration paths.
//
// CLI selection:
//   - --fsm <name>            pick a named entry from fsms[]
//   - (no --fsm; one entry)   use that single entry
//   - (no --fsm; multiple)    error: must pass --fsm <name>
//   - --fsm-path + --storage-root  bypass the config file entirely
//
// session_id is always runtime: pass --session-id <id> on the CLI, or
// the engine generates a default of "session-<pid>-<timestamp>".

import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const CANDIDATE_FILES = [".fsmrc.json", "fsm.config.json"];

// loadConfig returns { fsms: [{ name, fsm_path, storage_root, session_id? }], _config_path }
// or { fsms: [] } if no config file is present.
//
// The legacy single-FSM shape is normalised to a one-element array with
// name "default".
//
// Throws on malformed JSON, missing required fields, or duplicate names.
export function loadConfig(cwd = process.cwd()) {
  for (const name of CANDIDATE_FILES) {
    const path = join(cwd, name);
    if (existsSync(path)) {
      let raw;
      try {
        raw = JSON.parse(readFileSync(path, "utf8"));
      } catch (err) {
        throw new Error(`fsm config at ${path} is malformed JSON: ${err.message}`);
      }
      const fsms = normaliseConfig(raw, path);
      return { fsms, _config_path: path };
    }
  }
  return { fsms: [] };
}

function normaliseConfig(raw, path) {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`fsm config at ${path} must be a JSON object with an "fsms" array`);
  }
  if (!Array.isArray(raw.fsms)) {
    throw new Error(`fsm config at ${path} must declare "fsms" as an array of FSM entries`);
  }
  return validateFsmArray(raw.fsms, path);
}

function validateFsmArray(entries, path) {
  if (entries.length === 0) {
    throw new Error(`fsm config at ${path} declares an empty fsms list`);
  }
  const names = new Set();
  const allowedKeys = new Set(["name", "fsm_path", "storage_root"]);
  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
      throw new Error(`fsm config at ${path}: fsms[${i}] must be an object`);
    }
    if (typeof entry.name !== "string" || !entry.name) {
      throw new Error(`fsm config at ${path}: fsms[${i}].name must be a non-empty string`);
    }
    if (names.has(entry.name)) {
      throw new Error(`fsm config at ${path}: duplicate fsm name "${entry.name}"`);
    }
    names.add(entry.name);
    if (typeof entry.fsm_path !== "string" || !entry.fsm_path) {
      throw new Error(`fsm config at ${path}: fsms[${i} = "${entry.name}"].fsm_path must be a non-empty string`);
    }
    if (typeof entry.storage_root !== "string" || !entry.storage_root) {
      throw new Error(`fsm config at ${path}: fsms[${i} = "${entry.name}"].storage_root must be a non-empty string`);
    }
    for (const key of Object.keys(entry)) {
      if (!allowedKeys.has(key)) {
        throw new Error(
          `fsm config at ${path}: fsms[${i} = "${entry.name}"] has unknown key "${key}". Allowed keys: name, fsm_path, storage_root.`,
        );
      }
    }
  }
  return entries;
}

// resolveSettings merges CLI args over config-file values, applies
// defaults, and returns a normalised settings object.
//
// `cliArgs` keys (all optional):
//   - fsmName       — pick a named entry from .fsmrc.json fsms[]
//   - fsmPath       — direct path override (bypasses fsmName lookup)
//   - storageRoot   — direct override
//   - sessionId     — direct override
//
// Resolution rules:
//
//   1. If both fsmPath and storageRoot are passed via CLI, they bypass
//      the config file entirely. The config is only consulted for an
//      optional default sessionId.
//
//   2. Otherwise, resolve a named entry:
//      a. If --fsm <name> is given, look up that name in fsms[].
//      b. Else, if exactly one entry exists in fsms[], use it.
//      c. Else, throw with the list of available names.
//
//      CLI overrides for fsmPath / storageRoot / sessionId are then
//      layered on top of the chosen entry.
//
// Returns { fsmPath, storageRoot, sessionId, configPath, selectedName }.
export function resolveSettings(cliArgs = {}, cwd = process.cwd()) {
  const cfg = loadConfig(cwd);
  const direct = cliArgs.fsmPath && cliArgs.storageRoot;

  if (direct) {
    return {
      fsmPath: resolve(cwd, cliArgs.fsmPath),
      storageRoot: resolve(cwd, cliArgs.storageRoot),
      sessionId: cliArgs.sessionId ?? defaultSessionId(),
      configPath: cfg._config_path ?? null,
      selectedName: null,
    };
  }

  let entry;
  if (cliArgs.fsmName) {
    entry = cfg.fsms.find((f) => f.name === cliArgs.fsmName);
    if (!entry) {
      const available = cfg.fsms.map((f) => f.name).join(", ") || "(none)";
      throw new Error(
        `fsm: --fsm "${cliArgs.fsmName}" not found in ${cfg._config_path ?? ".fsmrc.json"}. Available: ${available}`,
      );
    }
  } else if (cfg.fsms.length === 1) {
    entry = cfg.fsms[0];
  } else if (cfg.fsms.length === 0) {
    throw new Error(
      "fsm: no FSM configured. Pass --fsm-path + --storage-root, or add a .fsmrc.json with fsms[].",
    );
  } else {
    const available = cfg.fsms.map((f) => f.name).join(", ");
    throw new Error(
      `fsm: multiple FSMs configured (${available}). Pass --fsm <name> to pick one, or --fsm-path + --storage-root to bypass the config.`,
    );
  }

  const fsmPath = cliArgs.fsmPath ?? entry.fsm_path;
  const storageRoot = cliArgs.storageRoot ?? entry.storage_root;
  const sessionId = cliArgs.sessionId ?? defaultSessionId();

  return {
    fsmPath: resolve(cwd, fsmPath),
    storageRoot: resolve(cwd, storageRoot),
    sessionId,
    configPath: cfg._config_path ?? null,
    selectedName: entry.name,
  };
}

function defaultSessionId() {
  return `session-${process.pid}-${Date.now()}`;
}
