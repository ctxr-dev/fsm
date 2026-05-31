/**
 * Tests for lib/urlState.ts: buildSchema, useUrlState, codecs.
 *
 * Coverage:
 *   - Codecs round-trip for string / number / boolean / csv.
 *   - buildSchema fromQuery + toQuery round-trip.
 *   - useUrlState hydrates the signal from window.location.search on mount.
 *   - useUrlState reacts to popstate.
 *   - useUrlState writes back to URL (debounced).
 *   - Empty state produces no query string.
 */

import { describe, expect, test, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/preact';

import { buildSchema, codecs, useUrlState } from '../urlState';

describe('codecs', () => {
  test('string', () => {
    expect(codecs.string.parse('hello')).toBe('hello');
    expect(codecs.string.parse(null)).toBeUndefined();
    expect(codecs.string.parse('')).toBe('');
    expect(codecs.string.serialise('hello')).toBe('hello');
    expect(codecs.string.serialise('')).toBeUndefined();
    expect(codecs.string.serialise(undefined)).toBeUndefined();
    expect(codecs.string.serialise(123)).toBeUndefined();
  });

  test('number', () => {
    expect(codecs.number.parse('42')).toBe(42);
    expect(codecs.number.parse('3.14')).toBe(3.14);
    expect(codecs.number.parse('abc')).toBeUndefined();
    expect(codecs.number.parse(null)).toBeUndefined();
    expect(codecs.number.serialise(42)).toBe('42');
    expect(codecs.number.serialise(NaN)).toBeUndefined();
    expect(codecs.number.serialise(Infinity)).toBeUndefined();
    expect(codecs.number.serialise('42')).toBeUndefined();
  });

  test('boolean', () => {
    expect(codecs.boolean.parse('1')).toBe(true);
    expect(codecs.boolean.parse('true')).toBe(true);
    expect(codecs.boolean.parse('0')).toBe(false);
    expect(codecs.boolean.parse('false')).toBe(false);
    expect(codecs.boolean.parse(null)).toBeUndefined();
    expect(codecs.boolean.serialise(true)).toBe('1');
    expect(codecs.boolean.serialise(false)).toBeUndefined();
    expect(codecs.boolean.serialise('true')).toBeUndefined();
  });

  test('csv', () => {
    expect(codecs.csv.parse('a,b,c')).toEqual(['a', 'b', 'c']);
    expect(codecs.csv.parse(' a , b ')).toEqual(['a', 'b']);
    expect(codecs.csv.parse('')).toBeUndefined();
    expect(codecs.csv.parse(null)).toBeUndefined();
    expect(codecs.csv.serialise(['a', 'b'])).toBe('a,b');
    expect(codecs.csv.serialise([])).toBeUndefined();
    expect(codecs.csv.serialise('not array')).toBeUndefined();
  });
});

describe('buildSchema', () => {
  interface S {
    tab: string;
    offset: number;
    flag: boolean;
    kinds?: string[];
  }
  const initial: S = { tab: 'a', offset: 0, flag: false };
  const schema = buildSchema<S>(initial, {
    tab: codecs.string,
    offset: codecs.number,
    flag: codecs.boolean,
    kinds: codecs.csv,
  });

  test('fromQuery: empty params → initial', () => {
    expect(schema.fromQuery(new URLSearchParams())).toEqual(initial);
  });

  test('fromQuery: partial params → initial + provided keys', () => {
    const got = schema.fromQuery(new URLSearchParams('tab=details&offset=20'));
    expect(got).toEqual({ tab: 'details', offset: 20, flag: false });
  });

  test('fromQuery: malformed values fall back to initial for that field', () => {
    const got = schema.fromQuery(new URLSearchParams('offset=NaN'));
    expect(got.offset).toBe(0);
  });

  test('toQuery: defaults produce empty string', () => {
    expect(schema.toQuery({ tab: '', offset: NaN, flag: false } as S)).toBe('');
  });

  test('toQuery: non-trivially-serialisable values emitted', () => {
    // offset=0 IS serialised (0 is a valid number); empty string + false
    // + NaN + [] are all dropped by their codecs.
    expect(schema.toQuery({ tab: 'x', offset: 0, flag: true })).toBe('?tab=x&offset=0&flag=1');
  });

  test('round-trip: every populated field survives', () => {
    const state: S = { tab: 'events', offset: 40, flag: true, kinds: ['run_started', 'state_entered'] };
    const q = schema.toQuery(state);
    const params = new URLSearchParams(q.replace(/^\?/, ''));
    const back = schema.fromQuery(params);
    expect(back).toEqual({ ...initial, ...state });
  });
});

describe('useUrlState', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
  });
  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  interface S {
    name: string;
    n: number;
  }
  const initial: S = { name: '', n: 0 };
  const schema = buildSchema<S>(initial, {
    name: codecs.string,
    n: codecs.number,
  });

  test('hydrates from window.location.search on mount', () => {
    window.history.replaceState(null, '', '/?name=alpha&n=7');
    const { result } = renderHook(() => useUrlState(schema, initial));
    expect(result.current.value).toEqual({ name: 'alpha', n: 7 });
  });

  test('starts from initial when no query params', () => {
    const { result } = renderHook(() => useUrlState(schema, initial));
    expect(result.current.value).toEqual(initial);
  });

  test('writes back to URL after mutation (debounced)', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useUrlState(schema, initial));
    act(() => {
      result.current.value = { name: 'beta', n: 3 };
    });
    expect(window.location.search).toBe('');
    vi.advanceTimersByTime(60);
    expect(window.location.search).toBe('?name=beta&n=3');
    vi.useRealTimers();
  });

  test('responds to popstate', () => {
    const { result } = renderHook(() => useUrlState(schema, initial));
    expect(result.current.value).toEqual(initial);
    window.history.replaceState(null, '', '/?name=gamma');
    act(() => {
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    expect(result.current.value).toEqual({ name: 'gamma', n: 0 });
  });
});
