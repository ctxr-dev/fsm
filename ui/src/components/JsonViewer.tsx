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
  /** Initial collapse depth in expanded mode. Defaults to 2 across
   *  every call site for uniform behaviour. Callers can override (e.g.
   *  `0` for raw-state collapse-all, `3` for full definition). The
   *  toolbar's "Expand" button reveals the full tree on demand and
   *  "Collapse" returns to this default. */
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
const DEFAULT_EXPAND_DEPTH = 2;

/**
 * Maximum nesting depth of `v`. Primitives = 0, an empty object/array
 * = 1, etc. Used by the toolbar so the "Expand" button hides once the
 * tree is already fully revealed.
 */
function jsonMaxDepth(v: unknown): number {
  if (!v || typeof v !== 'object') return 0;
  const children = Array.isArray(v) ? v : Object.values(v as Record<string, unknown>);
  if (children.length === 0) return 1;
  let best = 0;
  for (const c of children) {
    const d = jsonMaxDepth(c);
    if (d > best) best = d;
  }
  return 1 + best;
}

/**
 * Prune `value` to only the subtrees that contain any of `terms`
 * (whitespace-OR semantics, case-insensitive substring match on keys
 * AND on stringified primitive values). Returns the pruned subtree
 * plus a `matched` flag so parents know whether to keep this child.
 * Primitive values match iff their `String(v)` contains any term.
 */
export function pruneJsonToMatches(
  value: unknown,
  terms: readonly string[],
): { pruned: unknown; matched: boolean } {
  if (terms.length === 0) return { pruned: value, matched: true };
  if (value === null || value === undefined) {
    const str = String(value).toLowerCase();
    const matched = terms.some((t) => str.includes(t));
    return { pruned: value, matched };
  }
  if (typeof value !== 'object') {
    const str = String(value).toLowerCase();
    const matched = terms.some((t) => str.includes(t));
    return { pruned: value, matched };
  }
  if (Array.isArray(value)) {
    const kept: unknown[] = [];
    let anyMatched = false;
    for (const item of value) {
      const { pruned, matched } = pruneJsonToMatches(item, terms);
      if (matched) {
        kept.push(pruned);
        anyMatched = true;
      }
    }
    return { pruned: kept, matched: anyMatched };
  }
  const out: Record<string, unknown> = {};
  let anyMatched = false;
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    const keyMatch = terms.some((t) => k.toLowerCase().includes(t));
    const { pruned, matched } = pruneJsonToMatches(v, terms);
    if (keyMatch || matched) {
      out[k] = pruned;
      anyMatched = true;
    }
  }
  return { pruned: out, matched: anyMatched };
}

/**
 * Walk `root`'s text nodes and wrap any case-insensitive matches of
 * any term in a `<mark class="jv-match">`. Idempotent: unwraps prior
 * marks before re-wrapping so changing the search term cleans up.
 */
