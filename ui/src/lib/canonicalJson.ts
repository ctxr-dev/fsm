/**
 * Canonical JSON serialiser (RFC 8785-flavoured).
 *
 * Produces a deterministic, key-sorted, whitespace-minimal JSON
 * string. The contract MUST match the Python-side `canonical_json` in
 * `ctxr/fsm/core/canonical.py` so that an outputs/inputs hash computed
 * in the browser matches the one the server computed at commit time
 * (W18d's "verify cosignature locally" button in the Signature Ledger
 * relies on this).
 *
 * Differences from `JSON.stringify`:
 *
 *   1. Object keys are sorted lexicographically (UTF-16 code-unit
 *      order, matching `Array.prototype.sort` default — which is also
 *      what Python's `sorted()` uses for str inputs).
 *   2. No whitespace anywhere (no indentation, no spaces between
 *      separators).
 *   3. Numbers go through the standard JSON.stringify encoding (so
 *      `1` not `1.0`, `1e+21` for big exponents, etc.) — Python's
 *      canonical_json uses the SAME rule via `json.dumps(..., sort_keys=True,
 *      separators=(',', ':'))`. Floats with exact integer values
 *      ARE encoded WITHOUT a decimal point. Cross-language tests
 *      pin this.
 *   4. `undefined` and functions throw (JSON-incompatible); the same
 *      shape would be silently dropped by JSON.stringify, which is
 *      wrong for a cosignature input.
 *   5. NaN / +Infinity / -Infinity throw (JSON doesn't have these
 *      values; Python's json refuses them with allow_nan=False which
 *      we mirror).
 *
 * Edge cases that are NOT supported in v1 (each would force a heavier
 * library; flag if a real consumer hits one):
 *
 *   - BigInt: throws. JSON.stringify would also throw.
 *   - Cyclic references: hangs. Same as JSON.stringify behaviour
 *     (which is "TypeError: Converting circular structure to JSON").
 *     We propagate.
 *   - Date objects: serialised via `value.toISOString()` (matching
 *     Python `datetime.isoformat()`). Tests pin this.
 */

class CanonicalJsonError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CanonicalJsonError';
  }
}

export { CanonicalJsonError };

export function canonicalJson(value: unknown): string {
  return encode(value);
}

function encode(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) {
      throw new CanonicalJsonError(
        `non-finite number not representable in canonical JSON: ${value}`,
      );
    }
    return JSON.stringify(value);
  }
  if (typeof value === 'bigint') {
    throw new CanonicalJsonError('BigInt not representable in canonical JSON');
  }
  if (typeof value === 'function' || typeof value === 'undefined') {
    throw new CanonicalJsonError(
      `value of type ${typeof value} not representable in canonical JSON`,
    );
  }
  if (value instanceof Date) {
    return JSON.stringify(value.toISOString());
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    return `[${value.map(encode).join(',')}]`;
  }
  if (typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj).sort();
    if (keys.length === 0) return '{}';
    const parts: string[] = [];
    for (const key of keys) {
      const v = obj[key];
      if (typeof v === 'undefined') continue; // mirror JSON.stringify drop-undefined for objects
      parts.push(`${JSON.stringify(key)}:${encode(v)}`);
    }
    return `{${parts.join(',')}}`;
  }
  // Symbol or other exotic — JSON.stringify would also fail.
  throw new CanonicalJsonError(
    `value of type ${typeof value} not representable in canonical JSON`,
  );
}

/**
 * SHA-256 a string using the SubtleCrypto API. Used by the Signature
 * Ledger to verify cosignatures: `sha256Hex(brief_id || canonical_json(inputs)
 * || canonical_json(outputs) || session_id)`.
 *
 * Returns the digest as a lowercase hex string (64 chars), matching
 * Python's `hashlib.sha256(...).hexdigest()`.
 *
 * Throws if SubtleCrypto is unavailable (extremely rare; only
 * non-secure-context pre-2020 browsers).
 */
export async function sha256Hex(text: string): Promise<string> {
  if (typeof crypto === 'undefined' || !crypto.subtle?.digest) {
    throw new Error('SubtleCrypto unavailable; cannot hash');
  }
  const enc = new TextEncoder();
  const data = enc.encode(text);
  const buf = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}
