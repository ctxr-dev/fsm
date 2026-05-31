/**
 * Tests for lib/canonicalJson.ts: canonicalJson + sha256Hex.
 *
 * The contract MUST match Python's `json.dumps(..., sort_keys=True, separators=(',', ':'))`.
 * Tests verify each property:
 *   - Keys sorted lexicographically (matches Python's sorted() default).
 *   - No whitespace anywhere.
 *   - Nested objects respect the rule recursively.
 *   - undefined values are dropped from objects (matches JSON.stringify).
 *   - undefined values throw at top level / in arrays (would be silently dropped by JSON.stringify;
 *     we surface them).
 *   - NaN / Infinity throw (json with allow_nan=False).
 *   - BigInt throws.
 *   - Date → ISO string.
 *   - sha256Hex returns 64-char lowercase hex matching Python hashlib.
 */

import { describe, expect, test } from 'vitest';

import { CanonicalJsonError, canonicalJson, sha256Hex } from '../canonicalJson';

describe('canonicalJson — primitives', () => {
  test('null', () => {
    expect(canonicalJson(null)).toBe('null');
  });
  test('booleans', () => {
    expect(canonicalJson(true)).toBe('true');
    expect(canonicalJson(false)).toBe('false');
  });
  test('numbers', () => {
    expect(canonicalJson(0)).toBe('0');
    expect(canonicalJson(1)).toBe('1');
    expect(canonicalJson(-3.14)).toBe('-3.14');
    expect(canonicalJson(1e21)).toBe('1e+21');
  });
  test('strings escape JSON-spec characters', () => {
    expect(canonicalJson('hello')).toBe('"hello"');
    expect(canonicalJson('a"b')).toBe('"a\\"b"');
    expect(canonicalJson('\n')).toBe('"\\n"');
    expect(canonicalJson('')).toBe('""');
    // unicode literal stays as-is per JSON.stringify default
    expect(canonicalJson('日本語')).toBe('"日本語"');
  });
});

describe('canonicalJson — non-finite / unrepresentable', () => {
  test('NaN throws', () => {
    expect(() => canonicalJson(NaN)).toThrow(CanonicalJsonError);
  });
  test('+Infinity throws', () => {
    expect(() => canonicalJson(Infinity)).toThrow(CanonicalJsonError);
  });
  test('-Infinity throws', () => {
    expect(() => canonicalJson(-Infinity)).toThrow(CanonicalJsonError);
  });
  test('BigInt throws', () => {
    expect(() => canonicalJson(BigInt(1))).toThrow(CanonicalJsonError);
  });
  test('function throws', () => {
    expect(() => canonicalJson(() => 0)).toThrow(CanonicalJsonError);
  });
  test('top-level undefined throws', () => {
    expect(() => canonicalJson(undefined)).toThrow(CanonicalJsonError);
  });
  test('undefined inside an array throws', () => {
    expect(() => canonicalJson([1, undefined, 3])).toThrow(CanonicalJsonError);
  });
  test('symbol throws', () => {
    expect(() => canonicalJson(Symbol('s'))).toThrow(CanonicalJsonError);
  });
});

describe('canonicalJson — arrays', () => {
  test('empty array', () => {
    expect(canonicalJson([])).toBe('[]');
  });
  test('flat array, no whitespace', () => {
    expect(canonicalJson([1, 2, 3])).toBe('[1,2,3]');
    expect(canonicalJson(['a', 'b'])).toBe('["a","b"]');
  });
  test('nested arrays', () => {
    expect(canonicalJson([[1, 2], [3]])).toBe('[[1,2],[3]]');
  });
});

describe('canonicalJson — objects', () => {
  test('empty object', () => {
    expect(canonicalJson({})).toBe('{}');
  });
  test('keys sorted lexicographically', () => {
    expect(canonicalJson({ b: 1, a: 2 })).toBe('{"a":2,"b":1}');
    expect(canonicalJson({ z: 1, a: 1, m: 1 })).toBe('{"a":1,"m":1,"z":1}');
  });
  test('no whitespace between separators', () => {
    expect(canonicalJson({ a: 1, b: 'two' })).toBe('{"a":1,"b":"two"}');
  });
  test('nested objects recurse + sort', () => {
    expect(canonicalJson({ b: { y: 1, x: 2 }, a: 0 })).toBe('{"a":0,"b":{"x":2,"y":1}}');
  });
  test('undefined values are dropped (matches JSON.stringify)', () => {
    expect(canonicalJson({ a: 1, b: undefined, c: 2 })).toBe('{"a":1,"c":2}');
  });
  test('object with mixed values', () => {
    expect(canonicalJson({ name: 'x', tags: ['a', 'b'], n: 0, ok: true, none: null })).toBe(
      '{"n":0,"name":"x","none":null,"ok":true,"tags":["a","b"]}',
    );
  });
});

describe('canonicalJson — Date', () => {
  test('serialises to ISO string', () => {
    const d = new Date('2026-01-15T12:34:56.789Z');
    expect(canonicalJson(d)).toBe('"2026-01-15T12:34:56.789Z"');
  });
});

describe('canonicalJson — determinism', () => {
  test('same input produces byte-identical output across calls', () => {
    const input = { tags: ['x', 'y'], a: 1, nested: { z: true, b: null } };
    const a = canonicalJson(input);
    const b = canonicalJson(input);
    expect(a).toBe(b);
  });
  test('two different orderings of the same object produce the same string', () => {
    const a = canonicalJson({ a: 1, b: 2, c: 3 });
    const b = canonicalJson({ c: 3, b: 2, a: 1 });
    const c = canonicalJson({ b: 2, c: 3, a: 1 });
    expect(a).toBe(b);
    expect(b).toBe(c);
  });
});

describe('sha256Hex', () => {
  test('returns 64-char lowercase hex for "hello"', async () => {
    // sha256("hello") matches python's hashlib.sha256(b"hello").hexdigest()
    const h = await sha256Hex('hello');
    expect(h).toBe('2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824');
    expect(h.length).toBe(64);
  });

  test('returns canonical hex for empty string', async () => {
    const h = await sha256Hex('');
    expect(h).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  });

  test('different inputs produce different hashes', async () => {
    const a = await sha256Hex('a');
    const b = await sha256Hex('b');
    expect(a).not.toBe(b);
  });
});
