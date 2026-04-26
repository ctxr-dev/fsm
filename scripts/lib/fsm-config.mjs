// fsm-config.mjs — load consumer config (.fsmrc.json or fsm.config.json)
// from the current working directory. CLI args override config file values.
//
// Schema (multi-FSM, the going-forward shape):
//
//   {
//     "fsms": [
//       {
//         "name": "<unique-name>",
//         "fsm_path": "<path>",
//         "storage_root": "<path>",
//         "session_id": "<optional>"
//       },
//       ...
//     ]
//   }
//
// Legacy single-FSM shape (still accepted; auto-wrapped as fsms[0] with
// name "default"):
//
//   {
//     "fsm_path": "<path>",
//     "storage_root": "<path>",
//     "session_id": "<optional>"
//   }
//
// CLI selection:
//   - --fsm <name>            pick a named entry from fsms[]
//   - (no --fsm; one entry)   use that single entry
//   - (no --fsm; multiple)    error: must pass --fsm <name>
//   - --fsm-path + --storage-root  bypass the config file entirely

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
  if (raw === null || typeof raw !== "object") {
    throw new Error(`fsm config at ${path} must be a JSON object`);
  }
  // Multi-FSM shape: fsms[] is an array of entries.
  if (Array.isArray(raw.fsms)) {
    return validateFsmArray(raw.fsms, path);
  }
  // Map shape: fsms is a map of name → { fsm_path, storage_root, session_id }.
  // Accepted as a convenience; auto-flattened to the array form.
  if (raw.fsms && typeof raw.fsms === "object") {
    const entries = Object.entries(raw.fsms).map(([name, body]) => ({
      name,
      ...body,
    }));
    return validateFsmArray(entries, path);
  }
  // Legacy single-FSM shape: top-level fsm_path / storage_root.
  if (raw.fsm_path || raw.storage_root) {
    return validateFsmArray(
      [
        {
          name: "default",
          fsm_path: raw.fsm_path,
          storage_root: raw.storage_root,
          session_id: raw.session_id,
        },
      ],
      path,
    );
  }
  throw new Error(
    `fsm config at ${path} must declare either fsms[] (array), fsms{} (map), or top-level fsm_path + storage_root (single-FSM legacy)`,
  );
}

function validateFsmArray(entries, path) {
  if (entries.length === 0) {
    throw new Error(`fsm config at ${path} declares an empty fsms list`);
  }
  const names = new Set();
  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    if (!entry || typeof entry !== "object") {
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
  const sessionId = cliArgs.sessionId ?? entry.session_id ?? defaultSessionId();

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
