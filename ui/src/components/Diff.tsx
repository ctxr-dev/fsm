import type { JSX } from 'preact';
import { useMemo } from 'preact/hooks';

export interface DiffProps {
  before: string;
  after: string;
  /** Show 1-based line numbers in a gutter for both panes. */
  lineNumbers?: boolean;
  /** Optional aria-label for the diff region. */
  label?: string;
  className?: string;
}

type LineKind = 'same' | 'removed' | 'added' | 'empty';

interface DiffLine {
  beforeLine: string | null;
  beforeNumber: number | null;
  afterLine: string | null;
  afterNumber: number | null;
  kind: LineKind;
}

/**
 * Compute a naive line-by-line diff suitable for the W6 MVP — no LCS, no
 * syntax highlighting. Lines that match by index are "same"; otherwise the
 * left side reads `before` and the right side reads `after`.
 *
 * This is intentionally simple. When the API ships structured diffs we will
 * replace this with a real LCS or Myers diff.
 */
function computeDiff(before: string, after: string): DiffLine[] {
  const beforeLines = before.length === 0 ? [] : before.split(/\r?\n/);
  const afterLines = after.length === 0 ? [] : after.split(/\r?\n/);
  const max = Math.max(beforeLines.length, afterLines.length);
  const lines: DiffLine[] = [];
  for (let i = 0; i < max; i += 1) {
    const b = i < beforeLines.length ? beforeLines[i] : null;
    const a = i < afterLines.length ? afterLines[i] : null;
    let kind: LineKind;
    if (b === null && a === null) {
      kind = 'empty';
    } else if (b === null) {
      kind = 'added';
    } else if (a === null) {
      kind = 'removed';
    } else if (b === a) {
      kind = 'same';
    } else {
      // changed on both sides — render as removed/added pair on the same row.
      kind = 'same';
    }
    lines.push({
      beforeLine: b,
      beforeNumber: b !== null ? i + 1 : null,
      afterLine: a,
      afterNumber: a !== null ? i + 1 : null,
      kind:
        b !== null && a !== null && b !== a
          ? 'same' /* will use per-cell classes below */
          : kind,
    });
  }
  return lines;
}

function leftCellClass(line: DiffLine): string {
  if (line.beforeLine === null) {
    return 'bg-slate-50 dark:bg-slate-900/40';
  }
  if (line.afterLine === null) {
    return 'bg-red-50 dark:bg-red-950/40 text-red-900 dark:text-red-200';
  }
  if (line.beforeLine !== line.afterLine) {
    return 'bg-red-50 dark:bg-red-950/40 text-red-900 dark:text-red-200';
  }
  return '';
}

function rightCellClass(line: DiffLine): string {
  if (line.afterLine === null) {
    return 'bg-slate-50 dark:bg-slate-900/40';
  }
  if (line.beforeLine === null) {
    return 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-900 dark:text-emerald-200';
  }
  if (line.beforeLine !== line.afterLine) {
    return 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-900 dark:text-emerald-200';
  }
  return '';
}

/**
 * Diff — side-by-side text diff. No syntax highlighting in the MVP; the goal
 * is readable, predictable output for prompt / response inspection.
 */
export function Diff({
  before,
  after,
  lineNumbers = true,
  label = 'Text diff',
  className = '',
}: DiffProps): JSX.Element {
  const lines = useMemo(() => computeDiff(before, after), [before, after]);

  const composed = [
    'grid grid-cols-2 gap-px overflow-auto rounded-md',
    'border border-slate-200 dark:border-slate-700',
    'bg-slate-200 dark:bg-slate-700',
    'font-mono text-xs',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div class={composed} role="group" aria-label={label}>
      <div class="bg-white dark:bg-slate-800">
        <div class="sticky top-0 z-10 bg-slate-50 dark:bg-slate-900 px-3 py-1.5 text-[0.65rem] uppercase tracking-wide text-slate-500 dark:text-slate-400 border-b border-slate-200 dark:border-slate-700">
          Before
        </div>
        <pre class="m-0 p-0">
          {lines.map((line, i) => (
            <div
              key={`b-${i}`}
              class={`flex items-start ${leftCellClass(line)}`}
            >
              {lineNumbers ? (
                <span
                  class="select-none w-10 shrink-0 px-2 py-0.5 text-right text-slate-400 dark:text-slate-500"
                  aria-hidden="true"
                >
                  {line.beforeNumber ?? ''}
                </span>
              ) : null}
              <span class="whitespace-pre-wrap break-words px-2 py-0.5 flex-1">
                {line.beforeLine ?? ' '}
              </span>
            </div>
          ))}
        </pre>
      </div>
      <div class="bg-white dark:bg-slate-800">
        <div class="sticky top-0 z-10 bg-slate-50 dark:bg-slate-900 px-3 py-1.5 text-[0.65rem] uppercase tracking-wide text-slate-500 dark:text-slate-400 border-b border-slate-200 dark:border-slate-700">
          After
        </div>
        <pre class="m-0 p-0">
          {lines.map((line, i) => (
            <div
              key={`a-${i}`}
              class={`flex items-start ${rightCellClass(line)}`}
            >
              {lineNumbers ? (
                <span
                  class="select-none w-10 shrink-0 px-2 py-0.5 text-right text-slate-400 dark:text-slate-500"
                  aria-hidden="true"
                >
                  {line.afterNumber ?? ''}
                </span>
              ) : null}
              <span class="whitespace-pre-wrap break-words px-2 py-0.5 flex-1">
                {line.afterLine ?? ' '}
              </span>
            </div>
          ))}
        </pre>
      </div>
    </div>
  );
}

export default Diff;
