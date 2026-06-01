/**
 * Tooltip — hover-or-focus surfaced overlay for terse, full-string content.
 *
 * Use when a label is visually truncated (FlowGraph state names,
 * predicate edge labels, etc.) and the operator must be able to read
 * the FULL value without taking an extra click. Click-to-open is a
 * Sheet's job; Tooltip is the at-a-glance preview.
 *
 * Contract (matches the W18 interaction grammar):
 *   - Open on mouseenter (after 400 ms delay) OR focus (instant —
 *     keyboard navigation is intentional, so a hover-style delay
 *     would feel sluggish).
 *   - Close on mouseleave (100 ms grace), blur, Escape, scroll, or
 *     pointerdown outside both trigger and bubble.
 *   - role="tooltip" on the floating element; trigger gains
 *     aria-describedby while visible.
 *   - Honours prefers-reduced-motion (drops the opacity transition).
 *   - Singleton: only one bubble is mounted at a time.
 *   - Warm window (300 ms after a close) lets the next hover open
 *     instantly so scanning a tree feels responsive.
 *   - Portals into #tooltip-root under document.body so the bubble
 *     escapes ReactFlow / Sheet / Card overflow:hidden ancestors.
 *
 * Performance: the bubble portal is mounted ONLY on first open. A
 * JsonViewer with 1000 Tooltip-wrapped rows costs 1000 inert wrapping
 * spans and ZERO portals until the user hovers.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';
import { createPortal } from 'preact/compat';
import type { ComponentChild, JSX, VNode } from 'preact';

// ---------------------------------------------------------------------------
// Shared module-level state (singleton open + warm window).
// ---------------------------------------------------------------------------

let nextTooltipId = 1;
let warmUntil = 0; // wall-clock ms after which the warm window expires
let closeAll: (() => void) | null = null; // installed by the currently-open instance

/**
 * Test-only helper: reset every piece of module-level singleton state
 * so a fresh test run can't be influenced by a stale warm window /
 * closeAll registration / id counter from the previous test. Call
 * from `beforeEach` (or `afterEach`) in any suite that renders
 * <Tooltip>.
 *
 * Out of the testing path this is a no-op tool — it doesn't affect
 * normal app behaviour, but exporting it documents the singleton
 * surface and keeps the test suite order-independent (Copilot finding
 * on PR #57: warm window could leak between tests).
 */
export function __resetTooltipStateForTests(): void {
  nextTooltipId = 1;
  warmUntil = 0;
  closeAll = null;
}

/**
 * Returns the singleton #tooltip-root portal target, creating it if
 * absent. The target lives under document.body so the bubble escapes
 * every overflow:hidden ancestor in the app.
 */
function ensurePortalTarget(): HTMLDivElement {
  let el = document.getElementById('tooltip-root') as HTMLDivElement | null;
  if (!el) {
    el = document.createElement('div');
    el.id = 'tooltip-root';
    document.body.appendChild(el);
  }
  return el;
}

// Placement contract is currently 'top' | 'bottom'. The bubble auto-
// flips between the two when the preferred side would clip the
// viewport. 'left' / 'right' / 'auto' are NOT supported in v1 — if a
// future caller needs side placement, computePosition() needs to grow
// the corresponding cases first. Keeping the type narrow prevents
// callers from passing values the layout engine silently ignores.
export type TooltipPlacement = 'top' | 'bottom';

export interface TooltipProps {
  /** The full-detail body shown when the tooltip surfaces. */
  content: ComponentChild;
  /** Open delay in ms applied to hover. Focus always opens instantly
   *  (keyboard navigation is intentional, so a hover-style delay would
   *  feel sluggish to keyboard users). Default 400. */
  delay?: number;
  /** Preferred side. The bubble auto-flips to the opposite side if the
   *  preferred side would clip the viewport. Default 'top'. */
  placement?: TooltipPlacement;
  /** When true, the trigger is wrapped verbatim with no listeners. */
  disabled?: boolean;
  /** Optional stable id for the bubble (used by aria-describedby). */
  id?: string;
  /** Extra Tailwind classes on the wrapping span. */
  className?: string;
  /** Single trigger child (any VNode). */
  children: VNode;
}

interface Position {
  top: number;
  left: number;
  flipped: boolean;
}

