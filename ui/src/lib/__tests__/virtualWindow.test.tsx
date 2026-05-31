/**
 * Tests for lib/virtualWindow.ts: useWindow.
 *
 * Coverage:
 *   - Below threshold: range covers all rows, onScroll is a no-op.
 *   - Above threshold: initial range covers viewport-ish slice.
 *   - Above threshold: scroll updates range with overscan respected.
 *   - Spacer maths are correct at boundaries (start=0, end=totalRows).
 *   - Negative bottom spacer is clamped at 0.
 *   - Total height = rows * rowHeight.
 *   - initialScrollTop seeds the visible range.
 */

import { describe, expect, test } from 'vitest';
import { renderHook } from '@testing-library/preact';

import { useWindow, DEFAULT_VIRTUALISE_THRESHOLD } from '../virtualWindow';

function fakeScrollEvent(scrollTop: number, clientHeight: number): Event {
  const el = document.createElement('div');
  Object.defineProperty(el, 'scrollTop', { value: scrollTop, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true });
  const e = new Event('scroll');
  Object.defineProperty(e, 'currentTarget', { value: el, configurable: true });
  return e;
}

describe('useWindow', () => {
  test('below threshold: range covers all rows, no virtualisation', () => {
    const { result } = renderHook(() => useWindow(50, 20));
    expect(result.current.range).toEqual({ start: 0, end: 50 });
    expect(result.current.totalHeight).toBe(50 * 20);
    expect(result.current.topSpacerHeight).toBe(0);
    expect(result.current.bottomSpacerHeight).toBe(0);
  });

  test('below threshold: onScroll is a no-op (does not crash on missing target)', () => {
    const { result } = renderHook(() => useWindow(10, 20));
    // Calling onScroll without virtualisation MUST be safe; pass a real Event.
    expect(() => result.current.onScroll(new Event('scroll'))).not.toThrow();
    // Range is unchanged.
    expect(result.current.range).toEqual({ start: 0, end: 10 });
  });

  test('above threshold: initial range is windowed', () => {
    const totalRows = DEFAULT_VIRTUALISE_THRESHOLD + 100;
    const { result } = renderHook(() => useWindow(totalRows, 20));
    expect(result.current.range.start).toBe(0);
    expect(result.current.range.end).toBeLessThan(totalRows);
    expect(result.current.range.end).toBeGreaterThan(0);
  });

  test('above threshold: scroll updates range respecting overscan', () => {
    const totalRows = 5000;
    const rowHeight = 20;
    const overscan = 10;
    const { result, rerender } = renderHook(() => useWindow(totalRows, rowHeight, { overscan }));
    // Scroll to row ~100 (px = 100 * 20 = 2000). Viewport 400px = 20 rows.
    result.current.onScroll(fakeScrollEvent(2000, 400));
    rerender();
    const r = result.current.range;
    // start = floor(2000/20) - overscan = 100 - 10 = 90
    expect(r.start).toBe(90);
    // end = start + ceil(400/20) + overscan*2 = 90 + 20 + 20 = 130
    expect(r.end).toBe(130);
  });

  test('above threshold: end is clamped to totalRows', () => {
    const totalRows = 1100;
    const rowHeight = 20;
    const { result, rerender } = renderHook(() => useWindow(totalRows, rowHeight, { overscan: 5 }));
    // Scroll to near the bottom.
    result.current.onScroll(fakeScrollEvent(21500, 400));
    rerender();
    expect(result.current.range.end).toBe(totalRows);
    expect(result.current.range.start).toBeLessThanOrEqual(totalRows);
  });

  test('above threshold: start is clamped to 0', () => {
    const totalRows = 2000;
    const { result, rerender } = renderHook(() => useWindow(totalRows, 20, { overscan: 50 }));
    // Negative scrollTop would put start below 0 without clamping.
    result.current.onScroll(fakeScrollEvent(0, 400));
    rerender();
    expect(result.current.range.start).toBe(0);
  });

  test('spacer maths sum to totalHeight - visible-rows * rowHeight', () => {
    const totalRows = 2000;
    const rowHeight = 20;
    const { result, rerender } = renderHook(() => useWindow(totalRows, rowHeight, { overscan: 5 }));
    result.current.onScroll(fakeScrollEvent(400, 400));
    rerender();
    const r = result.current.range;
    expect(result.current.topSpacerHeight).toBe(r.start * rowHeight);
    expect(result.current.bottomSpacerHeight).toBe((totalRows - r.end) * rowHeight);
    expect(result.current.totalHeight).toBe(totalRows * rowHeight);
  });

  test('initialScrollTop seeds the initial range', () => {
    const totalRows = 2000;
    const rowHeight = 20;
    const { result } = renderHook(() =>
      useWindow(totalRows, rowHeight, { overscan: 10, initialScrollTop: 1000 }),
    );
    // floor(1000/20) - 10 = 50 - 10 = 40
    expect(result.current.range.start).toBe(40);
  });

  test('threshold override: when totalRows exceeds custom threshold', () => {
    const { result } = renderHook(() => useWindow(150, 20, { virtualiseThreshold: 100, overscan: 5 }));
    // With 150 > 100 threshold, virtualisation kicks in even on a small list.
    expect(result.current.range.end).toBeLessThan(150);
  });
});
