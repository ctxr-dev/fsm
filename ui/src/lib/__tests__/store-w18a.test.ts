/**
 * Tests for the W18a extensions to lib/store.ts:
 *   - sheet stack mutators (openSheet, closeTopSheet, closeSheet, clearSheets)
 *   - recents (rememberRun, rememberSpec, rememberQuery) + cap
 *   - notifications (pushNotification + cap + markAllNotificationsRead)
 *   - persistence (loadStoredPrefs + wirePrefsPersistence + serialise/deserialise)
 *
 * These tests use the global store signals directly. afterEach resets
 * every signal we touch so tests don't leak state.
 */

import { describe, expect, test, beforeEach, afterEach, vi } from 'vitest';

import {
  NOTIFICATIONS_CAP,
  RECENT_CAP,
  _clearStoredPrefs,
  _resetPersistenceWiring,
  clearSheets,
  closeSheet,
  closeTopSheet,
  densityMode,
  loadStoredPrefs,
  markAllNotificationsRead,
  notifications,
  openSheet,
  pushNotification,
  recentQueries,
  recentRuns,
  recentSpecs,
  rememberQuery,
  rememberRun,
  rememberSpec,
  sheetStack,
  theme,
  urlStateEnabled,
  wirePrefsPersistence,
} from '../store';

beforeEach(() => {
  // Reset signals to defaults.
  sheetStack.value = [];
  recentRuns.value = [];
  recentSpecs.value = [];
  recentQueries.value = [];
  notifications.value = [];
  densityMode.value = 'comfortable';
  theme.value = 'auto';
  urlStateEnabled.value = true;
  _clearStoredPrefs();
  _resetPersistenceWiring();
});

afterEach(() => {
  // Tidy localStorage so the next test starts clean.
  _clearStoredPrefs();
  _resetPersistenceWiring();
});

describe('sheet stack', () => {
  test('openSheet appends an entry', () => {
    expect(sheetStack.value).toEqual([]);
    openSheet({ id: 'a', title: 'A', content: null });
    expect(sheetStack.value.length).toBe(1);
    expect(sheetStack.value[0].id).toBe('a');
  });

  test('multiple openSheet calls stack in order', () => {
    openSheet({ id: 'a', title: 'A', content: null });
    openSheet({ id: 'b', title: 'B', content: null });
    openSheet({ id: 'c', title: 'C', content: null });
    expect(sheetStack.value.map((e) => e.id)).toEqual(['a', 'b', 'c']);
  });

  test('closeTopSheet pops the rightmost', () => {
    openSheet({ id: 'a', title: 'A', content: null });
    openSheet({ id: 'b', title: 'B', content: null });
    closeTopSheet();
    expect(sheetStack.value.map((e) => e.id)).toEqual(['a']);
  });

  test('closeTopSheet on empty stack is a no-op', () => {
    expect(() => closeTopSheet()).not.toThrow();
    expect(sheetStack.value).toEqual([]);
  });

  test('closeTopSheet calls onClose hook BEFORE popping', () => {
    const order: string[] = [];
    openSheet({
      id: 'a',
      title: 'A',
      content: null,
      onClose: () => order.push('onClose'),
    });
    closeTopSheet();
    expect(order).toEqual(['onClose']);
    expect(sheetStack.value).toEqual([]);
  });

  test('closeSheet by id removes a non-top entry without disturbing siblings', () => {
    openSheet({ id: 'a', title: 'A', content: null });
    openSheet({ id: 'b', title: 'B', content: null });
    openSheet({ id: 'c', title: 'C', content: null });
    closeSheet('b');
    expect(sheetStack.value.map((e) => e.id)).toEqual(['a', 'c']);
  });

  test('closeSheet with unknown id is a no-op', () => {
    openSheet({ id: 'a', title: 'A', content: null });
    closeSheet('nope');
    expect(sheetStack.value.map((e) => e.id)).toEqual(['a']);
  });

  test('clearSheets closes all with onClose hooks fired', () => {
    const closed: string[] = [];
    openSheet({ id: 'a', title: 'A', content: null, onClose: () => closed.push('a') });
    openSheet({ id: 'b', title: 'B', content: null, onClose: () => closed.push('b') });
    clearSheets();
    expect(sheetStack.value).toEqual([]);
    expect(closed.sort()).toEqual(['a', 'b']);
  });
});

describe('recents', () => {
  test('rememberRun adds + dedupes by id', () => {
    rememberRun({ id: '1', status: 'completed', lastSeenAt: '2026-01-01T00:00:00Z' });
    rememberRun({ id: '2', status: 'paused', lastSeenAt: '2026-01-02T00:00:00Z' });
    rememberRun({ id: '1', status: 'in_progress', lastSeenAt: '2026-01-03T00:00:00Z' });
    expect(recentRuns.value).toHaveLength(2);
    // Most recent first.
    expect(recentRuns.value[0].id).toBe('1');
    expect(recentRuns.value[0].status).toBe('in_progress');
    expect(recentRuns.value[1].id).toBe('2');
  });

  test('rememberRun caps at RECENT_CAP', () => {
    for (let i = 0; i < RECENT_CAP + 10; i++) {
      rememberRun({ id: `r${i}`, status: 'completed', lastSeenAt: `2026-01-${(i % 28) + 1}T00:00:00Z` });
    }
    expect(recentRuns.value).toHaveLength(RECENT_CAP);
    // Most recent first → newest id at index 0.
    expect(recentRuns.value[0].id).toBe(`r${RECENT_CAP + 9}`);
  });

  test('rememberSpec dedupes by (slug, version) pair', () => {
    rememberSpec({ slug: 'a', version: 1, lastSeenAt: 't1' });
    rememberSpec({ slug: 'a', version: 2, lastSeenAt: 't2' });
    rememberSpec({ slug: 'a', version: 1, lastSeenAt: 't3' });
    expect(recentSpecs.value).toHaveLength(2);
    expect(recentSpecs.value[0]).toEqual({ slug: 'a', version: 1, lastSeenAt: 't3' });
  });

  test('rememberQuery dedupes + caps + trims whitespace-only', () => {
    rememberQuery('alpha');
    rememberQuery('beta');
    rememberQuery('alpha');
    rememberQuery('   '); // ignored
    rememberQuery('');    // ignored
    expect(recentQueries.value).toEqual(['alpha', 'beta']);
  });
});

