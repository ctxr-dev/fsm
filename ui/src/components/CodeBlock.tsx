/**
 * CodeBlock — monospace text rendering with copy / download / search / fullscreen
 *               plus an opt-in Markdown rendered view.
 *
 * Used for non-JSON payloads: prompt templates, predicate DSL,
 * generated SQL fragments. Same toolbar contract as JsonViewer.
 *
 * Markdown view (W21 user-requested): when the content looks like
 * markdown (or the caller passes `language="markdown"`) the toolbar
 * exposes a Rendered / Raw toggle. Rendered mode runs marked +
 * DOMPurify and styles the output with Tailwind's @tailwindcss/
 * typography `prose` classes, giving GitHub-flavored Markdown with
 * proper headings, lists, fenced code blocks, links, tables, etc.
 *
 * Line gutter is auto-enabled for >5 lines (raw mode only).
 *
 * Search highlights matching substrings inline via `<mark>` for visual
 * parity with JsonViewer's match emphasis (raw mode only).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { JSX } from 'preact';
import { marked } from 'marked';
import DOMPurify from 'dompurify';

import { copyText } from '../lib/clipboard';
import { Sheet } from './Sheet';

// Configure marked once at module load. GFM enables tables, task
// lists, strikethrough, auto-link, etc. breaks=true so single newlines
// become <br>, matching GitHub's rendering on issues / PRs / comments
// (which is what most operators expect when they author prompt
// templates). The CommonMark default (breaks=false) collapses single
// newlines into the surrounding paragraph; that's wrong for the
// prompt-as-instructions style the FSM ecosystem uses.
marked.use({
  gfm: true,
  breaks: true,
});

// Heuristic: does the text look like markdown? Used to default the
// view mode when the caller didn't pass `language="markdown"`. This
// keeps the FSM library domain-agnostic — it doesn't NEED to know its
// prompt templates are markdown, the UI just notices when they are.
function looksLikeMarkdown(text: string): boolean {
  if (!text || text.length < 4) return false;
  return (
    /^#{1,6}\s/m.test(text) || // ATX headers
    /^```/m.test(text) || // fenced code blocks
    /\[[^\]]+\]\([^)]+\)/.test(text) || // [link](url)
    /^\s*[-*+]\s+\S/m.test(text) || // unordered lists
    /^\s*\d+\.\s+\S/m.test(text) || // ordered lists
    /\*\*[^*]+\*\*/.test(text) || // **bold**
    /^\s*>\s/m.test(text) // > blockquote
  );
}

/** Render markdown to a safe HTML string. Synchronous marked is fine
 *  for the prompt-template scale we're rendering; DOMPurify strips any
 *  script tags / event handlers / data: URIs / etc. that the upstream
 *  content might contain.
 *
 *  Forbidden tags lock the surface down further: <img>/<picture>/
 *  <video>/<audio>/<source>/<track> can trigger outbound network
 *  requests just by rendering, leaking the operator's IP to whatever
 *  URL the spec author chose. <svg> + <math> have large attack
 *  surfaces around foreignObject / use[href] / etc. <iframe>/<object>/
 *  <embed>/<form>/<link>/<meta>/<base>/<style>/<script> are the usual
 *  active-content vectors. The result is text-only HTML: headers,
 *  paragraphs, lists, tables, blockquotes, code, fences, inline
 *  emphasis, links (still allowed; the user clicks intentionally). */
function renderMarkdown(text: string): string {
  const raw = marked.parse(text, { async: false }) as string;
  // <input> is intentionally NOT forbidden: GFM task lists generate
  // `<input type="checkbox" disabled>` which is benign (read-only, no
  // outbound effect) and stripping it would render `- [x] done` as a
  // bullet with no checkbox at all — a visible regression from the
  // GFM contract callers expect. DOMPurify's default attribute
  // sanitiser still drops onclick / onerror / etc. The other form
  // controls stay forbidden because they invite spec authors to
  // simulate input forms inside a viewer surface.
  return DOMPurify.sanitize(raw, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: [
      'style',
      'script',
      'iframe',
      'object',
      'embed',
      'img',
      'picture',
      'video',
      'audio',
      'source',
      'track',
      'svg',
      'math',
      'form',
      'button',
      'select',
      'textarea',
      'link',
      'meta',
      'base',
    ],
  });
}

