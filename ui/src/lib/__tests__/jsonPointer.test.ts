/**
 * RFC 6901 JSON Pointer tests.
 *
 * Edge cases covered:
 *   - Root pointer (`''`).
 *   - Escaping of `~` and `/` (order-sensitive: `~` first).
 *   - Round-tripping every escape combination.
 *   - Numeric indices (treated as string segments per RFC).
 *   - Invalid pointer (no leading `/`) throws.
 *   - Empty segments (a trailing `/` produces a `''` segment) — spec-allowed.
 */

import { describe, expect, test } from 'vitest';

import {
  ROOT_POINTER,
  escapeSegment,
  joinPointer,
  parsePointer,
  pointerForPath,
  unescapeSegment,
} from '../jsonPointer';

describe('escapeSegment', () => {
  test('passes through plain segments unchanged', () => {
    expect(escapeSegment('foo')).toBe('foo');
    expect(escapeSegment('')).toBe('');
    expect(escapeSegment('123abc')).toBe('123abc');
  });

  test('encodes ~ as ~0', () => {
    expect(escapeSegment('~')).toBe('~0');
    expect(escapeSegment('~tilde~')).toBe('~0tilde~0');
  });

  test('encodes / as ~1', () => {
    expect(escapeSegment('a/b')).toBe('a~1b');
    expect(escapeSegment('/')).toBe('~1');
  });

  test('encodes ~ BEFORE /, so a literal ~1 in input stays distinguishable', () => {
    expect(escapeSegment('~1')).toBe('~01');
    expect(escapeSegment('a~/b')).toBe('a~0~1b');
  });

  test('numeric inputs are coerced to string segments', () => {
    expect(escapeSegment(0)).toBe('0');
    expect(escapeSegment(42)).toBe('42');
  });
});

describe('unescapeSegment', () => {
  test('inverse of escapeSegment for every combo', () => {
    const samples = ['foo', '', 'a/b', '~', '~1', 'a~/b', 'x~0y', '123', '/', '~tilde~/slash~'];
    for (const s of samples) {
      expect(unescapeSegment(escapeSegment(s))).toBe(s);
    }
  });

  test('passes invalid escapes through verbatim', () => {
    // Unknown ~? sequences are spec-undefined; we choose forgiving passthrough.
    expect(unescapeSegment('~9')).toBe('~9');
    expect(unescapeSegment('a~')).toBe('a~'); // trailing lone ~
  });
});

describe('joinPointer', () => {
  test('joins root + segment', () => {
    expect(joinPointer(ROOT_POINTER, 'a')).toBe('/a');
    expect(joinPointer('', 'items')).toBe('/items');
  });

  test('joins nested pointer + segment', () => {
    expect(joinPointer('/a', 'b')).toBe('/a/b');
    expect(joinPointer('/items', 0)).toBe('/items/0');
    expect(joinPointer('/items/0', 'name')).toBe('/items/0/name');
  });

  test('escapes the new segment', () => {
    expect(joinPointer('', 'a/b')).toBe('/a~1b');
    expect(joinPointer('/x', '~y')).toBe('/x/~0y');
  });
});

describe('pointerForPath', () => {
  test('empty path produces root', () => {
    expect(pointerForPath([])).toBe(ROOT_POINTER);
    expect(pointerForPath([])).toBe('');
  });

  test('single segment', () => {
    expect(pointerForPath(['foo'])).toBe('/foo');
  });

  test('multiple segments', () => {
    expect(pointerForPath(['items', 0, 'name'])).toBe('/items/0/name');
  });

  test('escapes each segment independently', () => {
    expect(pointerForPath(['a/b', '~'])).toBe('/a~1b/~0');
  });
});

describe('parsePointer', () => {
  test('root returns empty array', () => {
    expect(parsePointer('')).toEqual([]);
  });

  test('single segment', () => {
    expect(parsePointer('/foo')).toEqual(['foo']);
  });

  test('nested segments', () => {
    expect(parsePointer('/a/b/c')).toEqual(['a', 'b', 'c']);
  });

  test('decodes escaped segments', () => {
    expect(parsePointer('/a~1b/~0c')).toEqual(['a/b', '~c']);
  });

  test('round-trips through pointerForPath', () => {
    const paths: (string | number)[][] = [
      [],
      ['a'],
      ['a', 'b'],
      ['items', 0, 'name'],
      ['a/b', '~tilde'],
      ['weird~/segment', 'plain'],
    ];
    for (const p of paths) {
      const ptr = pointerForPath(p);
      const back = parsePointer(ptr);
      // Coerce numbers to strings because parsePointer returns strings.
      expect(back).toEqual(p.map(String));
    }
  });

  test('throws TypeError on non-empty pointer missing leading /', () => {
    expect(() => parsePointer('foo')).toThrow(TypeError);
    expect(() => parsePointer('foo/bar')).toThrow(TypeError);
  });

  test('preserves empty segments (trailing /)', () => {
    expect(parsePointer('/a/')).toEqual(['a', '']);
    expect(parsePointer('/')).toEqual(['']);
  });
});
