/**
 * SheetHost — renders the `sheetStack` signal as a layered stack of
 * right-anchored panels.
 *
 * Mounted once at the Shell level. Reads `sheetStack.value` and renders
 * one panel per entry, each offset 24px to the left of the previous so
 * the stack is visible as a small breadcrumb of inspection trails
 * (spec → state in spec → commit signature for that state, etc.).
 *
 * Top sheet handling:
 *   - Esc closes the TOP sheet only (unless pinned).
 *   - Click outside closes the TOP sheet only (unless fullscreen or pinned).
 *   - The TOP sheet's `urlFragment` is mirrored to `?sheet=<fragment>`.
 *   - Browser back pops the top sheet (via the urlFragment effect).
 *
 * Why a single host rather than each Sheet rendering itself: the stack
 * needs ONE event listener for Esc / click-outside to avoid double-fire
 * issues, and ONE focus trap that knows about the topmost panel only.
 * Per-Sheet self-rendering would race; a central host serialises.
 *
 * Bundle-friendly: this component renders nothing when the stack is
 * empty (an early return saves Preact a diff every render).
 */

import { useCallback, useEffect, useMemo, useRef } from 'preact/hooks';
import type { JSX, VNode } from 'preact';

import {
  closeTopSheet,
  sheetStack,
  type SheetEntry,
} from '../lib/store';
import {
  useBodyScrollLock,
  useEscapeToClose,
  useFocusTrap,
} from '../lib/a11y';

const WIDTH_CLASSES: Record<NonNullable<SheetEntry['width']>, string> = {
  'right-third': 'w-full sm:w-[33vw] sm:min-w-[420px] sm:max-w-[640px]',
  'right-half': 'w-full sm:w-[50vw] sm:min-w-[520px] sm:max-w-[960px]',
  fullscreen: 'w-full max-w-none',
};

/**
 * SheetHost is the singleton portal-mounted host. Render it once
 * inside the Shell, NOT per-route — multiple instances would
 * duplicate every key listener.
 */
export function SheetHost(): JSX.Element | null {
  const stack = sheetStack.value;
  if (stack.length === 0) return null;
  const top = stack[stack.length - 1];

  // Body-scroll-lock + escape-to-close + focus-trap only apply to the
  // top sheet. Stacked sheets below are visually present (lateral
  // offset) but not interactive.
  useBodyScrollLock(true);

  const onEscape = useCallback(() => {
    if (top.pinned) return; // pinned sheets stay open through Esc.
    closeTopSheet();
  }, [top.id, top.pinned]);

  useEscapeToClose(true, onEscape);

  // Top-sheet URL fragment mirror. Push fragment on open; pop on
  // unmount (handled by stack pop in store).
  useEffect(() => {
    if (!top.urlFragment) return undefined;
    const url = new URL(window.location.href);
    url.searchParams.set('sheet', top.urlFragment);
    window.history.replaceState(window.history.state, '', url.toString());
    return () => {
      const cleanup = new URL(window.location.href);
      if (cleanup.searchParams.get('sheet') === top.urlFragment) {
        cleanup.searchParams.delete('sheet');
        window.history.replaceState(window.history.state, '', cleanup.toString());
      }
    };
  }, [top.id, top.urlFragment]);

  return (
    <div
      class="fixed inset-0 z-40 pointer-events-none"
      aria-hidden={stack.length === 0}
    >
      {stack.map((entry, index) => {
        const isTop = index === stack.length - 1;
        return (
          <SheetPanel
            key={entry.id}
            entry={entry}
            isTop={isTop}
            stackDepth={stack.length - 1 - index}
          />
        );
      })}
    </div>
  );
}

interface SheetPanelProps {
  entry: SheetEntry;
  isTop: boolean;
  /** 0 = topmost, larger = pushed deeper below. */
  stackDepth: number;
}

function SheetPanel({ entry, isTop, stackDepth }: SheetPanelProps): JSX.Element {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const width = entry.width ?? 'right-half';
  const widthClass = WIDTH_CLASSES[width];

  // Focus trap only on the top sheet.
  useFocusTrap(panelRef, isTop);

  const onBackdrop = useCallback(
    (e: MouseEvent) => {
      if (!isTop) return;
      if (width === 'fullscreen' || entry.pinned) return;
      if (e.target === e.currentTarget) closeTopSheet();
    },
    [isTop, width, entry.pinned],
  );

  const onClose = useCallback(() => {
    if (isTop) closeTopSheet();
  }, [isTop]);

  // Layered visual offset: deeper sheets push left by 24px per level.
  const offsetStyle = useMemo<JSX.CSSProperties>(() => {
    if (stackDepth === 0) return {};
    const px = stackDepth * 24;
    return { transform: `translateX(-${px}px)` };
  }, [stackDepth]);

  // Backdrop is only solid for the top sheet. Stacked-below sheets
  // are visually faded so the user perceives the layering.
  const backdropOpacity = isTop ? 'bg-slate-900/60 backdrop-blur-sm' : 'bg-slate-900/20';
  // Non-top sheets cannot be clicked through.
  const pointer = 'pointer-events-auto';
  // Z-index: deeper sheets are lower so the top sheet receives clicks.
  const z = 40 + (stackDepth === 0 ? 9 : 9 - Math.min(stackDepth, 8));

  return (
    <div
      class={['fixed inset-0 flex justify-end', pointer, backdropOpacity].join(' ')}
      style={{ zIndex: z }}
      onClick={onBackdrop}
      aria-hidden={!isTop}
    >
      <aside
        ref={panelRef}
        role="dialog"
        aria-modal={isTop}
        aria-labelledby={`sheet-title-${entry.id}`}
        tabIndex={-1}
        style={offsetStyle}
        class={[
          'h-full bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100',
          'border-l border-slate-200 dark:border-slate-700 shadow-2xl',
          'flex flex-col focus:outline-none',
          'motion-safe:transition-transform motion-safe:duration-200 motion-safe:ease-out',
          widthClass,
        ].join(' ')}
      >
        <header class="flex items-center justify-between gap-2 px-4 py-3 border-b border-slate-200 dark:border-slate-700">
          <h2
            id={`sheet-title-${entry.id}`}
            class="text-base font-semibold leading-tight truncate"
          >
            {entry.title}
          </h2>
          <div class="flex items-center gap-1">
            <button
              type="button"
              onClick={onClose}
              aria-label="Close sheet"
              class="inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 20 20"
                fill="currentColor"
                class="w-4 h-4"
                aria-hidden="true"
              >
                <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
              </svg>
            </button>
          </div>
        </header>
        <div class="flex-1 overflow-auto px-4 py-3 text-sm">
          {entry.content as VNode}
        </div>
      </aside>
    </div>
  );
}

export default SheetHost;