export interface CodeBlockProps {
  text: string;
  /** Format hint declared by the consumer (e.g. an FSM Worker's
   *  ``prompt_template_language``). Free-form string so consumers
   *  own the convention; common values include 'markdown', 'jinja',
   *  'plain', 'json'. When omitted or unknown to this component the
   *  body renders as plain monospace (with the markdown heuristic
   *  applying as a courtesy fallback). */
  language?: string;
  lineNumbers?: boolean;
  maxInlineHeight?: string;
  filename?: string;
  ariaLabel?: string;
  className?: string;
  /** Force markdown rendering mode regardless of heuristic detection.
   *  Useful when the caller knows for certain the content is markdown
   *  but doesn't want to declare `language="markdown"` (which would
   *  imply other things about the file type for download / copy). */
  renderMarkdown?: boolean;
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
  renderMarkdown: renderMarkdownProp,
}: CodeBlockProps): JSX.Element {
  // markdownEligible: caller declared language=markdown OR explicitly
  // opted in OR the heuristic says the content looks markdown-ish.
  // When eligible, the toolbar exposes a Raw/Rendered toggle and we
  // default to Rendered (the visually informative view).
  const markdownEligible =
    language === 'markdown' ||
    renderMarkdownProp === true ||
    (renderMarkdownProp !== false && looksLikeMarkdown(text));

  const [view, setView] = useState<'raw' | 'rendered'>(
    markdownEligible ? 'rendered' : 'raw',
  );
  // Sync view back to 'raw' when the content stops being
  // markdown-eligible (e.g. caller swapped the text prop to a plain
  // string). Without this the toggle disappears AND the renderedHtml
  // becomes '' — the body would render an empty Sheet with no way
  // for the user to flip back to raw.
  useEffect(() => {
    if (!markdownEligible && view === 'rendered') setView('raw');
  }, [markdownEligible, view]);
  const [search, setSearch] = useState('');
  const [sheetOpen, setSheetOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  const lines = useMemo(() => text.split('\n'), [text]);
  const showGutter = lineNumbers ?? lines.length > 5;
  const bytes = useMemo(() => new Blob([text]).size, [text]);
  const sizeLabel = bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} kB`;

  // Memoise the rendered HTML. Only compute when the user is actually
  // looking at the rendered view; parsing + sanitising a long prompt
  // every render is wasteful when the user is in raw mode.
  const renderedHtml = useMemo(() => {
    if (!markdownEligible || view !== 'rendered') return '';
    return renderMarkdown(text);
  }, [text, markdownEligible, view]);

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
        {view === 'raw' ? (
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
        ) : (
          <span class="ml-auto" />
        )}
        {markdownEligible ? (
          <button
            type="button"
            onClick={() => setView(view === 'raw' ? 'rendered' : 'raw')}
            aria-label={`Show ${view === 'raw' ? 'rendered' : 'raw'} view`}
            title={`Currently ${view}; click to switch`}
            class="h-6 px-2 text-xs rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
          >
            {view === 'raw' ? 'Rendered' : 'Raw'}
          </button>
        ) : null}
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
          class="h-6 px-2 text-xs rounded text-emerald-700 dark:text-emerald-400 border border-transparent hover:bg-slate-100 dark:hover:bg-slate-700 hover:border-emerald-500/40 font-semibold focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        >
          ⛶ Full-screen
        </button>
      </header>
      {view === 'rendered' ? (
        <div
          class={[
            'cb-body cb-markdown',
            'prose prose-sm dark:prose-invert max-w-none',
            'prose-headings:scroll-mt-4 prose-code:before:hidden prose-code:after:hidden',
            'prose-pre:bg-slate-100 dark:prose-pre:bg-slate-900/60',
            'bg-white dark:bg-slate-900/40 p-3 overflow-auto',
            maxInlineHeight,
          ].join(' ')}
          // eslint-disable-next-line react/no-danger -- output is run through DOMPurify above; script / iframe / event-handler vectors are stripped before reaching the DOM
          dangerouslySetInnerHTML={{ __html: renderedHtml }}
        />
      ) : (
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
      )}
      <Sheet
        open={sheetOpen}
        onClose={onCloseSheet}
        title={`Code · ${language}`}
        width="right-half"
      >
        {view === 'rendered' ? (
          <div
            class={[
              'prose prose-sm dark:prose-invert max-w-none',
              'prose-headings:scroll-mt-4 prose-code:before:hidden prose-code:after:hidden',
              'prose-pre:bg-slate-100 dark:prose-pre:bg-slate-900/60',
              'p-4',
            ].join(' ')}
            // eslint-disable-next-line react/no-danger -- output is run through DOMPurify above; same justification as the inline render path
            dangerouslySetInnerHTML={{ __html: renderedHtml }}
          />
        ) : (
          <pre class="font-mono text-xs leading-snug whitespace-pre p-4">{text}</pre>
        )}
      </Sheet>
    </section>
  );
}

export default CodeBlock;
