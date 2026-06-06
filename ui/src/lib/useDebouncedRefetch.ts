/**
 * useDebouncedRefetch — coalesces high-frequency refetch triggers into a
 * single bounded-latency call to ``fn``.
 *
 * Why a hook (rather than a plain ``debounce(fn, 200)`` utility):
 *
 *   - The run-detail view subscribes to an SSE firehose and wants to
 *     refetch lists / topology / counters on every event burst. Naive
 *     debounce starves the user under a sustained burst because every
 *     new event resets the timer. We add a ``maxWait`` so the user
 *     sees fresh data at least once per second even when events keep
 *     arriving every 50ms.
 *   - The hook owns the timer ids in refs that are cleared on unmount
 *     so route changes don't leave a pending ``fn`` firing into an
 *     unmounted tree. A free function debounce would leak.
 *
 * Defaults — ``wait=200ms`` matches the prefs-persistence debounce
 * already used in ``store.ts`` (consistent perceived latency across the
 * dashboard). ``maxWait=1000ms`` is the upper bound a user notices as
 * "live" vs. "lagging".
 */

import { useEffect, useMemo, useRef } from 'preact/hooks';

export interface UseDebouncedRefetchOptions {
  /** Reset-on-trigger window in ms. Default 200. */
  wait?: number;
  /** Hard upper bound between the first queued trigger and a fire. Default 1000. */
  maxWait?: number;
}

export interface UseDebouncedRefetchHandle {
  /** Queue a refetch. Subsequent calls within ``wait`` reset the timer. */
  trigger: () => void;
  /** Fire ``fn`` immediately if anything is pending and clear timers. */
  flush: () => void;
  /** Drop any pending fire without invoking ``fn``. */
  cancel: () => void;
}

export function useDebouncedRefetch<T>(
  fn: () => Promise<T> | T | void,
  opts: UseDebouncedRefetchOptions = {},
): UseDebouncedRefetchHandle {
  const wait = opts.wait ?? 200;
  const maxWait = opts.maxWait ?? 1000;

  const waitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const maxTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const firstQueuedAt = useRef<number | null>(null);
  // Hold the latest ``fn`` in a ref so a stale closure from an earlier
  // render is never invoked — callers commonly pass a fresh arrow on
  // every render.
  const fnRef = useRef(fn);
  fnRef.current = fn;

  // Memoize the returned handle so consumers can safely include it in
  // an effect's dependency array without thrashing on every render. The
  // previous implementation returned a fresh `{ trigger, flush, cancel }`
  // object every call, which made every render churn the identity and
  // forced any dependent useEffect (e.g. runDetail's single SSE
  // subscription that depends on four refetchers) to tear down and
  // re-open. The closures themselves are stable because they only touch
  // refs (waitTimer / maxTimer / firstQueuedAt / fnRef) which carry
  // mutable state without re-creating the function identity.
  const handle = useMemo<UseDebouncedRefetchHandle>(() => {
    const clearTimers = (): void => {
      if (waitTimer.current != null) {
        clearTimeout(waitTimer.current);
        waitTimer.current = null;
      }
      if (maxTimer.current != null) {
        clearTimeout(maxTimer.current);
        maxTimer.current = null;
      }
      firstQueuedAt.current = null;
    };

    const fire = (): void => {
      clearTimers();
      void fnRef.current();
    };

    const trigger = (): void => {
      if (firstQueuedAt.current == null) {
        firstQueuedAt.current = Date.now();
        // Arm the maxWait safety net only on the first queued trigger of
        // a burst; subsequent triggers within the burst leave it alone.
        maxTimer.current = setTimeout(fire, maxWait);
      }
      if (waitTimer.current != null) clearTimeout(waitTimer.current);
      waitTimer.current = setTimeout(fire, wait);
    };

    const flush = (): void => {
      if (firstQueuedAt.current == null) {
        // Nothing pending — flush is a no-op so callers can safely call
        // it from cleanup paths without guarding.
        return;
      }
      fire();
    };

    const cancel = (): void => {
      clearTimers();
    };

    return { trigger, flush, cancel };
    // wait + maxWait are captured into the trigger closure; if a caller
    // mutates them mid-life we want the new values to take effect on the
    // next trigger. fnRef is mutable so the latest `fn` is always
    // dispatched without a re-memo.
  }, [wait, maxWait]);

  // Cleanup on unmount: drop any pending fire so the component cannot
  // refetch into a torn-down tree. The cancel closure is stable across
  // re-renders (handle is memoised), so we deliberately exclude it from
  // the deps array — this effect should run only on mount/unmount.
  useEffect(() => {
    return () => {
      handle.cancel();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount/unmount only
  }, []);

  return handle;
}
