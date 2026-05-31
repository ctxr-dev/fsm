/**
 * Tests for lib/fuzzy.ts: scoreOne + rankFuzzy.
 *
 * Properties verified:
 *   - Empty query → score 0, all matches.
 *   - All-chars-must-appear-in-order rule.
 *   - Prefix bonus applies only to leading match.
 *   - Word-boundary bonus on `_`, `-`, ` `, `/`, `.`, `:` predecessors.
 *   - CamelHump bonus on uppercase matches.
 *   - Gap penalty: more contiguous = higher score.
 *   - Length tiebreaker prefers shorter strings.
 *   - rankFuzzy excludes non-matches + applies weight.
 */

import { describe, expect, test } from 'vitest';

import { rankFuzzy, scoreOne } from '../fuzzy';

describe('scoreOne', () => {
  test('empty query → score 0, empty matches', () => {
    expect(scoreOne('foo', '')).toEqual({ score: 0, matches: [] });
  });

  test('returns null when characters do not appear in order', () => {
    expect(scoreOne('abc', 'cab')).toBeNull();
    expect(scoreOne('hello', 'world')).toBeNull();
  });

  test('returns matches when query is a prefix', () => {
    const r = scoreOne('hello', 'he');
    expect(r).not.toBeNull();
    expect(r!.matches).toEqual([0, 1]);
  });

  test('case-insensitive matching', () => {
    expect(scoreOne('Hello', 'hl')).not.toBeNull();
  });

  test('contiguous matches outscore split matches', () => {
    const contig = scoreOne('abcdef', 'abc')!;
    const split = scoreOne('axbycz', 'abc')!;
    expect(contig.score).toBeGreaterThan(split.score);
  });

  test('prefix match outscores middle match of same length', () => {
    const prefix = scoreOne('foo', 'fo')!;
    const middle = scoreOne('xfoo', 'fo')!;
    expect(prefix.score).toBeGreaterThan(middle.score);
  });

  test('word-boundary chars boost match', () => {
    const wb = scoreOne('foo_bar', 'b')!;
    const inner = scoreOne('foobar', 'b')!;
    expect(wb.score).toBeGreaterThan(inner.score);
  });

  test('camelHump boost for uppercase match', () => {
    const ch = scoreOne('fooBar', 'B')!;
    const lc = scoreOne('foobar', 'b')!;
    expect(ch.score).toBeGreaterThan(lc.score);
  });

  test('shorter strings win tiebreaker', () => {
    const short = scoreOne('ab', 'ab')!;
    const long = scoreOne('abc', 'ab')!;
    expect(short.score).toBeGreaterThan(long.score);
  });

  test('matches array tracks the matched indices', () => {
    const r = scoreOne('hello world', 'how')!;
    expect(r.matches).toEqual([0, 4, 6]);
  });
});

describe('rankFuzzy', () => {
  test('empty query returns all items in original order, score 0', () => {
    const items = ['c', 'a', 'b'];
    const hits = rankFuzzy(items, '', { text: (x) => x });
    expect(hits.map((h) => h.item)).toEqual(['c', 'a', 'b']);
    expect(hits.every((h) => h.score === 0)).toBe(true);
  });

  test('excludes non-matches', () => {
    const items = ['abc', 'def', 'ghi'];
    const hits = rankFuzzy(items, 'a', { text: (x) => x });
    expect(hits.length).toBe(1);
    expect(hits[0].item).toBe('abc');
  });

  test('ranks by score descending', () => {
    const items = ['abcdef', 'azbzcz'];
    const hits = rankFuzzy(items, 'abc', { text: (x) => x });
    expect(hits[0].item).toBe('abcdef'); // contiguous
    expect(hits[1].item).toBe('azbzcz');
  });

  test('weight multiplies the score', () => {
    const items: { name: string; tier: number }[] = [
      { name: 'lowprio', tier: 1 },
      { name: 'lowprio2', tier: 5 }, // higher weight, same query
    ];
    const hits = rankFuzzy(items, 'low', {
      text: (i) => i.name,
      weight: (i) => i.tier,
    });
    expect(hits[0].item.name).toBe('lowprio2');
  });

  test('respects all query characters appear in order across items', () => {
    const items = ['runs', 'topology', 'specs'];
    const hits = rankFuzzy(items, 'topo', { text: (x) => x });
    expect(hits.map((h) => h.item)).toEqual(['topology']);
  });
});
