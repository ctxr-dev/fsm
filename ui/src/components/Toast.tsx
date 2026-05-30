import type { JSX } from 'preact';
import { signal } from '@preact/signals';
import { useEffect } from 'preact/hooks';
import type { PillVariant } from './Pill';

export type ToastVariant = Exclude<PillVariant, 'info'> | 'info';

export interface Toast {
  id: string;
  message: string;
  variant: ToastVariant;
  /** Milliseconds until auto-dismiss. Defaults to 4000. */
  durationMs: number;
}

export interface ShowToastOptions {
  variant?: ToastVariant;
  durationMs?: number;
}

const toastsSignal = signal<Toast[]>([]);

function makeId(): string {
  return `toast-${Date.now().toString(36)}-${Math.random()
    .toString(36)
    .slice(2, 8)}`;
}

function pushToast(message: string, opts: ShowToastOptions = {}): string {
  const id = makeId();
  const variant = opts.variant ?? 'info';
  const durationMs = opts.durationMs ?? 4000;
  toastsSignal.value = [
    ...toastsSignal.value,
    { id, message, variant, durationMs },
  ];
  if (durationMs > 0) {
    setTimeout(() => dismissToast(id), durationMs);
  }
  return id;
}

function dismissToast(id: string): void {
  toastsSignal.value = toastsSignal.value.filter((t) => t.id !== id);
}

/**
 * useToast — handle for pushing transient notifications onto the global queue.
 *
 * Toasts share a single in-memory signal so any component can dispatch them
 * without prop drilling, and a single <ToastContainer /> at the app root
 * renders them.
 */
export function useToast(): {
  show: (message: string, opts?: ShowToastOptions) => string;
  dismiss: (id: string) => void;
  success: (message: string, opts?: ShowToastOptions) => string;
  warning: (message: string, opts?: ShowToastOptions) => string;
  danger: (message: string, opts?: ShowToastOptions) => string;
  info: (message: string, opts?: ShowToastOptions) => string;
} {
  return {
    show: pushToast,
    dismiss: dismissToast,
    success: (message, opts) => pushToast(message, { ...opts, variant: 'success' }),
    warning: (message, opts) => pushToast(message, { ...opts, variant: 'warning' }),
    danger: (message, opts) => pushToast(message, { ...opts, variant: 'danger' }),
    info: (message, opts) => pushToast(message, { ...opts, variant: 'info' }),
  };
}

const VARIANT_STYLES: Record<ToastVariant, string> = {
  neutral:
    'bg-white text-slate-900 border-slate-200 dark:bg-slate-800 dark:text-slate-100 dark:border-slate-700',
  success:
    'bg-emerald-50 text-emerald-900 border-emerald-200 dark:bg-emerald-900/40 dark:text-emerald-100 dark:border-emerald-700',
  warning:
    'bg-amber-50 text-amber-900 border-amber-200 dark:bg-amber-900/40 dark:text-amber-100 dark:border-amber-700',
  danger:
    'bg-red-50 text-red-900 border-red-200 dark:bg-red-900/40 dark:text-red-100 dark:border-red-700',
  info:
    'bg-slate-50 text-slate-900 border-slate-200 dark:bg-slate-700 dark:text-slate-100 dark:border-slate-600',
};

/**
 * ToastContainer — renders the live toast queue.
 *
 * Mount this exactly once near the app root. The container is `aria-live=polite`
 * so screen readers announce new messages without interrupting the user.
 */
export function ToastContainer(): JSX.Element {
  // Subscribe to the signal. Reading `.value` inside the render establishes
  // the dependency.
  const items = toastsSignal.value;

  // Keep the cleanup discipline tight — if the container unmounts we drop
  // pending toasts so we don't leak timers on hot-reload.
  useEffect(() => {
    return () => {
      toastsSignal.value = [];
    };
  }, []);

  return (
    <div
      class="fixed inset-x-0 bottom-0 z-50 flex flex-col items-center gap-2 p-4 pointer-events-none sm:items-end"
      aria-live="polite"
      aria-atomic="false"
      role="region"
      aria-label="Notifications"
    >
      {items.map((t) => (
        <div
          key={t.id}
          role="status"
          class={[
            'pointer-events-auto w-full max-w-sm rounded-lg border shadow-md',
            'px-4 py-3 text-sm flex items-start gap-3',
            VARIANT_STYLES[t.variant],
          ].join(' ')}
        >
          <span class="flex-1 leading-snug">{t.message}</span>
          <button
            type="button"
            onClick={() => dismissToast(t.id)}
            class="shrink-0 text-current opacity-60 hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
            aria-label="Dismiss notification"
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
      ))}
    </div>
  );
}

export default ToastContainer;
