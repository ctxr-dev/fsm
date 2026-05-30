import type { JSX, VNode } from 'preact';

export interface EmptyStateProps {
  /** Optional icon / illustration (any VNode). */
  icon?: VNode;
  /** Headline — required so empty states always feel intentional. */
  title: string;
  /** Optional supporting copy below the headline. */
  message?: string;
  /** Optional CTA — usually a <Button />. */
  action?: VNode;
  className?: string;
}

/**
 * EmptyState — used when a collection (table, list, timeline) has zero rows.
 * Keep copy short, action-oriented, and avoid blaming the user.
 */
export function EmptyState({
  icon,
  title,
  message,
  action,
  className = '',
}: EmptyStateProps): JSX.Element {
  const composed = [
    'flex flex-col items-center justify-center text-center',
    'gap-3 py-12 px-6',
    'text-slate-600 dark:text-slate-300',
    className,
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <div class={composed} role="status">
      {icon ? (
        <div class="text-slate-400 dark:text-slate-500" aria-hidden="true">
          {icon}
        </div>
      ) : null}
      <h3 class="text-lg font-semibold text-slate-900 dark:text-slate-100">
        {title}
      </h3>
      {message ? (
        <p class="max-w-md text-sm leading-relaxed">{message}</p>
      ) : null}
      {action ? <div class="mt-2">{action}</div> : null}
    </div>
  );
}

export default EmptyState;
