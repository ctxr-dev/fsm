import type { JSX, VNode } from 'preact';

export interface TableColumn<T> {
  /** Stable key — also used as React-style key for column-level reconciliation. */
  key: string;
  /** Header label. */
  label: string;
  /** Optional cell renderer. Defaults to (row as any)[key] coerced to string. */
  render?: (row: T) => VNode | string | number | null;
  /** Optional fixed width (any CSS length, e.g. "8rem", "120px"). */
  width?: string;
  /** Optional alignment. */
  align?: 'left' | 'right' | 'center';
  /** Optional className for the cell. */
  className?: string;
}

export interface TableProps<T> {
  columns: TableColumn<T>[];
  rows: T[];
  onRowClick?: (row: T) => void;
  /** Rendered when rows is empty. */
  emptyState?: VNode;
  /** Stable key extractor for rows. Defaults to row index — pass one for re-orders. */
  rowKey?: (row: T, index: number) => string | number;
  /** ARIA caption for the table. */
  caption?: string;
  className?: string;
}

const ALIGN_CLASSES: Record<NonNullable<TableColumn<unknown>['align']>, string> = {
  left: 'text-left',
  right: 'text-right',
  center: 'text-center',
};

/**
 * Table — semantic <table> with sticky header, hover highlight, and keyboard
 * navigation when `onRowClick` is provided (rows become tabbable, Enter/Space
 * activate).
 */
export function Table<T>({
  columns,
  rows,
  onRowClick,
  emptyState,
  rowKey,
  caption,
  className = '',
}: TableProps<T>): JSX.Element {
  if (rows.length === 0 && emptyState) {
    return <>{emptyState}</>;
  }

  const composed = [
    'w-full border-collapse text-sm',
    'text-slate-900 dark:text-slate-100',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  const interactive = typeof onRowClick === 'function';

  const handleRowKeyDown = (row: T) => (event: KeyboardEvent) => {
    if (!interactive) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onRowClick?.(row);
    }
  };

  return (
    <div class="overflow-auto rounded-md border border-slate-200 dark:border-slate-700">
      <table class={composed}>
        {caption ? <caption class="sr-only">{caption}</caption> : null}
        <thead class="sticky top-0 z-10 bg-slate-50 dark:bg-slate-800">
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                scope="col"
                style={col.width ? { width: col.width } : undefined}
                class={[
                  'px-3 py-2 font-semibold border-b border-slate-200 dark:border-slate-700',
                  'text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400',
                  col.align ? ALIGN_CLASSES[col.align] : 'text-left',
                ].join(' ')}
              >
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const key = rowKey ? rowKey(row, index) : index;
            return (
              <tr
                key={key}
                tabIndex={interactive ? 0 : undefined}
                role={interactive ? 'button' : undefined}
                onClick={interactive ? () => onRowClick?.(row) : undefined}
                onKeyDown={interactive ? handleRowKeyDown(row) : undefined}
                class={[
                  'border-b border-slate-100 dark:border-slate-800 last:border-b-0',
                  'transition-colors',
                  interactive
                    ? 'cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-700/60 ' +
                      'focus:outline-none focus-visible:ring-2 focus-visible:ring-inset ' +
                      'focus-visible:ring-emerald-500'
                    : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
              >
                {columns.map((col) => {
                  const cell = col.render
                    ? col.render(row)
                    : ((row as unknown as Record<string, unknown>)[col.key] as
                        | string
                        | number
                        | null
                        | undefined);
                  return (
                    <td
                      key={col.key}
                      class={[
                        'px-3 py-2 align-middle',
                        col.align ? ALIGN_CLASSES[col.align] : 'text-left',
                        col.className ?? '',
                      ]
                        .filter(Boolean)
                        .join(' ')}
                    >
                      {cell as JSX.Element}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default Table;
