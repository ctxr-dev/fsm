/**
 * KeyValueTable — flat key/value display where:
 *
 *   - Key click → copies the key + emits onSelect(key, value).
 *   - Primitive value click → copies the stringified value.
 *   - Complex value (object/array) → embedded inline <JsonViewer>.
 *
 * Use it for: DoctorReport pragmas, run metadata args, manifest fields,
 * any "show me a row per key" surface. Replaces ad-hoc `<dl>` lists.
 */

import { useCallback } from 'preact/hooks';
import type { JSX } from 'preact';

import { copyText } from '../lib/clipboard';
import { JsonViewer } from './JsonViewer';

export interface KvRow {
  key: string;
  value: unknown;
  /** Optional second-line hint shown below the key in dim text. */
  hint?: string;
}

export interface KeyValueTableProps {
  rows: readonly KvRow[];
  onSelect?: (key: string, value: unknown) => void;
  caption?: string;
  className?: string;
}

/** A value is "complex" if it's a non-null object or an array. */
function isComplex(value: unknown): boolean {
  return value !== null && typeof value === 'object';
}

/** Render a primitive value as a copy-friendly string. */
function stringifyPrimitive(value: unknown): string {
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

export function KeyValueTable({
  rows,
  onSelect,
  caption,
  className,
}: KeyValueTableProps): JSX.Element {
  const handleKeyClick = useCallback(
    (row: KvRow) => {
      void copyText(row.key);
      onSelect?.(row.key, row.value);
    },
    [onSelect],
  );

  return (
    <dl
      aria-label={caption ?? 'Key value table'}
      class={[
        'kv text-sm divide-y divide-slate-100 dark:divide-slate-800',
        className ?? '',
      ].join(' ')}
    >
      {rows.map((row) => (
        <div
          key={row.key}
          class="kv-row grid grid-cols-[12rem_1fr] gap-2 items-start py-1.5"
        >
          <dt class="min-w-0">
            <button
              type="button"
              onClick={() => handleKeyClick(row)}
              title={`Copy key "${row.key}" + select`}
              class="kv-key text-left text-slate-700 dark:text-slate-200 font-medium hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
            >
              {row.key}
            </button>
            {row.hint ? (
              <div class="text-[10px] text-slate-500 dark:text-slate-400">{row.hint}</div>
            ) : null}
          </dt>
          <dd class="kv-value min-w-0 font-mono text-xs">
            {isComplex(row.value) ? (
              <JsonViewer
                value={row.value}
                rootLabel={row.key}
                mode="inline"
                maxInlineHeight="max-h-32"
              />
            ) : (
              <button
                type="button"
                onClick={() => void copyText(stringifyPrimitive(row.value))}
                title="Copy value"
                class="text-left text-slate-700 dark:text-slate-300 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm break-all"
              >
                {stringifyPrimitive(row.value)}
              </button>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export default KeyValueTable;
