#!/usr/bin/env node
// fsm-validate-static — static well-formedness check on FSM YAML.
//
// Usage:
//   fsm-validate-static <path-to-fsm-yaml> [<more> ...]
//
// Exits 0 on clean, 1 on any validation failure.
// Prints a structured JSON report to stdout.

import { readFileSync, existsSync, writeSync } from "node:fs";
import { resolve } from "node:path";
import { parse as parseYaml } from "yaml";

import {
  validateFsmSchema,
  validateFsmStatic,
} from "./lib/fsm-schema.mjs";

const args = process.argv.slice(2);
if (args.length === 0) {
  process.stderr.write(
    "fsm-validate-static: at least one FSM YAML path is required\n",
  );
  process.stderr.write(
    "  usage: fsm-validate-static <path-to-fsm-yaml> [<more>]\n",
  );
  process.exit(2);
}

let totalErrors = 0;
const reports = [];

for (const arg of args) {
  const path = resolve(process.cwd(), arg);
  if (!existsSync(path)) {
    reports.push({
      file: arg,
      errors: [`File not found: ${path}`],
    });
    totalErrors += 1;
    continue;
  }

  let doc;
  try {
    doc = parseYaml(readFileSync(path, "utf8"));
  } catch (err) {
    reports.push({
      file: arg,
      errors: [`YAML parse failure: ${err.message}`],
    });
    totalErrors += 1;
    continue;
  }

  const schemaResult = validateFsmSchema(doc);
  if (!schemaResult.valid) {
    reports.push({ file: arg, phase: "schema", errors: schemaResult.errors });
    totalErrors += schemaResult.errors.length;
    continue;
  }

  const staticResult = validateFsmStatic(doc, { fsmFilePath: path });
  if (!staticResult.valid) {
    reports.push({ file: arg, phase: "static", errors: staticResult.errors });
    totalErrors += staticResult.errors.length;
    continue;
  }

  reports.push({
    file: arg,
    phase: "passed",
    errors: [],
    states: doc.fsm.states.length,
  });
}

const summary = {
  ok: totalErrors === 0,
  total_errors: totalErrors,
  files: reports,
};
// writeSync to fd 1 — see fsm-commit.mjs's emit() comment. process.exit
// after process.stdout.write truncates large payloads at the kernel pipe
// buffer (~64KB on macOS); writeSync is a blocking POSIX write that
// drains synchronously. Issue #12.
writeSync(1, `${JSON.stringify(summary, null, 2)}\n`);
process.exit(totalErrors === 0 ? 0 : 1);
