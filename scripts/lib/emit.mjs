// Shared emit() helper for the FSM CLIs.
//
// Each CLI (fsm-commit, fsm-next, fsm-inspect, fsm-validate-static) needs
// to write a JSON document to stdout and immediately exit. The naive
// shape — `process.stdout.write(text); process.exit(...)` — truncates
// payloads larger than the kernel pipe buffer (~64 KB on macOS) because
// process.stdout.write is async on a pipe and process.exit terminates
// the process before the userspace queue drains. Issue #12.
//
// fs.writeSync(1, buffer) is a blocking POSIX write to fd 1, but it can
// legally perform a partial write (returns bytes written, not a
// guarantee of full delivery). The loop below retries until the full
// payload reaches the kernel.
//
// EPIPE handling: when the parent reader closes the pipe early, writeSync
// throws EPIPE. We swallow it intentionally — there's no consumer left
// to receive the error, and propagating would mask the (more useful)
// upstream cause that closed the pipe.

import { writeSync } from "node:fs";
import { Buffer } from "node:buffer";

export function emitJson(payload) {
  const text = `${JSON.stringify(payload, null, 2)}\n`;
  const buf = Buffer.from(text, "utf8");
  let written = 0;
  while (written < buf.length) {
    let n;
    try {
      n = writeSync(1, buf, written, buf.length - written);
    } catch (err) {
      if (err && err.code === "EPIPE") return;
      throw err;
    }
    if (typeof n !== "number" || n <= 0) {
      // Defensive: writeSync should always advance or throw. A zero-byte
      // advance would otherwise spin forever; we exit the loop loudly
      // instead of silently truncating. The error reaches stderr (so it
      // appears in spawnSync's `result.stderr` rather than in the stdout
      // JSON the parent is trying to parse) and exits non-zero so the
      // caller's status check fires.
      try {
        process.stderr.write(
          `emitJson: writeSync did not advance (returned ${n}); aborting to avoid infinite loop\n`,
        );
      } catch {
        // stderr unwritable too; nothing useful we can do.
      }
      process.exit(2);
    }
    written += n;
  }
}
