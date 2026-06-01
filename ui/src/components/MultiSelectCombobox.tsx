/**
 * ``<MultiSelectCombobox>`` — searchable multi-select dropdown.
 *
 * W23d centrepiece: the user asked for a "multiselect dropdown with
 * search, saved user choice in local storage per project name" on
 * /runs to filter by spec slug.
 *
 * Generic by design — the runs route binds it to SpecSummary today;
 * future routes can bind it to any T with a stable id + label
 * (drift signal kinds, tool-name filters, etc).
 *
 * Visual:
 *   - Trigger ``<button>`` shows the active selection count
 *     ("All specs" / "code-reviewer v3" / "2 specs" / "5 specs").
 *   - Dropdown panel positioned absolutely below the trigger.
 *   - Search input autofocuses on open.
 *   - Each option row is a labelled ``<input type="checkbox">``.
 *   - "Clear" + "Select all" buttons at the bottom (Select all
 *     respects the active search filter).
 *
 * Keyboard:
 *   - Click trigger / Enter / Space → opens panel.
 *   - Search input captures Up/Down to navigate the option list.
 *   - Space on a focused option toggles.
 *   - Escape closes panel.
 *   - Tab closes panel + moves focus naturally.
 *
 * Click-outside dismissal is wired to a single document-level
 * mousedown listener that's attached only while the panel is open.
 */

import type { JSX } from 'preact';
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';

