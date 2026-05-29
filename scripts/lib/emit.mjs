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

// shellQuote wraps a value in POSIX single-quotes so it can be
// concatenated into a copy-pasteable shell command even when it
// contains spaces, $, backticks, or other special characters. Embedded
// single quotes are handled with the standard `'\''` close-reopen
// trick. Used by the CLI recovery hints in fsm-commit / fsm-next /
// fsm-inspect / fsm-resume.
export function shellQuote(value) {
  if (value === undefined || value === null) return "''";
  const s = String(value);
  return `'${s.replace(/'/g, "'\\''")}'`;
}

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
      // advance would otherwise spin forever. Throw a labeled error
      // instead of silently truncating; each CLI's top-level error
      // handler decides how to surface it (stderr message + non-zero
      // exit). The helper itself stays side-effect-free apart from the
      // intended write, so it remains reusable and testable.
      throw new Error(
        `emitJson: writeSync did not advance (returned ${n}); pipe state is unrecoverable`,
      );
    }
    written += n;
  }
}
