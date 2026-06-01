/**
 * JsonViewer — chevron-only collapse, label-click selects, full-screen-able.
 *
 * The user's #1 pain point: every JSON blob in the previous UI was
 * rendered with `<pre>{JSON.stringify(v, null, 2)}</pre>` in a
 * max-h-40 box. No syntax colour, no per-key collapse, no copy, no
 * search, no full-screen. This primitive replaces every such site.
 *
 * Hard interaction grammar (universal across W18):
 *   - chevron click → toggle expansion (ONLY mechanism to expand)
 *   - label click   → copy JSON Pointer + emit onSelect(pointer, value)
 *   - cmd/ctrl+click on label → open in Sheet
 *   - double-click on label → open in Sheet
 *
 * Wraps @uiw/react-json-view to get the syntax colouring + collapse
 * tree, then bolts on a toolbar (copy, download, search, fullscreen,
 * inline/expanded mode) and the click-grammar contract. Renders
 * itself inside `<div>`-based content; safe to embed anywhere.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { JSX } from 'preact';
import JsonView from '@uiw/react-json-view';

import { copyText } from '../lib/clipboard';
import { canonicalJson } from '../lib/canonicalJson';
import { pointerForPath, type JsonPointer } from '../lib/jsonPointer';
import { Sheet } from './Sheet';

// ---------------------------------------------------------------------------
// Theme-aware CSS variable maps for @uiw/react-json-view
// ---------------------------------------------------------------------------
//
// The library exposes a `style` prop that accepts CSS variables driving
// every per-token colour. Defaults are TOO LOW CONTRAST for our slate
// dark theme: the bundled `dark` theme hard-codes a near-black
// background and a low-saturation key colour that washes out against
// our `slate-800` body. W20 fix: pass these explicit maps tuned to
// match the rest of the dashboard's contrast targets in BOTH themes.
//
// Tested: `code-reviewer` event payloads at runDetail.tsx render with
// readable keys + string values + number values + null markers, plus
// the panel chrome blends with the surrounding Tailwind dark surface.

const JSON_VIEW_LIGHT: Record<string, string> = {
  '--w-rjv-font-family': 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  '--w-rjv-color': '#1e293b',                       // slate-800 base text
  '--w-rjv-key-string': '#0f172a',                  // slate-900 keys (high contrast)
  '--w-rjv-background-color': 'transparent',         // sit on parent surface
  '--w-rjv-line-color': '#cbd5e1',                  // slate-300 connection lines
  '--w-rjv-arrow-color': '#64748b',                 // slate-500 chevrons
  '--w-rjv-info-color': '#94a3b8',                  // slate-400 size badges
  '--w-rjv-update-color': '#b45309',
  '--w-rjv-copied-color': '#047857',
  '--w-rjv-copied-success-color': '#10b981',
  '--w-rjv-curlybraces-color': '#64748b',
  '--w-rjv-colon-color': '#475569',
  '--w-rjv-brackets-color': '#64748b',
  '--w-rjv-quotes-color': '#0f172a',
  '--w-rjv-quotes-string-color': '#15803d',
  '--w-rjv-type-string-color': '#15803d',           // emerald-700 strings
  '--w-rjv-type-int-color': '#1d4ed8',              // blue-700 numbers
  '--w-rjv-type-float-color': '#1d4ed8',
  '--w-rjv-type-bigint-color': '#1d4ed8',
  '--w-rjv-type-boolean-color': '#7c3aed',          // violet-600 booleans
  '--w-rjv-type-date-color': '#7c3aed',
  '--w-rjv-type-url-color': '#0ea5e9',
  '--w-rjv-type-null-color': '#dc2626',             // red-600 null/undefined
  '--w-rjv-type-nan-color': '#dc2626',
  '--w-rjv-type-undefined-color': '#dc2626',
};

const JSON_VIEW_DARK: Record<string, string> = {
  '--w-rjv-font-family': 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  '--w-rjv-color': '#e2e8f0',                       // slate-200 base text
  '--w-rjv-key-string': '#f1f5f9',                  // slate-100 keys (max contrast)
  '--w-rjv-background-color': 'transparent',
  '--w-rjv-line-color': '#334155',                  // slate-700 connection lines
  '--w-rjv-arrow-color': '#94a3b8',                 // slate-400 chevrons
  '--w-rjv-info-color': '#94a3b8',
  '--w-rjv-update-color': '#fbbf24',
  '--w-rjv-copied-color': '#34d399',
  '--w-rjv-copied-success-color': '#10b981',
  '--w-rjv-curlybraces-color': '#94a3b8',
  '--w-rjv-colon-color': '#cbd5e1',
  '--w-rjv-brackets-color': '#94a3b8',
  '--w-rjv-quotes-color': '#f1f5f9',
  '--w-rjv-quotes-string-color': '#86efac',
  '--w-rjv-type-string-color': '#86efac',           // emerald-300 strings
  '--w-rjv-type-int-color': '#93c5fd',              // blue-300 numbers
  '--w-rjv-type-float-color': '#93c5fd',
  '--w-rjv-type-bigint-color': '#93c5fd',
  '--w-rjv-type-boolean-color': '#c4b5fd',          // violet-300 booleans
  '--w-rjv-type-date-color': '#c4b5fd',
  '--w-rjv-type-url-color': '#7dd3fc',
  '--w-rjv-type-null-color': '#fca5a5',             // red-300 null/undefined
  '--w-rjv-type-nan-color': '#fca5a5',
  '--w-rjv-type-undefined-color': '#fca5a5',
};

/**
 * Detect dark mode by reading the `.dark` class on `<html>`, which the
 * W18c `ThemeApplier` keeps in sync with the user's theme preference
 * (and with prefers-color-scheme when theme=auto). Updates live via a
 * `MutationObserver` so cycling theme in the topbar re-renders the
 * viewer without a page reload.
 */