function computePosition(
  trigger: DOMRect,
  bubble: DOMRect,
  placement: TooltipPlacement,
): Position {
  const margin = 8;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let flipped = false;

  const placeTop = (): { top: number; left: number } => ({
    top: trigger.top - bubble.height - margin,
    left: trigger.left + trigger.width / 2 - bubble.width / 2,
  });
  const placeBottom = (): { top: number; left: number } => ({
    top: trigger.bottom + margin,
    left: trigger.left + trigger.width / 2 - bubble.width / 2,
  });

  const fitsVertically = (candidateTop: number): boolean =>
    candidateTop >= margin && candidateTop + bubble.height <= vh - margin;

  // Auto-flip is symmetric: if the preferred side clips EITHER the top
  // OR bottom edge AND the opposite side fits, we flip. Without this,
  // placement='bottom' near the page bottom would clamp the bubble
  // over the trigger instead of flipping above it.
  const preferred = placement === 'bottom' ? placeBottom() : placeTop();
  let top = preferred.top;
  let left = preferred.left;
  if (!fitsVertically(top)) {
    const alternate = placement === 'bottom' ? placeTop() : placeBottom();
    if (fitsVertically(alternate.top)) {
      top = alternate.top;
      left = alternate.left;
      flipped = true;
    }
  }

  // Final clamp so the bubble never punches outside the viewport even
  // when neither side fits cleanly (e.g. on a very short window).
  if (top < margin) top = margin;
  if (top + bubble.height > vh - margin) {
    top = Math.max(margin, vh - bubble.height - margin);
  }
  if (left < margin) left = margin;
  if (left + bubble.width > vw - margin) {
    left = Math.max(margin, vw - bubble.width - margin);
  }
  return { top, left, flipped };
}

/**
 * Tooltip component. Wraps a single trigger child in an inline-flex
 * span that owns the open/close listeners; renders the bubble in a
 * portal when open.
 */
