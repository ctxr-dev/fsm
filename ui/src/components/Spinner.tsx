import type { JSX } from 'preact';

export type SpinnerSize = 'sm' | 'md' | 'lg';

export interface SpinnerProps {
  size?: SpinnerSize;
  /** Accessible label announced to screen readers. Defaults to "Loading". */
  label?: string;
  className?: string;
}

const SIZE_CLASSES: Record<SpinnerSize, string> = {
  sm: 'h-4 w-4 border-2',
  md: 'h-6 w-6 border-2',
  lg: 'h-10 w-10 border-[3px]',
};

/**
 * Spinner — indeterminate loading indicator.
 *
 * Uses `role="status"` with a visually hidden label so AT users hear progress.
 * `animate-spin` is gated by the global `prefers-reduced-motion` rule in
 * `theme.css`, which collapses transitions/animations to ~0ms.
 */
export function Spinner({
  size = 'md',
  label = 'Loading',
  className = '',
}: SpinnerProps): JSX.Element {
  const composed = [
    'inline-block rounded-full animate-spin',
    'border-slate-300 border-t-emerald-500',
    'dark:border-slate-600 dark:border-t-emerald-400',
    SIZE_CLASSES[size],
    className,
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <span role="status" class="inline-flex items-center gap-2">
      <span aria-hidden="true" class={composed} />
      <span class="sr-only">{label}</span>
    </span>
  );
}

export default Spinner;