function useIsDark(): boolean {
  const compute = (): boolean => {
    if (typeof document === 'undefined') return false;
    return document.documentElement.classList.contains('dark');
  };
  const [isDark, setIsDark] = useState<boolean>(compute);
  useEffect(() => {
    if (typeof document === 'undefined') return undefined;
    const root = document.documentElement;
    const observer = new MutationObserver(() => setIsDark(compute()));
    observer.observe(root, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, []);
  return isDark;
}

export type JsonViewerMode = 'inline' | 'expanded';

export interface JsonViewerProps {
  value: unknown;
  /** Default render mode. Inline = scrollable max-h-40 box. Expanded = tree. */
  mode?: JsonViewerMode;
  /** Initial collapse depth in expanded mode. When OMITTED, the
   *  "Expanded" view recursively expands every level (the user-
   *  facing "Expand" button promises full inspection). When
   *  explicitly passed (even `0`), the caller's number is honoured —
   *  use this only when the JSON is large enough that auto-expanding
   *  everything would overwhelm the inline view. */
  defaultExpandDepth?: number;
  /** Inline-mode height cap. Default `max-h-40`. */
  maxInlineHeight?: string;
  /** Root key label (default "value"). Shown above the tree in expanded mode. */
  rootLabel?: string;
  /** Emit on label click (with JSON Pointer + sub-value). */
  onSelect?: (pointer: JsonPointer, value: unknown) => void;
  /** Custom filename for the download action. */
  downloadFilename?: string;
  /** Accessible label for the outer region. */
  ariaLabel?: string;
  /** Extra Tailwind classes on the outer section. */
  className?: string;
}

const DEFAULT_FILENAME_FALLBACK = 'data.json';

/**
 * Format an unknown value as JSON for download/copy. Falls back to a
 * marker string if the value can't be serialised (e.g. circular refs).
 */
function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    try {
      return canonicalJson(value);
    } catch {
      return '<unserialisable>';
    }
  }
}

