import type { ComponentChildren, JSX } from 'preact';
import { Spinner } from './Spinner';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md' | 'lg';

export interface ButtonProps {
  variant: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  disabled?: boolean;
  onClick?: (event: MouseEvent) => void;
  type?: 'button' | 'submit' | 'reset';
  children: ComponentChildren;
  className?: string;
  /** ARIA label override (use when the button has icon-only children). */
  'aria-label'?: string;
  /**
   * ARIA current marker. Set to ``"page"`` for the active page button
   * inside a Pagination control; ``"true"`` / ``"step"`` for other
   * "you are here" affordances (active step in a wizard, etc.).
   * AT users get an explicit "current page" announcement that the
   * visual ``primary`` variant cannot convey on its own.
   */
  'aria-current'?: 'page' | 'step' | 'true' | 'false';
  /** Form id for type="submit" buttons living outside the form. */
  form?: string;
  /** Autofocus on mount — opt in only. */
  autofocus?: boolean;
  /** Title / tooltip text. */
  title?: string;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    'bg-emerald-600 text-white hover:bg-emerald-700 active:bg-emerald-800 ' +
    'dark:bg-emerald-500 dark:hover:bg-emerald-400 dark:active:bg-emerald-600 ' +
    'border border-transparent shadow-sm',
  secondary:
    'bg-white text-slate-900 hover:bg-slate-100 active:bg-slate-200 ' +
    'border border-slate-300 ' +
    'dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700 ' +
    'dark:active:bg-slate-600 dark:border-slate-600 shadow-sm',
  ghost:
    'bg-transparent text-slate-700 hover:bg-slate-100 active:bg-slate-200 ' +
    'dark:text-slate-200 dark:hover:bg-slate-700 dark:active:bg-slate-600 ' +
    'border border-transparent',
  danger:
    'bg-red-600 text-white hover:bg-red-700 active:bg-red-800 ' +
    'dark:bg-red-500 dark:hover:bg-red-400 dark:active:bg-red-600 ' +
    'border border-transparent shadow-sm',
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-sm gap-1.5',
  md: 'h-10 px-4 text-sm gap-2',
  lg: 'h-12 px-6 text-base gap-2',
};

/**
 * Button — interactive primitive with semantic variants.
 *
 * - Visible focus ring (`focus-visible:ring-2 ring-offset-2 ring-emerald-500`).
 * - Keyboard support is native (button element).
 * - `loading=true` shows a spinner, disables clicks, sets aria-busy.
 * - `disabled=true` greys out, removes pointer events, sets aria-disabled.
 */
export function Button({
  variant,
  size = 'md',
  loading = false,
  disabled = false,
  onClick,
  type = 'button',
  children,
  className = '',
  'aria-label': ariaLabel,
  'aria-current': ariaCurrent,
  form,
  autofocus,
  title,
}: ButtonProps): JSX.Element {
  const isDisabled = disabled || loading;
  const base =
    'inline-flex items-center justify-center font-medium rounded-md ' +
    'select-none transition-colors ' +
    'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 ' +
    'focus-visible:ring-emerald-500 ' +
    'dark:focus-visible:ring-offset-slate-900 ' +
    'disabled:opacity-50 disabled:cursor-not-allowed';
  const composed = [
    base,
    SIZE_CLASSES[size],
    VARIANT_CLASSES[variant],
    className,
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <button
      type={type}
      class={composed}
      onClick={isDisabled ? undefined : onClick}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      aria-disabled={isDisabled || undefined}
      aria-label={ariaLabel}
      aria-current={ariaCurrent}
      form={form}
      autofocus={autofocus}
      title={title}
    >
      {loading ? <Spinner size="sm" label="Working" /> : null}
      <span class={loading ? 'opacity-80' : undefined}>{children}</span>
    </button>
  );
}

export default Button;