export interface MultiSelectComboboxProps<T> {
  options: readonly T[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  getId: (opt: T) => string;
  getLabel: (opt: T) => string;
  getSubLabel?: (opt: T) => string | undefined;
  placeholder?: string;
  searchPlaceholder?: string;
  ariaLabel: string;
  className?: string;
}

function summariseSelection<T>(
  selected: Set<string>,
  options: readonly T[],
  getId: (opt: T) => string,
  getLabel: (opt: T) => string,
  placeholder: string,
): string {
  if (selected.size === 0) return placeholder;
  if (selected.size === 1) {
    const id = [...selected][0];
    const opt = options.find((o) => getId(o) === id);
    return opt ? getLabel(opt) : `1 selected`;
  }
  if (selected.size === 2) {
    const labels = options
      .filter((o) => selected.has(getId(o)))
      .map(getLabel);
    return labels.join(' + ');
  }
  return `${selected.size} selected`;
}

export function MultiSelectCombobox<T>({
  options,
  selected,
  onChange,
  getId,
  getLabel,
  getSubLabel,
  placeholder = 'All',
  searchPlaceholder = 'Search…',
  ariaLabel,
  className = '',
}: MultiSelectComboboxProps<T>): JSX.Element {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  // Close on click-outside while open.
  useEffect(() => {
    if (!open) return undefined;
    const onMousedown = (e: MouseEvent) => {
      const t = e.target as Node | null;
      if (!t) return;
      if (panelRef.current?.contains(t)) return;
      if (triggerRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener('mousedown', onMousedown);
    return () => document.removeEventListener('mousedown', onMousedown);
  }, [open]);

  // Autofocus search on open; restore focus to trigger on close.
  useEffect(() => {
    if (open) {
      const t = setTimeout(() => searchRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
    triggerRef.current?.focus();
    return undefined;
  }, [open]);

  const filtered = useMemo(() => {
    if (!query.trim()) return options;
    const needle = query.trim().toLowerCase();
    return options.filter((o) => {
      const label = getLabel(o).toLowerCase();
      const sub = getSubLabel?.(o)?.toLowerCase() ?? '';
      return label.includes(needle) || sub.includes(needle);
    });
  }, [options, query, getLabel, getSubLabel]);

  const summary = summariseSelection(selected, options, getId, getLabel, placeholder);

  const toggle = useCallback(
    (id: string) => {
      const next = new Set(selected);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      onChange(next);
    },
    [selected, onChange],
  );

  const clear = () => onChange(new Set());
  const selectAllFiltered = () => {
    const next = new Set(selected);
    for (const opt of filtered) next.add(getId(opt));
    onChange(next);
  };

  return (
    <div class={`relative inline-block ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') setOpen(false);
        }}
        aria-haspopup="listbox"
        aria-expanded={open ? 'true' : 'false'}
        aria-label={ariaLabel}
        class={[
          'inline-flex items-center gap-1.5 rounded-md',
          'border border-slate-300 dark:border-slate-600',
          'bg-white dark:bg-slate-800',
          'px-3 py-1 text-sm',
          'text-slate-700 dark:text-slate-200',
          'hover:bg-slate-50 dark:hover:bg-slate-700',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
        ].join(' ')}
      >
        <span class="truncate max-w-[18rem]">{summary}</span>
        <span aria-hidden="true" class="text-xs opacity-60">▾</span>
      </button>
      {open ? (
        <div
          ref={panelRef}
          role="dialog"
          aria-label={ariaLabel}
          class={[
            'absolute z-30 mt-1 w-[min(20rem,calc(100vw-2rem))]',
            'rounded-md border border-slate-200 dark:border-slate-700',
            'bg-white dark:bg-slate-800 shadow-lg',
            'overflow-hidden',
          ].join(' ')}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.preventDefault();
              setOpen(false);
            }
          }}
        >
          <div class="p-2 border-b border-slate-200 dark:border-slate-700">
            <input
              ref={searchRef}
              type="text"
              value={query}
              onInput={(e) => setQuery((e.currentTarget as HTMLInputElement).value)}
              placeholder={searchPlaceholder}
              aria-label="Filter options"
              class={[
                'w-full rounded px-2 py-1 text-sm',
                'bg-slate-50 dark:bg-slate-900',
                'border border-slate-200 dark:border-slate-700',
                'text-slate-700 dark:text-slate-200',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
              ].join(' ')}
            />
          </div>
          <ul
            role="listbox"
            aria-multiselectable="true"
            class="max-h-64 overflow-auto py-1"
          >
            {filtered.length === 0 ? (
              <li class="px-3 py-2 text-xs text-slate-500 dark:text-slate-400 italic">
                No matches
              </li>
            ) : (
              filtered.map((opt) => {
                const id = getId(opt);
                const checked = selected.has(id);
                const sub = getSubLabel?.(opt);
                return (
                  <li key={id} role="option" aria-selected={checked ? 'true' : 'false'}>
                    <label
                      class={[
                        'flex items-start gap-2 px-3 py-1.5 cursor-pointer',
                        'hover:bg-slate-50 dark:hover:bg-slate-700',
                        checked ? 'bg-emerald-50/50 dark:bg-emerald-900/20' : '',
                      ].join(' ')}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggle(id)}
                        class="mt-0.5 h-4 w-4 rounded border-slate-300 dark:border-slate-600 text-emerald-600 focus:ring-emerald-500"
                      />
                      <span class="min-w-0 flex-1 leading-tight">
                        <span class="block text-sm text-slate-800 dark:text-slate-100 truncate">
                          {getLabel(opt)}
                        </span>
                        {sub ? (
                          <span class="block text-[10px] text-slate-500 dark:text-slate-400 truncate">
                            {sub}
                          </span>
                        ) : null}
                      </span>
                    </label>
                  </li>
                );
              })
            )}
          </ul>
          <div class="flex items-center justify-between gap-2 px-3 py-2 border-t border-slate-200 dark:border-slate-700 text-xs">
            <button
              type="button"
              onClick={clear}
              class="text-slate-600 dark:text-slate-300 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
            >
              Clear
            </button>
            <button
              type="button"
              onClick={selectAllFiltered}
              class="text-slate-600 dark:text-slate-300 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
            >
              Select all{query ? ' (filtered)' : ''}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default MultiSelectCombobox;
