import type { ComponentChildren, JSX, VNode } from 'preact';
import { useCallback, useEffect, useRef } from 'preact/hooks';

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ComponentChildren;
  /** Optional footer node — usually a row of buttons. */
  footer?: VNode;
  /** Override the panel width. Defaults to "max-w-lg". */
  widthClassName?: string;
  /** If false, clicking the backdrop will NOT close. */
  closeOnBackdrop?: boolean;
  /** Optional id for ARIA labelledby (auto-generated otherwise). */
  id?: string;
}

const FOCUSABLE_SELECTOR =
  'a[href], area[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

let dialogCounter = 0;

/**
 * Dialog — modal surface with focus trap and escape-to-close.
 *
 * - When `open` flips true, focus is moved into the dialog and the previously
 *   focused element is remembered so we can restore focus on close.
 * - Tab / Shift+Tab loop inside the dialog.
 * - Escape calls `onClose`.
 * - The backdrop is `aria-hidden`; the panel uses `role="dialog"` with
 *   `aria-modal="true"`.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  widthClassName = 'max-w-lg',
  closeOnBackdrop = true,
  id,
}: DialogProps): JSX.Element | null {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const lastActiveRef = useRef<HTMLElement | null>(null);
  const titleIdRef = useRef<string>(
    id ?? `dialog-title-${(dialogCounter += 1)}`,
  );

  const focusFirst = useCallback(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const focusables = panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
    const target = focusables[0] ?? panel;
    target.focus();
  }, []);

  // Mount / unmount lifecycle: remember + restore focus, lock scroll.
  useEffect(() => {
    if (!open) return undefined;
    lastActiveRef.current = (document.activeElement as HTMLElement) ?? null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    // Defer focus to give the panel a chance to mount its children.
    queueMicrotask(focusFirst);
    return () => {
      document.body.style.overflow = previousOverflow;
      lastActiveRef.current?.focus?.();
    };
  }, [open, focusFirst]);

  // Key handler: Escape closes, Tab cycles within the panel.
  useEffect(() => {
    if (!open) return undefined;
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusables = Array.from(
        panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter((el) => !el.hasAttribute('disabled'));
      if (focusables.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (event.shiftKey) {
        if (active === first || !panel.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else {
        if (active === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [open, onClose]);

  if (!open) return null;

  const onBackdropClick = (event: MouseEvent) => {
    if (!closeOnBackdrop) return;
    if (event.target === event.currentTarget) onClose();
  };

  return (
    <div
      class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm"
      onClick={onBackdropClick}
      aria-hidden="false"
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleIdRef.current}
        tabIndex={-1}
        class={[
          'w-full bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100',
          'rounded-xl shadow-xl border border-slate-200 dark:border-slate-700',
          'flex flex-col max-h-[calc(100vh-2rem)]',
          'focus:outline-none',
          widthClassName,
        ].join(' ')}
      >
        <header class="flex items-start justify-between gap-4 px-5 py-4 border-b border-slate-200 dark:border-slate-700">
          <h2
            id={titleIdRef.current}
            class="text-lg font-semibold leading-tight"
          >
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            class="shrink-0 -mr-1 -mt-1 inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
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
        </header>
        <div class="flex-1 overflow-auto px-5 py-4 text-sm">{children}</div>
        {footer ? (
          <footer class="flex items-center justify-end gap-2 px-5 py-4 border-t border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/40 rounded-b-xl">
            {footer}
          </footer>
        ) : null}
      </div>
    </div>
  );
}

export default Dialog;
