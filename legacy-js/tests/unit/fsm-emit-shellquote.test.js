// fsm-emit-shellquote.test.js — coverage for the shellQuote helper
// used by every CLI recovery hint. The contract: the returned string,
// when concatenated into a `sh -c` command line, expands back to the
// original input regardless of spaces, $, backticks, embedded quotes,
// newlines, or other shell-special characters.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";

import { shellQuote } from "../../scripts/lib/emit.mjs";

function shellExpand(quoted) {
  // Use a real shell to expand the quoted form back; that's the
  // strictest check we can make and matches the operator's
  // copy-paste path.
  return execFileSync("sh", ["-c", `printf %s ${quoted}`], { encoding: "utf8" });
}

test("shellQuote: plain alphanumeric round-trips", () => {
  assert.equal(shellExpand(shellQuote("hello")), "hello");
});

test("shellQuote: spaces preserved", () => {
  assert.equal(shellExpand(shellQuote("a path with spaces")), "a path with spaces");
});

test("shellQuote: shell-special characters do not expand", () => {
  for (const input of [
    "$HOME/no-expansion",
    "`whoami`",
    "rm -rf /; echo pwn",
    "with*glob?chars",
    "with(parens)",
    "with|pipe",
    'with"double"quotes',
    "with\\backslash",
  ]) {
    assert.equal(shellExpand(shellQuote(input)), input, `failed round-trip for: ${input}`);
  }
});

test("shellQuote: embedded single quote handled via close-reopen trick", () => {
  const input = "it's a path";
  assert.equal(shellExpand(shellQuote(input)), input);
});

test("shellQuote: empty string round-trips to empty", () => {
  assert.equal(shellExpand(shellQuote("")), "");
});

test("shellQuote: null/undefined produce empty-quoted form", () => {
  assert.equal(shellQuote(null), "''");
  assert.equal(shellQuote(undefined), "''");
});