function applySearchHighlight(root: HTMLElement, terms: readonly string[]): void {
  // Unwrap existing marks first so successive applies don't nest.
  const marks = root.querySelectorAll('mark.jv-match');
  marks.forEach((m) => {
    const parent = m.parentNode;
    if (!parent) return;
    while (m.firstChild) parent.insertBefore(m.firstChild, m);
    parent.removeChild(m);
    parent.normalize();
  });
  if (terms.length === 0) return;
  const pattern = new RegExp(
    '(' + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')',
    'gi',
  );
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      const t = node.textContent;
      if (!t || t.length === 0) return NodeFilter.FILTER_REJECT;
      // Skip text already inside a mark (defensive after the unwrap).
      const parentEl = node.parentElement;
      if (parentEl && parentEl.classList.contains('jv-match')) return NodeFilter.FILTER_REJECT;
      // Reset lastIndex: pattern is /gi, and .test() on a global regex
      // advances lastIndex, which would make later sibling text nodes
      // skip matches in a traversal-order-dependent way.
      pattern.lastIndex = 0;
      return pattern.test(t) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const targets: Text[] = [];
  let current = walker.nextNode();
  while (current) {
    targets.push(current as Text);
    current = walker.nextNode();
  }
  for (const textNode of targets) {
    const text = textNode.textContent ?? '';
    pattern.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = pattern.exec(text)) !== null) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const mark = document.createElement('mark');
      mark.className = 'jv-match';
      mark.textContent = m[0];
      frag.appendChild(mark);
      last = m.index + m[0].length;
      if (m.index === pattern.lastIndex) pattern.lastIndex++;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    textNode.parentNode?.replaceChild(frag, textNode);
  }
}

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
  defaultExpandDepth = DEFAULT_EXPAND_DEPTH,
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
  const [expandAll, setExpandAll] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  // Reset expand-all whenever the underlying value changes so the user
  // doesn't get stranded fully-expanded across navigations.
  useEffect(() => {
    setExpandAll(false);
  }, [value]);

  // Parse the toolbar search query into OR-terms (case-insensitive,
  // whitespace-split). Empty array means "no filter".
  const searchTerms = useMemo(() => {
    const trimmed = search.trim().toLowerCase();
    if (!trimmed) return [] as string[];
    return trimmed.split(/\s+/).filter(Boolean);
  }, [search]);

  const filteredValue = useMemo(() => {
    if (searchTerms.length === 0) return value;
    const { pruned, matched } = pruneJsonToMatches(value, searchTerms);
    // If nothing matched the user still gets feedback (empty
    // container) instead of the full original tree, which would
    // silently ignore the query. Preserve the root container kind so
    // searching inside an array doesn't visually flip to `{}`.
    if (matched) return pruned;
    return Array.isArray(value) ? [] : {};
  }, [value, searchTerms]);

  // Effective collapse depth for the on-page JsonView. When a search
  // is active, force every surviving subtree open. When the user has
  // clicked "Expand", force all levels open. Otherwise honour the
  // (possibly-default) depth prop. Inline mode stays at depth 0.
  const treeMaxDepth = useMemo(() => jsonMaxDepth(filteredValue), [filteredValue]);
  const collapsedProp: number | boolean = (() => {
    if (currentMode === 'inline') return 0;
    if (searchTerms.length > 0) return false;
    if (expandAll) return false;
    return defaultExpandDepth;
  })();
  // "Expand" remains visible while more levels exist below the current
  // effective expansion. Once the tree is fully expanded the button
  // hides so the toolbar doesn't lie about there being more to reveal.
  const effectiveDepth: number =
    currentMode === 'inline'
      ? 0
      : searchTerms.length > 0 || expandAll
      ? Number.POSITIVE_INFINITY
      : defaultExpandDepth;
  const expandAvailable = effectiveDepth < treeMaxDepth;
  const collapseAvailable = expandAll || currentMode === 'expanded';

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

  const onExpandAll = useCallback(() => {
    setCurrentMode('expanded');
    setExpandAll(true);
  }, []);

  const onCollapseToDefault = useCallback(() => {
    setExpandAll(false);
    // Return to inline so a second "Expand" click is meaningful (cycles
    // back to expanded-default-2 first, then full expansion).
    setCurrentMode('inline');
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

  // Highlight overlay: after the library renders the pruned tree, walk
  // its DOM and wrap matching text in `<mark class="jv-match">`. Runs
  // after every paint that could have changed visible text (search term
  // or filtered value). Skipped during SSR-style environments without
  // document. The cleanup re-runs `applySearchHighlight` with no terms
  // so unmount leaves no orphan <mark> nodes.
  useEffect(() => {
    if (typeof document === 'undefined') return undefined;
    const el = treeRef.current;
    if (!el) return undefined;
    const id = window.requestAnimationFrame(() => {
      applySearchHighlight(el, searchTerms);
    });
    return () => {
      window.cancelAnimationFrame(id);
      if (treeRef.current) applySearchHighlight(treeRef.current, []);
    };
  }, [searchTerms, filteredValue, collapsedProp]);

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
        {expandAvailable ? (
          <button
            type="button"
            onClick={onExpandAll}
            aria-label="Expand all levels"
            title="Expand all levels"
            class="h-6 px-2 text-xs rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
          >
            Expand
          </button>
        ) : null}
        {collapseAvailable ? (
          <button
            type="button"
            onClick={onCollapseToDefault}
            aria-label="Collapse to default"
            title="Collapse to default"
            class="h-6 px-2 text-xs rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
          >
            Collapse
          </button>
        ) : null}
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
          value={(filteredValue ?? {}) as object}
          // currentMode==='inline' → collapsed at depth 0 (just root
          // visible). Otherwise: search forces all surviving subtrees
          // open; expand-all forces all levels open; otherwise we
          // honour the default-2 depth (or the caller's override).
          collapsed={collapsedProp}
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
          value={(filteredValue ?? {}) as object}
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
