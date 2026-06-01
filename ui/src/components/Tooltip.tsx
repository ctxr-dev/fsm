/**
 * Tooltip — hover-or-focus surfaced overlay for terse, full-string content.
 *
 * Use when a label is visually truncated (FlowGraph state names,
 * predicate edge labels, etc.) and the operator must be able to read
 * the FULL value without taking an extra click. Click-to-open is a
 * Sheet's job; Tooltip is the at-a-glance preview.
 *
 * Contract (matches the W18 interaction grammar):
 *   - Open on mouseenter (after 400 ms delay) OR focus (immediate).
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

export type TooltipPlacement = 'top' | 'bottom' | 'left' | 'right' | 'auto';

export interface TooltipProps {
  /** The full-detail body shown when the tooltip surfaces. */
  content: ComponentChild;
  /** Open delay in ms. Default 400. Set 0 for an instant tooltip. */
  delay?: number;
  /** Preferred placement; auto-flips on viewport-edge clip. Default 'top'. */
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
  let top: number;
  let left: number;
  let flipped = false;

  const placeTop = (): { top: number; left: number } => ({
    top: trigger.top - bubble.height - margin,
    left: trigger.left + trigger.width / 2 - bubble.width / 2,
  });
  const placeBottom = (): { top: number; left: number } => ({
    top: trigger.bottom + margin,
    left: trigger.left + trigger.width / 2 - bubble.width / 2,
  });

  const initial = placement === 'bottom' ? placeBottom() : placeTop();
  top = initial.top;
  left = initial.left;
  if (top < margin) {
    const flipTo = placement === 'bottom' ? placeTop() : placeBottom();
    if (
      flipTo.top >= margin &&
      flipTo.top + bubble.height <= vh - margin
    ) {
      top = flipTo.top;
      left = flipTo.left;
      flipped = true;
    } else {
      top = margin;
    }
  }
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
    setOpen(false);
  }, []);

  // Suppress native title= on the trigger so the browser doesn't
  // race our bubble with its own. Restore on close.
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

  const scheduleOpen = useCallback((): void => {
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
    const useDelay = now < warmUntil ? 0 : delay;
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
    const onFocusIn = (): void => scheduleOpen();
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
