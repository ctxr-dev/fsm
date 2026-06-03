/**
 * Tests for lib/useDebouncedRefetch.ts.
 *
 * Coverage:
 *   - Idle window: bursty triggers collapse to one fire 200ms after the
 *     LAST trigger.
 *   - Max-wait safety net: sustained triggers (every 100ms) still fire
 *     at the 1000ms mark.
 *   - Unmount cancels a pending fire.
 *   - flush() fires immediately and clears timers (so the wait timer
 *     does not double-fire afterwards).
 *   - cancel() drops the pending fire without invoking fn.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { renderHook } from '@testing-library/preact';

import { useDebouncedRefetch } from '../useDebouncedRefetch';

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useDebouncedRefetch', () => {
  test('fires once per 200ms idle window', () => {
    const fn = vi.fn();
    const { result } = renderHook(() => useDebouncedRefetch(fn));

    result.current.trigger();
    result.current.trigger();
    result.current.trigger();
    expect(fn).not.toHaveBeenCalled();

    // Just before the window expires: still nothing.
    vi.advanceTimersByTime(199);
    expect(fn).not.toHaveBeenCalled();

    // At the 200ms mark: a single fire for the whole burst.
    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  test('every new trigger resets the wait window', () => {
    const fn = vi.fn();
    const { result } = renderHook(() => useDebouncedRefetch(fn));

    result.current.trigger();
    vi.advanceTimersByTime(150);
    result.current.trigger();
    vi.advanceTimersByTime(150);
    // 300ms elapsed total, but never 200ms of idleness — still no fire.
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(200);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  test('max-wait 1000ms forces a fire even under a sustained 100ms-cadence burst', () => {
    const fn = vi.fn();
    const { result } = renderHook(() => useDebouncedRefetch(fn));

    // 9 triggers spaced 100ms apart — at t=900ms total. The wait-timer
    // never gets to expire because each trigger arrives before 200ms
    // of idleness; but the maxWait safety net (armed at t=0) must fire
    // at t=1000.
    for (let i = 0; i < 9; i += 1) {
      result.current.trigger();
      vi.advanceTimersByTime(100);
    }
    expect(fn).not.toHaveBeenCalled();

    // Advance past the 1000ms ceiling counted from the first trigger.
    vi.advanceTimersByTime(100);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  test('unmount cancels a pending fire', () => {
    const fn = vi.fn();
    const { result, unmount } = renderHook(() => useDebouncedRefetch(fn));

    result.current.trigger();
    unmount();
    vi.advanceTimersByTime(5000);
    expect(fn).not.toHaveBeenCalled();
  });

  test('flush() fires immediately and clears timers (no double fire)', () => {
    const fn = vi.fn();
    const { result } = renderHook(() => useDebouncedRefetch(fn));

    result.current.trigger();
    result.current.flush();
    expect(fn).toHaveBeenCalledTimes(1);

    // The wait timer must have been cleared — advancing past 200ms
    // would otherwise produce a second fire.
    vi.advanceTimersByTime(1500);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  test('flush() with nothing pending is a no-op', () => {
    const fn = vi.fn();
    const { result } = renderHook(() => useDebouncedRefetch(fn));

    result.current.flush();
    expect(fn).not.toHaveBeenCalled();
  });

  test('cancel() drops the pending fire without invoking fn', () => {
    const fn = vi.fn();
    const { result } = renderHook(() => useDebouncedRefetch(fn));

    result.current.trigger();
    result.current.cancel();
    vi.advanceTimersByTime(2000);
    expect(fn).not.toHaveBeenCalled();
  });

  test('honours custom wait + maxWait options', () => {
    const fn = vi.fn();
    const { result } = renderHook(() =>
      useDebouncedRefetch(fn, { wait: 50, maxWait: 300 }),
    );

    result.current.trigger();
    vi.advanceTimersByTime(49);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
