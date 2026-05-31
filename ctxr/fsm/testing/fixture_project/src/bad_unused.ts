// Two deliberately-bad TS files to drive a real code review.
// bad_unused.ts: unused variable + missing await + non-null assertion.
import { readFile } from "fs/promises";

const UNUSED_CONSTANT = 42; // unused — bad

export async function loadConfig(path: string | null): Promise<string> {
  // Non-null assertion on possibly-null param — bad.
  const text = readFile(path!, "utf-8"); // missing await — bad
  return text as unknown as string; // double-cast laundering — bad
}
