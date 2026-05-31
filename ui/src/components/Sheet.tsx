/**
 * Sheet — right-anchored slide-out panel for deep inspection.
 *
 * Sibling of Dialog. Use it when the content is too rich for a
 * centred dialog (a full JsonViewer, a multi-tab Brief inspector, a
 * spec graph). Width modes cycle right-third → right-half → fullscreen.
 *
 * Inherits a11y from `lib/a11y.ts`: focus trap, escape-to-close, body-
 * scroll-lock — identical to Dialog. Pushes a history state so browser
 * back closes the sheet.
 *
 * For multi-sheet stacking, do NOT mount Sheet directly per call;
 * instead push entries to `sheetStack` in store and let `<SheetHost>`
 * render them. This component is the unit Sheet (used by Dialog-like
 * one-off slide-outs or by SheetHost internally).
 */

import type { ComponentChildren, JSX, VNode } from 'preact';
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';

import {
  useBodyScrollLock,
  useEscapeToClose,
  useFocusTrap,
} from '../lib/a11y';

export type SheetWidth = 'right-third' | 'right-half' | 'fullscreen';
export type SheetSide = 'right' | 'bottom';

export interface SheetProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ComponentChildren;
  /** Optional footer. Renders below body, separated by a divider. */
  footer?: VNode;
  /** Initial width. Header has a button to cycle widths. */
  width?: SheetWidth;
  /** Side of the viewport. Bottom is reserved for mobile; v1 only supports right. */
  side?: SheetSide;
  /** When false (or width=fullscreen), backdrop click does NOT close. */
  closeOnBackdrop?: boolean;
  /** When true, Escape does NOT close. */
  pinned?: boolean;
  /** Push history state on open / pop on close. Default true. */
  pushHistory?: boolean;
  /** ARIA labelledby id for the title element. Auto-generated if absent. */
  id?: string;
}

const WIDTH_CLASSES: Record<SheetWidth, string> = {
  'right-third': 'w-full sm:w-[33vw] sm:min-w-[420px] sm:max-w-[640px]',
  'right-half': 'w-full sm:w-[50vw] sm:min-w-[520px] sm:max-w-[960px]',
  fullscreen: 'w-full max-w-none',
};

const WIDTH_CYCLE: SheetWidth[] = ['right-third', 'right-half', 'fullscreen'];

let _sheetCounter = 0;

export function Sheet({
  open,
  onClose,
  title,
  children,
  footer,
  width: initialWidth = 'right-half',
  side = 'right',
  closeOnBackdrop = true,
  pinned = false,
  pushHistory = true,
  id,
}: SheetProps): JSX.Element | null {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const titleIdRef = useRef<string>(
    id ?? `sheet-title-${(_sheetCounter += 1)}`,
  );
  const [width, setWidth] = useState<SheetWidth>(initialWidth);

  useFocusTrap(panelRef, open);
  useEscapeToClose(open && !pinned, onClose);
  useBodyScrollLock(open);

  // Browser back closes: push a sentinel state on open; pop on close.
  // popstate from a back-button click fires onClose without re-pushing.
  useEffect(() => {
    if (!open || !pushHistory || typeof window === 'undefined') return undefined;
    const sentinel = { sheet: titleIdRef.current };
    window.history.pushState(sentinel, '');
    const onPop = () => onClose();
    window.addEventListener('popstate', onPop);
    return () => {
      window.removeEventListener('popstate', onPop);
      // If we still own the sentinel, pop it. Guarded so a route change
      // (which already popped) doesn't double-pop.
      if (window.history.state?.sheet === titleIdRef.current) {
        window.history.back();
      }
    };
  }, [open, pushHistory, onClose]);

  const cycleWidth = useCallback(() => {
    setWidth((prev) => {
      const idx = WIDTH_CYCLE.indexOf(prev);
      return WIDTH_CYCLE[(idx + 1) % WIDTH_CYCLE.length];
    });
  }, []);

  const onBackdrop = useCallback(
    (e: MouseEvent) => {
      if (width === 'fullscreen' || !closeOnBackdrop || pinned) return;
      if (e.target === e.currentTarget) onClose();
    },
    [width, closeOnBackdrop, pinned, onClose],
  );

  const sidePosition = useMemo(() => {
    return side === 'right' ? 'justify-end' : 'items-end';
  }, [side]);

  if (!open) return null;

  return (
    <div
      class={[
        'fixed inset-0 z-50 flex',
        sidePosition,
        width === 'fullscreen'
          ? 'bg-slate-900/80'
          : 'bg-slate-900/60 backdrop-blur-sm',
      ].join(' ')}
      onClick={onBackdrop}
      aria-hidden="false"
    >
      <aside
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleIdRef.current}
        tabIndex={-1}
        class={[
          'h-full bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100',
          'border-l border-slate-200 dark:border-slate-700 shadow-2xl',
          'flex flex-col focus:outline-none',
          'motion-safe:transition-transform motion-safe:duration-200 motion-safe:ease-out',
          WIDTH_CLASSES[width],
        ].join(' ')}
      >
        <header class="flex items-center justify-between gap-2 px-4 py-3 border-b border-slate-200 dark:border-slate-700">
          <h2
            id={titleIdRef.current}
            class="text-base font-semibold leading-tight truncate"
          >
            {title}
          </h2>
          <div class="flex items-center gap-1">
            <button
              type="button"
              onClick={cycleWidth}
              aria-label={`Cycle width (currently ${width})`}
              title="Cycle width"
              class="inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
            >
              <svg viewBox="0 0 20 20" fill="currentColor" class="w-4 h-4" aria-hidden="true">
                <path d="M3 4a1 1 0 011-1h12a1 1 0 011 1v2a1 1 0 01-1 1H4a1 1 0 01-1-1V4zM3 10a1 1 0 011-1h8a1 1 0 011 1v2a1 1 0 01-1 1H4a1 1 0 01-1-1v-2zM3 16a1 1 0 011-1h4a1 1 0 011 1v.001A1 1 0 018 17H4a1 1 0 01-1-1v0z" />
              </svg>
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close sheet"
              class="inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
            >
              <svg viewBox="0 0 20 20" fill="currentColor" class="w-4 h-4" aria-hidden="true">
                <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
              </svg>
            </button>
          </div>
        </header>
        <div class="flex-1 overflow-auto px-4 py-3 text-sm">{children}</div>
        {footer ? (
          <footer class="flex items-center justify-end gap-2 px-4 py-3 border-t border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/40">
            {footer}
          </footer>
        ) : null}
      </aside>
    </div>
  );
}

export default Sheet;