describe('notifications', () => {
  test('pushNotification prepends + caps at NOTIFICATIONS_CAP', () => {
    for (let i = 0; i < NOTIFICATIONS_CAP + 5; i++) {
      pushNotification({
        id: `n${i}`,
        kind: 'k',
        title: 'T',
        timestamp: `t${i}`,
        read: false,
      });
    }
    expect(notifications.value).toHaveLength(NOTIFICATIONS_CAP);
    // Newest first → last-pushed at index 0.
    expect(notifications.value[0].id).toBe(`n${NOTIFICATIONS_CAP + 4}`);
  });

  test('markAllNotificationsRead is idempotent', () => {
    pushNotification({ id: '1', kind: 'k', title: 'T', timestamp: 't', read: false });
    pushNotification({ id: '2', kind: 'k', title: 'T', timestamp: 't', read: true });
    const before = notifications.value;
    markAllNotificationsRead();
    expect(notifications.value.every((n) => n.read)).toBe(true);
    const ref = notifications.value;
    markAllNotificationsRead(); // second call should NOT allocate
    expect(notifications.value).toBe(ref);
    expect(before).not.toBe(notifications.value); // first call DID allocate
  });
});

describe('persistence', () => {
  test('loadStoredPrefs no-op when nothing stored', () => {
    expect(() => loadStoredPrefs()).not.toThrow();
    expect(theme.value).toBe('auto');
  });

  test('loadStoredPrefs hydrates valid stored prefs', () => {
    window.localStorage.setItem(
      'fsm-ui:prefs',
      JSON.stringify({
        densityMode: 'compact',
        theme: 'dark',
        urlStateEnabled: false,
        recentRuns: [{ id: 'r1', status: 'completed', lastSeenAt: 't1' }],
        recentSpecs: [{ slug: 's', version: 1, lastSeenAt: 't1' }],
        recentQueries: ['q1', 'q2'],
      }),
    );
    loadStoredPrefs();
    expect(densityMode.value).toBe('compact');
    expect(theme.value).toBe('dark');
    expect(urlStateEnabled.value).toBe(false);
    expect(recentRuns.value).toEqual([{ id: 'r1', status: 'completed', lastSeenAt: 't1' }]);
    expect(recentSpecs.value).toEqual([{ slug: 's', version: 1, lastSeenAt: 't1' }]);
    expect(recentQueries.value).toEqual(['q1', 'q2']);
  });

  test('loadStoredPrefs ignores invalid density / theme values', () => {
    window.localStorage.setItem(
      'fsm-ui:prefs',
      JSON.stringify({ densityMode: 'enormous', theme: 'rainbow' }),
    );
    loadStoredPrefs();
    expect(densityMode.value).toBe('comfortable');
    expect(theme.value).toBe('auto');
  });

  test('loadStoredPrefs ignores malformed JSON', () => {
    window.localStorage.setItem('fsm-ui:prefs', 'not json {{{');
    expect(() => loadStoredPrefs()).not.toThrow();
    expect(theme.value).toBe('auto');
  });

  test('loadStoredPrefs filters malformed list entries per-field', () => {
    window.localStorage.setItem(
      'fsm-ui:prefs',
      JSON.stringify({
        recentRuns: [
          { id: 'good', status: 'completed', lastSeenAt: 't1' },
          { broken: true },
          null,
        ],
        recentQueries: ['good', 42, null, 'also-good'],
      }),
    );
    loadStoredPrefs();
    expect(recentRuns.value).toEqual([{ id: 'good', status: 'completed', lastSeenAt: 't1' }]);
    expect(recentQueries.value).toEqual(['good', 'also-good']);
  });

  test('wirePrefsPersistence writes to localStorage on signal change (debounced)', async () => {
    vi.useFakeTimers();
    wirePrefsPersistence();
    theme.value = 'dark';
    densityMode.value = 'compact';
    expect(window.localStorage.getItem('fsm-ui:prefs')).toBeNull();
    vi.advanceTimersByTime(250);
    const stored = JSON.parse(window.localStorage.getItem('fsm-ui:prefs') ?? '{}');
    expect(stored.theme).toBe('dark');
    expect(stored.densityMode).toBe('compact');
    vi.useRealTimers();
  });

  test('wirePrefsPersistence is idempotent', () => {
    wirePrefsPersistence();
    // Second call should be a no-op; if it weren't, we'd register a second
    // effect and double-write. The flag-resetting _resetPersistenceWiring
    // (called by beforeEach) is the test-only escape hatch.
    expect(() => wirePrefsPersistence()).not.toThrow();
  });
});
