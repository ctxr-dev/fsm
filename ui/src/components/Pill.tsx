import type { ComponentChildren, JSX } from 'preact';

export type PillVariant = 'neutral' | 'success' | 'warning' | 'danger' | 'info';
export type PillSize = 'sm' | 'md';

export interface PillProps {
  variant: PillVariant;
  size?: PillSize;
  children: ComponentChildren;
  className?: string;
  /** Optional title attribute (tooltip text) — useful for truncated pills. */
  title?: string;
  /** ARIA label override. */
  'aria-label'?: string;
}

const VARIANT_CLASSES: Record<PillVariant, string> = {
  neutral:
    'bg-slate-100 text-slate-700 ring-1 ring-inset ring-slate-200 ' +
    'dark:bg-slate-700 dark:text-slate-200 dark:ring-slate-600',
  success:
    'bg-emerald-100 text-emerald-800 ring-1 ring-inset ring-emerald-200 ' +
    'dark:bg-emerald-900 dark:text-emerald-200 dark:ring-emerald-700',
  warning:
    'bg-amber-100 text-amber-800 ring-1 ring-inset ring-amber-200 ' +
    'dark:bg-amber-900 dark:text-amber-200 dark:ring-amber-700',
  danger:
    'bg-red-100 text-red-800 ring-1 ring-inset ring-red-200 ' +
    'dark:bg-red-900 dark:text-red-200 dark:ring-red-700',
  info:
    'bg-slate-200 text-slate-800 ring-1 ring-inset ring-slate-300 ' +
    'dark:bg-slate-600 dark:text-slate-100 dark:ring-slate-500',
};

const SIZE_CLASSES: Record<PillSize, string> = {
  sm: 'px-2 py-0.5 text-xs',
  md: 'px-2.5 py-1 text-sm',
};

/**
 * Pill — small status badge. Semantic colour mapping is the contract; callers
 * pick a `variant`, never override colours directly.
 */
export function Pill({
  variant,
  size = 'sm',
  children,
  className = '',
  title,
  'aria-label': ariaLabel,
}: PillProps): JSX.Element {
  const base =
    'inline-flex items-center gap-1 rounded-full font-medium whitespace-nowrap';
  const composed = [
    base,
    SIZE_CLASSES[size],
    VARIANT_CLASSES[variant],
    className,
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <span class={composed} title={title} aria-label={ariaLabel}>
      {children}
    </span>
  );
}

export default Pill;