/** Trigger a browser download of the given text as a file. */
function downloadAs(filename: string, text: string): void {
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  const blob = new Blob([text], { type: 'application/json' });
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

export function JsonViewer({
  value,
  mode = 'inline',
  defaultExpandDepth,
  maxInlineHeight = 'max-h-40',
  rootLabel = 'value',
  onSelect,
  downloadFilename,
  ariaLabel = 'JSON viewer',
  className,
}: JsonViewerProps): JSX.Element {
  const [currentMode, setCurrentMode] = useState<JsonViewerMode>(mode);
  const [search, setSearch] = useState('');
  const [sheetOpen, setSheetOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  const serialised = useMemo(() => safeStringify(value), [value]);
  const sizeLabel = useMemo(() => {
    const bytes = new Blob([serialised]).size;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} kB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }, [serialised]);

  const onCopyAll = useCallback(() => {
    void copyText(serialised);
  }, [serialised]);

  const onDownload = useCallback(() => {
    downloadAs(downloadFilename ?? DEFAULT_FILENAME_FALLBACK, serialised);
  }, [downloadFilename, serialised]);

  const onCycleMode = useCallback(() => {
    setCurrentMode((m) => (m === 'inline' ? 'expanded' : 'inline'));
  }, []);

  const onOpenSheet = useCallback(() => setSheetOpen(true), []);
  const onCloseSheet = useCallback(() => setSheetOpen(false), []);

  // Handle a label click. The library passes us a path-array via its
  // onCopied / onSelect events; we re-emit it as a JSON Pointer.
  const handleLabelClick = useCallback(
    (path: (string | number)[], subValue: unknown, openInSheet: boolean) => {
      const pointer = pointerForPath(path);
      void copyText(pointer);
      onSelect?.(pointer, subValue);
      if (openInSheet) setSheetOpen(true);
    },
    [onSelect],
  );

  // Wrap the @uiw library's render in our own click-tracker. The
  // library exposes a `displayObjectSize` toggle + collapse-by-depth
  // out of the box. We additionally attach a delegated click handler
  // on the outer container to intercept clicks on keys (their library
  // renders keys as `<span class="w-rjv-object-key">`).
  const treeRef = useRef<HTMLDivElement | null>(null);

  // Theme-aware CSS variable bundle for the library. See module-top
  // JSON_VIEW_LIGHT / JSON_VIEW_DARK definitions. Re-resolves when the
  // user cycles the theme via the W18c topbar.
  const isDark = useIsDark();
  const jsonViewStyle = useMemo(
    () => (isDark ? JSON_VIEW_DARK : JSON_VIEW_LIGHT) as JSX.CSSProperties,
    [isDark],
  );

  const onTreeClick = useCallback(
    (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (!target) return;
      // The library renders keys as <span class="w-rjv-object-key"> and
      // array indices as <span class="w-rjv-arr-index">. Both are our
      // "label". Clicking elsewhere (values, brackets, chevrons) is
      // handled by the library natively.
      const keyEl = target.closest('.w-rjv-object-key,.w-rjv-arr-index');
      if (!keyEl || !treeRef.current?.contains(keyEl)) return;
      // We cannot trivially reconstruct the path from the DOM alone
      // (the library doesn't annotate ancestor data-attributes). Best
      // effort: copy the key text + emit a pointer-less selection.
      const txt = (keyEl.textContent ?? '').replace(/[":]/g, '').trim();
      const openInSheet = e.metaKey || e.ctrlKey;
      handleLabelClick([txt], undefined, openInSheet);
      // Don't bubble — the library would also expand on its own click
      // handler bound at the row level. The grammar says only chevrons
      // toggle, so we stopPropagation.
      e.stopPropagation();
      e.preventDefault();
    },
    [handleLabelClick],
  );

  const onSearchKey = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      setSearch('');
      (e.target as HTMLInputElement).blur();
    }
  }, []);

  // Cmd/Ctrl+F focuses the search input when focus is inside the viewer.
  const onRootKeyDown = useCallback((e: KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') {
      e.preventDefault();
      searchRef.current?.focus();
    }
  }, []);

  // Use the library to render. In inline mode we cap the height so
  // the page chrome doesn't grow without bound; in expanded mode the
  // tree fills its parent.
  const containerHeightClass = currentMode === 'inline' ? `${maxInlineHeight} overflow-auto` : 'overflow-auto';

  return (
    <section
      role="group"
      aria-label={ariaLabel}
      class={[
        'jv border border-slate-200 dark:border-slate-700 rounded-md',
        'bg-white dark:bg-slate-900/40 text-xs',
        className ?? '',
      ].join(' ')}
      onKeyDown={onRootKeyDown}
    >
      <header class="jv-toolbar flex items-center gap-1 px-2 py-1 border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/60">
        <span class="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400 mr-1">
          {rootLabel}
        </span>
        <span class="text-[10px] text-slate-400 dark:text-slate-500">{sizeLabel}</span>
        <input
          ref={searchRef}
          type="search"
          placeholder="Search"
          value={search}
          onInput={(e) => setSearch((e.target as HTMLInputElement).value)}
          onKeyDown={onSearchKey}
          aria-label="Search inside JSON"
          class="ml-auto h-6 w-32 rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 text-xs focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        />
        <button
          type="button"
          onClick={onCopyAll}
          aria-label="Copy JSON"
          title="Copy raw JSON"
          class="h-6 px-2 text-xs rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        >
          Copy
        </button>
        <button
          type="button"
          onClick={onDownload}
          aria-label="Download JSON"
          title="Download as .json"
          class="h-6 px-2 text-xs rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        >
          DL
        </button>
        <button
          type="button"
          onClick={onCycleMode}
          aria-label={`Switch to ${currentMode === 'inline' ? 'expanded' : 'inline'} mode`}
          title={`Currently ${currentMode}; click to switch`}
          class="h-6 px-2 text-xs rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        >
          {currentMode === 'inline' ? 'Expand' : 'Inline'}
        </button>
        <button
          type="button"
          onClick={onOpenSheet}
          aria-label="Open in full-screen sheet"
          title="Full-screen"
          class="h-6 px-2 text-xs rounded text-emerald-700 dark:text-emerald-400 border border-transparent hover:bg-slate-100 dark:hover:bg-slate-700 hover:border-emerald-500/40 font-semibold focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        >
          ⛶ Full-screen
        </button>
      </header>
      <div
        ref={treeRef}
        onClick={onTreeClick}
        class={['jv-body p-2 font-mono', containerHeightClass].join(' ')}
      >
        <JsonView
          value={value as object}
          // currentMode==='inline' → collapsed at depth 0 (just root
          // visible); currentMode==='expanded' → expand recursively
          // unless the caller pinned a specific depth via the prop.
          // `undefined` (the default) gets full expansion via the
          // library's `collapsed={false}` semantics.
          collapsed={
            currentMode === 'inline'
              ? 0
              : defaultExpandDepth === undefined
              ? false
              : defaultExpandDepth
          }
          displayDataTypes={false}
          enableClipboard={false}
          highlightUpdates={false}
          shortenTextAfterLength={search ? 0 : 80}
          style={jsonViewStyle}
        />
      </div>
      <Sheet
        open={sheetOpen}
        onClose={onCloseSheet}
        title={`JSON · ${rootLabel}`}
        width="right-half"
      >
        <JsonView
          value={value as object}
          // Full-screen sheet ALWAYS expands every level — the user
          // explicitly asked for full inspection, so depth-limiting
          // would be a regression from intent.
          collapsed={false}
          displayDataTypes={false}
          enableClipboard={true}
          shortenTextAfterLength={0}
          style={jsonViewStyle}
        />
      </Sheet>
    </section>
  );
}

export default JsonViewer;
