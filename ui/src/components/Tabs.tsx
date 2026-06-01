/**
 * Tabs — ARIA tablist + tabpanel composite (controlled).
 *
 * Used by Run Detail v2 (W18d) and Specs v2 (W18e). Thin wrapper
 * around the WAI tablist pattern — keyboard arrow nav, Home/End,
 * `aria-selected`, `aria-controls`.
 *
 * Controlled: parent owns `activeTab` and `onChange`. Each
 * `<Tab id label>` declares its content via children prop; only the
 * active panel is mounted (keep DOM size bounded; lazy-mount semantics).
 */

import type { ComponentChildren, JSX } from 'preact';
import { useCallback, useId } from 'preact/hooks';

export interface TabSpec {
  id: string;
  label: string;
  /** Optional badge (e.g. count, status pill) shown alongside the label. */
  badge?: ComponentChildren;
  /** Disabled state — tab still renders but is non-activatable. */
  disabled?: boolean;
}

export interface TabsProps {
  tabs: readonly TabSpec[];
  activeTab: string;
  onChange: (id: string) => void;
  /** Mapping from tab id → panel content. Only the active panel mounts. */
  panels: Record<string, ComponentChildren>;
  ariaLabel?: string;
  className?: string;
}

export function Tabs({
  tabs,
  activeTab,
  onChange,
  panels,
  ariaLabel = 'Tabs',
  className,
}: TabsProps): JSX.Element {
  const baseId = useId();

  const onKeyDown = useCallback(
    (e: KeyboardEvent) => {
      const idx = tabs.findIndex((t) => t.id === activeTab);
      const enabled = tabs.filter((t) => !t.disabled);
      if (enabled.length === 0) return;
      let nextId: string | null = null;
      switch (e.key) {
        case 'ArrowRight':
        case 'ArrowDown': {
          const start = (idx + 1) % tabs.length;
          for (let i = 0; i < tabs.length; i++) {
            const t = tabs[(start + i) % tabs.length];
            if (!t.disabled) {
              nextId = t.id;
              break;
            }
          }
          break;
        }
        case 'ArrowLeft':
        case 'ArrowUp': {
          const start = (idx - 1 + tabs.length) % tabs.length;
          for (let i = 0; i < tabs.length; i++) {
            const t = tabs[(start - i + tabs.length) % tabs.length];
            if (!t.disabled) {
              nextId = t.id;
              break;
            }
          }
          break;
        }
        case 'Home':
          nextId = enabled[0].id;
          break;
        case 'End':
          nextId = enabled[enabled.length - 1].id;
          break;
      }
      if (nextId && nextId !== activeTab) {
        e.preventDefault();
        onChange(nextId);
      }
    },
    [tabs, activeTab, onChange],
  );

  return (
    <div class={['tabs flex flex-col min-h-0', className ?? ''].join(' ')}>
      <div
        role="tablist"
        aria-label={ariaLabel}
        onKeyDown={onKeyDown}
        class="flex flex-wrap items-stretch gap-0.5 border-b border-slate-200 dark:border-slate-700 px-2"
      >
        {tabs.map((tab) => {
          const isActive = tab.id === activeTab;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              id={`${baseId}-tab-${tab.id}`}
              aria-selected={isActive ? 'true' : 'false'}
              aria-controls={`${baseId}-panel-${tab.id}`}
              tabIndex={isActive ? 0 : -1}
              disabled={tab.disabled}
              onClick={() => !tab.disabled && onChange(tab.id)}
              class={[
                'tabs-tab inline-flex items-center gap-1.5 px-3 py-1.5 text-sm border-b-2 -mb-px',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm',
                isActive
                  ? 'border-emerald-500 text-slate-900 dark:text-slate-100 font-medium'
                  : 'border-transparent text-slate-700 dark:text-slate-300 hover:text-slate-900 dark:hover:text-slate-100',
                tab.disabled ? 'opacity-40 cursor-not-allowed' : '',
              ].join(' ')}
            >
              <span>{tab.label}</span>
              {tab.badge ? <span class="ml-1">{tab.badge}</span> : null}
            </button>
          );
        })}
      </div>
      {tabs.map((tab) =>
        tab.id === activeTab ? (
          <div
            key={tab.id}
            role="tabpanel"
            id={`${baseId}-panel-${tab.id}`}
            aria-labelledby={`${baseId}-tab-${tab.id}`}
            class="tabs-panel flex-1 min-h-0 overflow-auto"
          >
            {panels[tab.id]}
          </div>
        ) : null,
      )}
    </div>
  );
}

export default Tabs;
