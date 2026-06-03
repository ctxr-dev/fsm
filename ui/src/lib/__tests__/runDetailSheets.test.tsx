/**
 * Tests for lib/runDetailSheets.ts.
 *
 * Coverage:
 *   - opener idempotency: opening the same admin sheet twice leaves the
 *     stack at length 1 (the second call is a no-op).
 *   - distinct openers stack correctly: admin then state then edge
 *     yields a stack of length 3 in that order.
 *   - URL fragment: rendering the SheetHost while the openers are used
 *     mirrors the TOP sheet's id into ``?sheet=...`` on
 *     ``window.location``.
 */

import { afterEach, beforeEach, describe, expect, test } from 'vitest';
import { render, cleanup } from '@testing-library/preact';

import {
  adminSheetId,
  edgeSheetId,
  openAdminSheet,
  openEdgeSheet,
  openStateEntrySheet,
  stateEntrySheetId,
} from '../runDetailSheets';
import { sheetStack } from '../store';
import { SheetHost } from '../../chrome/SheetHost';

beforeEach(() => {
  sheetStack.value = [];
  // Reset URL so query-param assertions start clean.
  window.history.replaceState({}, '', 'http://localhost/');
});

afterEach(() => {
  cleanup();
  sheetStack.value = [];
  window.history.replaceState({}, '', 'http://localhost/');
});

describe('runDetailSheets opener idempotency', () => {
  test('opening the same admin sheet twice does not duplicate the entry', () => {
    expect(sheetStack.value).toEqual([]);
    openAdminSheet({ runId: 'run-1' });
    openAdminSheet({ runId: 'run-1' });
    expect(sheetStack.value.length).toBe(1);
    expect(sheetStack.value[0].id).toBe(adminSheetId('run-1'));
  });

  test('opening the same state-entry sheet twice does not duplicate', () => {
    openStateEntrySheet({ entryId: 'evt-42', runId: 'run-1' });
    openStateEntrySheet({ entryId: 'evt-42', runId: 'run-1' });
    expect(sheetStack.value.length).toBe(1);
    expect(sheetStack.value[0].id).toBe(stateEntrySheetId('evt-42'));
  });

  test('opening the same edge sheet twice does not duplicate', () => {
    openEdgeSheet({ runId: 'run-1', fromStateId: 'a', toStateId: 'b' });
    openEdgeSheet({ runId: 'run-1', fromStateId: 'a', toStateId: 'b' });
    expect(sheetStack.value.length).toBe(1);
    expect(sheetStack.value[0].id).toBe(edgeSheetId('a', 'b'));
  });

  test('reverse-direction edge is a DIFFERENT sheet', () => {
    openEdgeSheet({ runId: 'run-1', fromStateId: 'a', toStateId: 'b' });
    openEdgeSheet({ runId: 'run-1', fromStateId: 'b', toStateId: 'a' });
    expect(sheetStack.value.length).toBe(2);
  });

  test('opener returns the resolved sheet id', () => {
    const id = openAdminSheet({ runId: 'run-9' });
    expect(id).toBe(adminSheetId('run-9'));
  });
});

describe('runDetailSheets stacking', () => {
  test('admin then state then edge stack to length 3 in order', () => {
    openAdminSheet({ runId: 'run-1' });
    openStateEntrySheet({ entryId: 'evt-42', runId: 'run-1' });
    openEdgeSheet({ runId: 'run-1', fromStateId: 'a', toStateId: 'b' });
    expect(sheetStack.value.length).toBe(3);
    expect(sheetStack.value.map((e) => e.id)).toEqual([
      adminSheetId('run-1'),
      stateEntrySheetId('evt-42'),
      edgeSheetId('a', 'b'),
    ]);
  });
});

describe('runDetailSheets URL fragment mirroring (via SheetHost)', () => {
  test('TOP sheet id appears in ?sheet= when SheetHost is mounted', () => {
    openAdminSheet({ runId: 'run-1' });
    render(<SheetHost />);
    const url = new URL(window.location.href);
    expect(url.searchParams.get('sheet')).toBe(adminSheetId('run-1'));
  });

  test('mirrors the TOP sheet id when multiple sheets are stacked', () => {
    openAdminSheet({ runId: 'run-1' });
    openStateEntrySheet({ entryId: 'evt-42', runId: 'run-1' });
    render(<SheetHost />);
    const url = new URL(window.location.href);
    expect(url.searchParams.get('sheet')).toBe(stateEntrySheetId('evt-42'));
  });
});
