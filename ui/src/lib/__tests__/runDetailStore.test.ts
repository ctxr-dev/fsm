/**
 * Tests for lib/runDetailStore.ts.
 */

import { beforeEach, describe, expect, test } from 'vitest';

import {
  clearAllFilters,
  clearFilter,
  eventPassesFilters,
  filtersToChips,
  runDetailFilters,
  setFilter,
  signaturePassesFilters,
  toggleFilter,
  toolCallPassesFilters,
} from '../runDetailStore';

beforeEach(() => {
  clearAllFilters();
});

describe('runDetailFilters mutators', () => {
  test('toggleFilter sets a key', () => {
    toggleFilter('stateId', 'emit');
    expect(runDetailFilters.value).toEqual({ stateId: 'emit' });
  });

  test('toggleFilter twice with same value clears the key', () => {
    toggleFilter('stateId', 'emit');
    toggleFilter('stateId', 'emit');
    expect(runDetailFilters.value).toEqual({});
  });

  test('toggleFilter with different value replaces', () => {
    toggleFilter('stateId', 'emit');
    toggleFilter('stateId', 'done');
    expect(runDetailFilters.value).toEqual({ stateId: 'done' });
  });

  test('setFilter undefined clears the key', () => {
    setFilter('stateId', 'emit');
    setFilter('stateId', undefined);
    expect(runDetailFilters.value).toEqual({});
  });

  test('clearFilter removes the key', () => {
    setFilter('stateId', 'emit');
    setFilter('eventKind', 'run_started');
    clearFilter('stateId');
    expect(runDetailFilters.value).toEqual({ eventKind: 'run_started' });
  });

  test('clearAllFilters wipes everything', () => {
    setFilter('stateId', 'emit');
    setFilter('eventKind', 'k');
    clearAllFilters();
    expect(runDetailFilters.value).toEqual({});
  });
});

describe('filtersToChips', () => {
  test('empty filters produce empty chips', () => {
    expect(filtersToChips({})).toEqual([]);
  });

  test('renders one chip per non-empty key', () => {
    const chips = filtersToChips({
      stateId: 'emit',
      eventKind: 'run_started',
      toolName: 'Read',
    });
    expect(chips.length).toBe(3);
    expect(chips.map((c) => c.kind).sort()).toEqual(['event', 'state', 'tool']);
  });

  test('chip ids are stable + namespaced', () => {
    const chips = filtersToChips({ stateId: 'x' });
    expect(chips[0].id).toBe('state:x');
  });

  test('producer label is shortened', () => {
    const chips = filtersToChips({ producerId: 'abcdef0123456789' });
    expect(chips[0].label).toContain('abcdef012345');
  });
});

describe('eventPassesFilters', () => {
  test('empty filters → always passes', () => {
    expect(eventPassesFilters({ kind: 'k' }, {})).toBe(true);
  });

  test('kind mismatch → fails', () => {
    expect(eventPassesFilters({ kind: 'k1' }, { eventKind: 'k2' })).toBe(false);
  });

  test('kind match → passes', () => {
    expect(eventPassesFilters({ kind: 'k1' }, { eventKind: 'k1' })).toBe(true);
  });

  test('producerId mismatch → fails', () => {
    expect(eventPassesFilters({ producer_id: 'p1' }, { producerId: 'p2' })).toBe(false);
  });

  test('stateId matches against state_id in payload', () => {
    expect(eventPassesFilters({ payload: { state_id: 'emit' } }, { stateId: 'emit' })).toBe(true);
  });

  test('stateId matches against from_state in payload', () => {
    expect(eventPassesFilters({ payload: { from_state: 'emit' } }, { stateId: 'emit' })).toBe(true);
  });

  test('stateId matches against to_state in payload', () => {
    expect(eventPassesFilters({ payload: { to_state: 'done' } }, { stateId: 'done' })).toBe(true);
  });

  test('stateId fails when neither field matches', () => {
    expect(eventPassesFilters({ payload: {} }, { stateId: 'emit' })).toBe(false);
  });

  test('AND-composes multiple filter keys', () => {
    expect(
      eventPassesFilters(
        { kind: 'k', producer_id: 'p1', payload: { state_id: 'emit' } },
        { eventKind: 'k', producerId: 'p1', stateId: 'emit' },
      ),
    ).toBe(true);
    expect(
      eventPassesFilters(
        { kind: 'k', producer_id: 'p1', payload: { state_id: 'emit' } },
        { eventKind: 'k', producerId: 'p2' }, // producer mismatch
      ),
    ).toBe(false);
  });
});

describe('toolCallPassesFilters', () => {
  test('toolName filter excludes mismatches', () => {
    expect(toolCallPassesFilters({ tool_name: 'Read' }, { toolName: 'Write' })).toBe(false);
  });

  test('toolName filter accepts matches', () => {
    expect(toolCallPassesFilters({ tool_name: 'Read' }, { toolName: 'Read' })).toBe(true);
  });

  test('producerId filter narrows', () => {
    expect(toolCallPassesFilters({ producer_id: 'p' }, { producerId: 'q' })).toBe(false);
  });
});

describe('signaturePassesFilters', () => {
  test('stateId filter excludes mismatches', () => {
    expect(signaturePassesFilters({ state_id: 'emit' }, { stateId: 'done' })).toBe(false);
  });

  test('empty filter passes everything', () => {
    expect(signaturePassesFilters({ state_id: 'emit' }, {})).toBe(true);
  });
});
