import type { ComponentChildren, JSX } from 'preact';

export interface CardProps {
  /** Optional extra class names appended to the base style. */
  className?: string;
  /** Optional heading rendered above the body in a slightly heavier weight. */
  title?: string;
  /** Children rendered inside the card body. */
  children: ComponentChildren;
  /** Optional id for ARIA / linking. */
  id?: string;
  /** Optional inline role for accessibility (defaults to none). */
  role?: JSX.AriaRole;
}

/**
 * Card — surface primitive used for grouped content.
 *
 * Visuals: rounded-xl, subtle shadow, 1px border, light/dark aware. Padding is
 * baked in (`p-4`); pass `className="p-0"` when callers need flush children.
 */
export function Card({
  className = '',
  title,
  children,
  id,
  role,
}: CardProps): JSX.Element {
  const base =
    'rounded-xl shadow-sm border border-slate-200 dark:border-slate-700 ' +
    'bg-white dark:bg-slate-800 p-4 text-slate-900 dark:text-slate-100';
  const composed = className ? `${base} ${className}` : base;
  return (
    <section id={id} role={role} class={composed}>
      {title ? (
        <header class="mb-3">
          <h2 class="text-lg font-semibold leading-tight">{title}</h2>
        </header>
      ) : null}
      {children}
    </section>
  );
}

export default Card;
