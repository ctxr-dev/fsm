/**
 * CodeBlock — monospace text rendering with copy / download / search / fullscreen.
 *
 * Used for non-JSON payloads: prompt templates, predicate DSL,
 * generated SQL fragments. Same toolbar contract as JsonViewer but
 * no tree, no chevrons. v1 has no syntax highlighting; a future
 * dynamic-import of Shiki can plug in if a real need arises.
 *
 * Line gutter is auto-enabled for >5 lines.
 *
 * Search highlights matching substrings inline via `<mark>` for visual
 * parity with JsonViewer's match emphasis.
 */

import { useCallback, useMemo, useRef, useState } from 'preact/hooks';
import type { JSX } from 'preact';

import { copyText } from '../lib/clipboard';
import { Sheet } from './Sheet';

export interface CodeBlockProps {
  text: string;
  language?: 'plain' | 'sql' | 'python' | 'markdown' | 'jinja';
  lineNumbers?: boolean;
  maxInlineHeight?: string;
  filename?: string;
  ariaLabel?: string;
  className?: string;
}

function downloadAs(filename: string, text: string, mime = 'text/plain'): void {
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

interface LineProps {
  index: number;
  text: string;
  query: string;
  showGutter: boolean;
}

function Line({ index, text, query, showGutter }: LineProps): JSX.Element {
  // Highlight matches inline. The split-and-rebuild keeps the original
  // whitespace exact (preserves indentation).
  const segments = useMemo(() => {
    if (!query) return [{ text, match: false }];
    const re = new RegExp(escapeRegExp(query), 'gi');
    const out: { text: string; match: boolean }[] = [];
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) != null) {
      if (m.index > last) out.push({ text: text.slice(last, m.index), match: false });
      out.push({ text: m[0], match: true });
      last = m.index + m[0].length;
      if (m[0].length === 0) re.lastIndex += 1; // avoid infinite loop on zero-width
    }
    if (last < text.length) out.push({ text: text.slice(last), match: false });
    return out;
  }, [text, query]);

  return (
    <div class="cb-line flex">
      {showGutter && (
        <span
          class="cb-gutter select-none w-10 text-right pr-2 text-slate-400 dark:text-slate-500"
          aria-hidden="true"
        >
          {index + 1}
        </span>
      )}
      <span class="cb-text flex-1 whitespace-pre break-all">
        {segments.map((s, i) =>
          s.match ? (
            <mark key={i} class="jv-match bg-amber-200 dark:bg-amber-700/60 rounded-sm">
              {s.text}
            </mark>
          ) : (
            <span key={i}>{s.text}</span>
          ),
        )}
      </span>
    </div>
  );
}

export function CodeBlock({
  text,
  language = 'plain',
  lineNumbers,
  maxInlineHeight = 'max-h-64',
  filename,
  ariaLabel = 'Code block',
  className,
}: CodeBlockProps): JSX.Element {
  const [search, setSearch] = useState('');
  const [sheetOpen, setSheetOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  const lines = useMemo(() => text.split('\n'), [text]);
  const showGutter = lineNumbers ?? lines.length > 5;
  const bytes = useMemo(() => new Blob([text]).size, [text]);
  const sizeLabel = bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} kB`;

  const onCopy = useCallback(() => void copyText(text), [text]);
  const onDownload = useCallback(
    () => downloadAs(filename ?? `code.${language}`, text),
    [filename, language, text],
  );
  const onOpenSheet = useCallback(() => setSheetOpen(true), []);
  const onCloseSheet = useCallback(() => setSheetOpen(false), []);

  const onRootKeyDown = useCallback((e: KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') {
      e.preventDefault();
      searchRef.current?.focus();
    }
  }, []);

  const onSearchKey = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      setSearch('');
      (e.target as HTMLInputElement).blur();
    }
  }, []);

  return (
    <section
      role="group"
      aria-label={ariaLabel}
      class={[
        'cb border border-slate-200 dark:border-slate-700 rounded-md',
        'bg-white dark:bg-slate-900/40 text-xs',
        className ?? '',
      ].join(' ')}
      onKeyDown={onRootKeyDown}
    >
      <header class="cb-toolbar flex items-center gap-1 px-2 py-1 border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/60">
        <span class="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400 mr-1">
          {language}
        </span>
        <span class="text-[10px] text-slate-400 dark:text-slate-500">
          {lines.length} lines · {sizeLabel}
        </span>
        <input
          ref={searchRef}
          type="search"
          placeholder="Search"
          value={search}
          onInput={(e) => setSearch((e.target as HTMLInputElement).value)}
          onKeyDown={onSearchKey}
          aria-label="Search inside code"
          class="ml-auto h-6 w-32 rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 text-xs focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        />
        <button
          type="button"
          onClick={onCopy}
          aria-label="Copy code"
          title="Copy"
          class="h-6 px-2 text-xs rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        >
          Copy
        </button>
        <button
          type="button"
          onClick={onDownload}
          aria-label="Download code"
          title="Download"
          class="h-6 px-2 text-xs rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        >
          DL
        </button>
        <button
          type="button"
          onClick={onOpenSheet}
          aria-label="Open in full-screen sheet"
          title="Full-screen"
          class="h-6 px-2 text-xs rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        >
          ↗
        </button>
      </header>
      <pre
        class={[
          'cb-body font-mono leading-snug bg-slate-50 dark:bg-slate-900/40 p-3 overflow-auto m-0',
          maxInlineHeight,
        ].join(' ')}
      >
        {lines.map((line, i) => (
          <Line key={i} index={i} text={line} query={search} showGutter={showGutter} />
        ))}
      </pre>
      <Sheet
        open={sheetOpen}
        onClose={onCloseSheet}
        title={`Code · ${language}`}
        width="right-half"
      >
        <pre class="font-mono text-xs leading-snug whitespace-pre">{text}</pre>
      </Sheet>
    </section>
  );
}

export default CodeBlock;