export function Tooltip(props: TooltipProps): JSX.Element {
  const {
    content,
    delay = 400,
    placement = 'top',
    disabled = false,
    id: providedId,
    className,
    children,
  } = props;

  const triggerRef = useRef<HTMLSpanElement | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  const openTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  const restoreTitleRef = useRef<string | null>(null);

  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<Position | null>(null);

  const bubbleId = useRef<string>(
    providedId ?? `tt-${nextTooltipId++}`,
  ).current;

  const isEmpty =
    content === null ||
    content === undefined ||
    (typeof content === 'string' && content.trim().length === 0);

  const clearOpenTimer = (): void => {
    if (openTimer.current !== null) {
      window.clearTimeout(openTimer.current);
      openTimer.current = null;
    }
  };
  const clearCloseTimer = (): void => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  };

  const closeNow = useCallback((): void => {
    clearOpenTimer();
    clearCloseTimer();
    // closeNow fires from Escape (intentional dismiss), scroll, outside
    // pointerdown, and singleton-handoff (a new Tooltip taking over).
    // All of those need to restore any suppressed title so the wrapper
    // doesn't leak the stripped-while-open state, AND should seed the
    // warm window so the next hover (e.g. the new tooltip in the
    // singleton-handoff case) opens instantly. Escape's separate
    // `warmUntil = 0` reset in the keydown handler still wins for the
    // genuine "dismiss intent" path.
    restoreTitle();
    warmUntil = Date.now() + 300;
    setOpen(false);
  }, []);

  // The wrapping span ALMOST never has a native title= (callers attach
  // title to the inner trigger child, not to the Tooltip wrapper),
  // but if a future caller does add one via the className prop's
  // sibling attributes we strip it on open and restore on close so the
  // browser doesn't race our bubble with its native title popup.
  //
  // NOT SUPPORTED: we do NOT recursively suppress title= on the inner
  // children. Callers must avoid setting `title=` on the trigger child
  // when it is already wrapped in <Tooltip> — that would produce a
  // double bubble (instant native + delayed custom). The audit-strings
  // lint catches this in practice; the contract is documented at the
  // top of this file.
  const suppressTitle = (): void => {
    const el = triggerRef.current;
    if (!el) return;
    const t = el.getAttribute('title');
    if (t !== null) {
      restoreTitleRef.current = t;
      el.removeAttribute('title');
    }
  };
  const restoreTitle = (): void => {
    const el = triggerRef.current;
    if (el && restoreTitleRef.current !== null) {
      el.setAttribute('title', restoreTitleRef.current);
      restoreTitleRef.current = null;
    }
  };

  const scheduleOpen = useCallback((opts?: { instant?: boolean }): void => {
    if (disabled || isEmpty) return;
    clearOpenTimer();
    clearCloseTimer();
    // Singleton: kick the currently open instance closed before
    // showing this one.
    if (closeAll && closeAll !== closeNow) {
      const prev = closeAll;
      closeAll = null;
      prev();
    }
    const now = Date.now();
    // Focus path passes instant=true: keyboard navigation already
    // signals intent, so the hover-style delay is wrong there. Warm
    // window also produces delay=0 for the second-hover-within-300ms
    // path.
    const useDelay = opts?.instant === true || now < warmUntil ? 0 : delay;
    // Always route through setTimeout (even for delay=0) so the
    // Preact re-render is flushable by vi.advanceTimersByTime in
    // tests. Going synchronous on delay<=0 left the re-render
    // queued on an unflushable scheduler (rAF) which is fake-time-
    // unfriendly.
    openTimer.current = window.setTimeout(() => {
      openTimer.current = null;
      suppressTitle();
      setOpen(true);
    }, Math.max(0, useDelay));
  }, [disabled, isEmpty, delay, closeNow]);

  const scheduleClose = useCallback((): void => {
    clearOpenTimer();
    clearCloseTimer();
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      restoreTitle();
      setOpen(false);
      warmUntil = Date.now() + 300;
    }, 100);
  }, []);

  // Position the bubble after it mounts.
  useLayoutEffect(() => {
    if (!open) {
      setPosition(null);
      return;
    }
    const trig = triggerRef.current;
    const bub = bubbleRef.current;
    if (!trig || !bub) return;
    const place = (): void => {
      const t = trig.getBoundingClientRect();
      const b = bub.getBoundingClientRect();
      setPosition(computePosition(t, b, placement));
    };
    place();
  }, [open, placement, content]);

  // Window-level dismiss listeners while open.
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        closeNow();
        warmUntil = 0;
      }
    };
    const onScroll = (): void => closeNow();
    const onPointerDown = (e: PointerEvent): void => {
      const trig = triggerRef.current;
      const bub = bubbleRef.current;
      const t = e.target as Node | null;
      if (!t) return;
      if (trig && trig.contains(t)) return;
      if (bub && bub.contains(t)) return;
      closeNow();
    };
    const onResize = (): void => {
      // Re-measure rather than close.
      const trig = triggerRef.current;
      const bub = bubbleRef.current;
      if (!trig || !bub) return;
      setPosition(
        computePosition(
          trig.getBoundingClientRect(),
          bub.getBoundingClientRect(),
          placement,
        ),
      );
    };
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', onScroll, { capture: true, passive: true });
    document.addEventListener('pointerdown', onPointerDown, true);
    window.addEventListener('resize', onResize);
    closeAll = closeNow;
    return () => {
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onScroll, { capture: true });
      document.removeEventListener('pointerdown', onPointerDown, true);
      window.removeEventListener('resize', onResize);
      if (closeAll === closeNow) closeAll = null;
    };
  }, [open, closeNow, placement]);

  // Cleanup on unmount: clear timers, restore title, unset singleton.
  useEffect(() => {
    return () => {
      clearOpenTimer();
      clearCloseTimer();
      restoreTitle();
      if (closeAll === closeNow) closeAll = null;
    };
  }, [closeNow]);

  // Attach native focusin/focusout listeners (Preact's onFocus does NOT
  // bubble through descendants; we need the bubbling focusin/out events
  // so that focusing a nested button inside the trigger surfaces the
  // tooltip). Done in useEffect so the listeners are attached once per
  // mount and torn down on unmount.
  useEffect(() => {
    const el = triggerRef.current;
    if (!el) return undefined;
    const onEnter = (): void => scheduleOpen();
    const onLeave = (): void => scheduleClose();
    // Focus path opens IMMEDIATELY (delay forced to 0) — keyboard
    // navigation is an intentional act, so honouring the hover-style
    // 400 ms delay would feel sluggish to keyboard users. The W18
    // contract treats `delay` as hover intent specifically.
    const onFocusIn = (): void => scheduleOpen({ instant: true });
    const onFocusOut = (): void => scheduleClose();
    el.addEventListener('mouseenter', onEnter);
    el.addEventListener('mouseleave', onLeave);
    el.addEventListener('focusin', onFocusIn);
    el.addEventListener('focusout', onFocusOut);
    return () => {
      el.removeEventListener('mouseenter', onEnter);
      el.removeEventListener('mouseleave', onLeave);
      el.removeEventListener('focusin', onFocusIn);
      el.removeEventListener('focusout', onFocusOut);
    };
  }, [scheduleOpen, scheduleClose]);

  // Mirror aria-describedby onto EVERY focusable descendant of the
  // wrapper while open. aria-describedby does not propagate to
  // descendants, and the actual focus target can be ANY focusable
  // element inside the trigger (nested arbitrarily — `<span><div>
  // <button/></div></span>`), so setting it only on the first child
  // misses the real focus target whenever a layout wrapper sits
  // between the Tooltip and the focusable control. Querying for the
  // standard set (button / a[href] / input / select / textarea /
  // [tabindex]) covers every realistic trigger shape.
  useEffect(() => {
    if (!open) return undefined;
    const wrapper = triggerRef.current;
    if (!wrapper) return undefined;
    const focusables = Array.from(
      wrapper.querySelectorAll<HTMLElement>(
        'button, a[href], input, select, textarea, [tabindex]',
      ),
    );
    // Fallback: if the trigger child isn't itself focusable (e.g. a
    // plain text span) tag the first DOM child anyway so the wrapper
    // still announces something when an assistive tech traverses to
    // the visible label.
    if (focusables.length === 0) {
      const first = wrapper.firstElementChild as HTMLElement | null;
      if (first) focusables.push(first);
    }
    // Snapshot prior aria-describedby on each so we can restore on
    // close (or strip our id only when appending to an existing list).
    const prev = new Map<HTMLElement, string | null>();
    for (const el of focusables) {
      const cur = el.getAttribute('aria-describedby');
      prev.set(el, cur);
      el.setAttribute(
        'aria-describedby',
        cur ? `${cur} ${bubbleId}` : bubbleId,
      );
    }
    return () => {
      for (const el of focusables) {
        const original = prev.get(el) ?? null;
        const current = el.getAttribute('aria-describedby');
        if (original === null) {
          el.removeAttribute('aria-describedby');
        } else if (current && current.includes(bubbleId)) {
          const restored = current
            .split(/\s+/)
            .filter((id) => id !== bubbleId)
            .join(' ');
          if (restored) el.setAttribute('aria-describedby', restored);
          else el.removeAttribute('aria-describedby');
        }
      }
    };
  }, [open, bubbleId]);

  if (disabled || isEmpty) {
    return children;
  }

  const bubble = open
    ? createPortal(
        <div
          ref={bubbleRef}
          role="tooltip"
          id={bubbleId}
          class={[
            'fixed z-[60] pointer-events-none max-w-xs rounded-md px-2 py-1',
            'text-xs whitespace-pre-wrap break-words shadow-lg border',
            'bg-slate-100 text-slate-900 border-slate-200',
            'dark:bg-slate-800 dark:text-slate-100 dark:border-slate-700',
            'motion-safe:transition-opacity motion-safe:duration-100',
            'motion-reduce:transition-none',
          ].join(' ')}
          // eslint-disable-next-line react/forbid-dom-props -- positioning is computed from the trigger + bubble DOMRects on open and on resize; must be inline to update without a class explosion
          style={{
            top: `${position?.top ?? -9999}px`,
            left: `${position?.left ?? -9999}px`,
            opacity: position ? 1 : 0,
          }}
        >
          {content}
        </div>,
        ensurePortalTarget(),
      )
    : null;

  return (
    <span
      ref={triggerRef}
      class={['inline-flex max-w-full', className ?? ''].join(' ')}
      aria-describedby={open ? bubbleId : undefined}
    >
      {children}
      {bubble}
    </span>
  );
}

export default Tooltip;
