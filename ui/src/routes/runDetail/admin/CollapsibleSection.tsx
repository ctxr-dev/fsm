/**
 * Small accordion primitive shared by the admin sheet's six sections.
 *
 * The right-column Admin Card on /runs/:id renders its sections inline
 * with fixed-weight headings; in the sheet we want the same Card chrome
 * but with an expand/collapse toggle so the operator can scan a long
 * stack and only open the one they care about. Native ``<details>``
 * would also work, but the surrounding chrome (Pill counts, error
 * pills, focus styles) is easier to compose with our own button than
 * with the disclosure widget's restricted styling surface.
 *
 * The header button is keyboard-friendly (Enter / Space toggle) and the
 * caret rotates 90° on open so the visual state matches the aria one.
 * When ``defaultOpen`` is true, the section mounts already expanded —
 * pass it for the section the operator is most likely to inspect first
 * (the run-scoped journal txn).
 */

import type { ComponentChildren, JSX, VNode } from 'preact';
import { useCallback, useState } from 'preact/hooks';

export interface CollapsibleSectionProps {
  /** Stable id; used as the aria-controls target for the disclosure. */
  id: string;
  /** Heading text (or a VNode for a Pill-decorated label). */
  title: ComponentChildren;
  /** Optional right-aligned slot (e.g. row count Pill or refresh button). */
  trailing?: VNode;
  /** Body — only rendered when expanded so child fetches don't fire
   *  until the operator opens the section. */
  children: ComponentChildren;
  /** Expanded on first mount. Default ``false``. */
  defaultOpen?: boolean;
}

export function CollapsibleSection({
  id,
  title,
  trailing,
  children,
  defaultOpen = false,
}: CollapsibleSectionProps): JSX.Element {
  const [open, setOpen] = useState<boolean>(defaultOpen);
  const onToggle = useCallback(() => setOpen((v) => !v), []);

  const panelId = `${id}-panel`;
  const headerId = `${id}-header`;

  return (
    <section
      class="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800"
      aria-labelledby={headerId}
    >
      <button
        type="button"
        id={headerId}
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={panelId}
        class="w-full flex items-center justify-between gap-2 px-3 py-2 text-left text-sm font-semibold text-slate-900 dark:text-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-500 rounded-xl"
      >
        <span class="flex items-center gap-2 min-w-0 truncate">
          <span
            aria-hidden="true"
            class={[
              'inline-flex items-center justify-center w-4 h-4 shrink-0',
              'text-slate-400 dark:text-slate-500',
              'motion-safe:transition-transform',
              open ? 'rotate-90' : '',
            ].join(' ')}
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 16 16"
              fill="currentColor"
              class="w-3 h-3"
            >
              <path d="M6 3.5L10.5 8 6 12.5V3.5z" />
            </svg>
          </span>
          <span class="truncate">{title}</span>
        </span>
        {trailing ? <span class="shrink-0">{trailing}</span> : null}
      </button>
      {open ? (
        <div
          id={panelId}
          role="region"
          aria-labelledby={headerId}
          class="border-t border-slate-200 dark:border-slate-700 px-3 py-3 text-sm"
        >
          {children}
        </div>
      ) : null}
    </section>
  );
}

export default CollapsibleSection;
