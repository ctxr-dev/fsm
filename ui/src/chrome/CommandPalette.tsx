/**
 * Command Palette — Cmd/Ctrl+K universal jump-to.
 *
 * Sources fan out:
 *   - Static actions (always present, context-gated where appropriate)
 *   - Recent runs (from `recentRuns` signal)
 *   - Recent specs (from `recentSpecs`)
 *   - Open runs (from `runsByStatus` if loaded)
 *   - Routes (from `ROUTES` registry)
 *
 * Keyboard: Up/Down navigate, Enter activates, Cmd+Enter opens in new
 * tab, Esc closes. Mounted via the Shell; opens on the global
 * `commandPaletteOpen` signal.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { JSX } from 'preact';

import {
  commandPaletteOpen,
  commandPaletteSeed,
  keyboardHelpOpen,
  recentRuns,
  recentSpecs,
  rememberQuery,
  runsByStatus,
  theme,
} from '../lib/store';
import { ROUTES, type RouteDef } from '../routes';
import { rankFuzzy, type FuzzyHit } from '../lib/fuzzy';

// ---------------------------------------------------------------------------
// Result row model
// ---------------------------------------------------------------------------

type ResultSource = 'action' | 'route' | 'open-run' | 'recent-run' | 'spec';

export interface PaletteResult {
  id: string;
  source: ResultSource;
  primary: string;
  secondary?: string;
  hotkey?: string;
  activate: (modifiers: { newTab: boolean }) => void;
}

// Source-priority weight: higher = ranked first within the same score.
const SOURCE_WEIGHT: Record<ResultSource, number> = {
  action: 1.4,
  'open-run': 1.2,
  'recent-run': 1.1,
  route: 1.0,
  spec: 0.9,
};

// ---------------------------------------------------------------------------
// Source builders (pure; tested separately)
// ---------------------------------------------------------------------------

export interface BuildContext {
  navigate: (path: string, opts?: { newTab?: boolean }) => void;
  currentPath: string;
}

function buildRouteResults(ctx: BuildContext): PaletteResult[] {
  return ROUTES.filter((r: RouteDef) => r.navGroup !== null).map((r) => ({
    id: `route:${r.path}`,
    source: 'route',
    primary: r.label,
    secondary: r.path,
    hotkey: r.shortcut,
    activate: ({ newTab }) => ctx.navigate(r.path, { newTab }),
  }));
}

function buildRecentRunResults(ctx: BuildContext): PaletteResult[] {
  return recentRuns.value.map((r) => ({
    id: `recent-run:${r.id}`,
    source: 'recent-run',
    primary: `Run ${r.id.slice(0, 7)}`,
    secondary: `${r.status}${r.spec ? ' · ' + r.spec : ''}`,
    activate: ({ newTab }) => ctx.navigate(`/runs/${r.id}`, { newTab }),
  }));
}

function buildOpenRunResults(ctx: BuildContext): PaletteResult[] {
  const grouped = runsByStatus.value;
  const open: PaletteResult[] = [];
  for (const status of ['in_progress', 'paused', 'faulted', 'drift_paused']) {
    const runs = grouped[status] ?? [];
    for (const r of runs) {
      open.push({
        id: `open-run:${r.id}`,
        source: 'open-run',
        primary: `Run ${r.id.slice(0, 7)}`,
        secondary: `${r.status} · ${r.fsm_spec_id?.slice(0, 8) ?? ''}`,
        activate: ({ newTab }) => ctx.navigate(`/runs/${r.id}`, { newTab }),
      });
    }
  }
  return open;
}

function buildSpecResults(ctx: BuildContext): PaletteResult[] {
  return recentSpecs.value.map((s) => ({
    id: `spec:${s.slug}:${s.version}`,
    source: 'spec',
    primary: `${s.slug}`,
    secondary: `v${s.version}`,
    activate: ({ newTab }) => ctx.navigate(`/specs?slug=${encodeURIComponent(s.slug)}&version=${s.version}`, { newTab }),
  }));
}

function buildActionResults(ctx: BuildContext): PaletteResult[] {
  const isOnRunDetail = /^\/runs\/[^/]+/.test(ctx.currentPath);
  const actions: PaletteResult[] = [
    {
      id: 'action:open-help',
      source: 'action',
      primary: 'Open keyboard help',
      hotkey: '?',
      activate: () => {
        keyboardHelpOpen.value = true;
      },
    },
    {
      id: 'action:toggle-theme',
      source: 'action',
      primary: 'Cycle theme (auto → light → dark)',
      activate: () => {
        const next = theme.value === 'auto' ? 'light' : theme.value === 'light' ? 'dark' : 'auto';
        theme.value = next;
      },
    },
  ];
  if (isOnRunDetail) {
    actions.push({
      id: 'action:run-compare',
      source: 'action',
      primary: 'Compare this run with another...',
      activate: () => {
        // The Compare picker lives on the run-detail page; for now we
        // surface the action and the page will handle once W18i lands.
      },
    });
  }
  return actions;
}

// Exported for unit tests.
export function buildAllResults(ctx: BuildContext): PaletteResult[] {
  return [
    ...buildActionResults(ctx),
    ...buildOpenRunResults(ctx),
    ...buildRecentRunResults(ctx),
    ...buildSpecResults(ctx),
    ...buildRouteResults(ctx),
  ];
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export interface CommandPaletteProps {
  /** Navigation primitive. Falls back to `window.location.assign` if absent. */
  navigate?: (path: string, opts?: { newTab?: boolean }) => void;
  /** Current pathname (e.g. for context-gated actions). Defaults to location.pathname. */
  currentPath?: string;
}

export function CommandPalette(props: CommandPaletteProps = {}): JSX.Element | null {
  const open = commandPaletteOpen.value;
  const seed = commandPaletteSeed.value;
  const [query, setQuery] = useState(seed);
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Default navigation: SPA-style via popstate so existing app router picks up.
  const navigate = useMemo(() => {
    if (props.navigate) return props.navigate;
    return (path: string, opts?: { newTab?: boolean }) => {
      if (typeof window === 'undefined') return;
      if (opts?.newTab) {
        window.open(path, '_blank', 'noopener,noreferrer');
        return;
      }
      window.history.pushState(null, '', path);
      window.dispatchEvent(new PopStateEvent('popstate'));
    };
  }, [props.navigate]);

  const currentPath = props.currentPath ?? (typeof window !== 'undefined' ? window.location.pathname : '/');

  const ctx: BuildContext = useMemo(() => ({ navigate, currentPath }), [navigate, currentPath]);

  const allResults = useMemo(() => buildAllResults(ctx), [ctx, open]);

  const ranked = useMemo<FuzzyHit<PaletteResult>[]>(() => {
    return rankFuzzy(allResults, query, {
      text: (r) => `${r.primary} ${r.secondary ?? ''}`,
      weight: (r) => SOURCE_WEIGHT[r.source],
    });
  }, [allResults, query]);

  // Reset cursor when query changes.
  useEffect(() => {
    setCursor(0);
  }, [query]);

  // Sync external seed.
  useEffect(() => {
    if (open) setQuery(seed);
  }, [open, seed]);

  // Focus the input when the palette opens.
  useEffect(() => {
    if (!open) return;
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [open]);

  const close = useCallback(() => {
    commandPaletteOpen.value = false;
    commandPaletteSeed.value = '';
    setQuery('');
  }, []);

  const activate = useCallback(
    (index: number, opts: { newTab: boolean }) => {
      const hit = ranked[index];
      if (!hit) return;
      if (query.trim()) rememberQuery(query.trim());
      hit.item.activate(opts);
      close();
    },
    [ranked, query, close],
  );

  const onKey = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        close();
        return;
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setCursor((c) => Math.min(ranked.length - 1, c + 1));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setCursor((c) => Math.max(0, c - 1));
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        activate(cursor, { newTab: e.metaKey || e.ctrlKey });
        return;
      }
    },
    [activate, close, cursor, ranked.length],
  );

  // Global open shortcut (Cmd/Ctrl+K). Mounted always so the user can
  // open it from anywhere.
  useEffect(() => {
    const onGlobal = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        commandPaletteOpen.value = !commandPaletteOpen.value;
      }
    };
    window.addEventListener('keydown', onGlobal);
    return () => window.removeEventListener('keydown', onGlobal);
  }, []);

  if (!open) return null;

  return (
    <div
      class="fixed inset-0 z-[60] flex items-start justify-center pt-24 px-4 bg-slate-900/60 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        role="combobox"
        aria-expanded="true"
        aria-haspopup="listbox"
        aria-controls="cmd-palette-list"
        class="cmd-palette w-full max-w-2xl bg-white dark:bg-slate-800 rounded-lg shadow-2xl border border-slate-200 dark:border-slate-700 flex flex-col max-h-[70vh]"
      >
        <div class="px-3 py-2 border-b border-slate-200 dark:border-slate-700">
          <input
            ref={inputRef}
            type="search"
            value={query}
            onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
            onKeyDown={onKey}
            placeholder="Type a command, run id, spec, or jump-to..."
            aria-label="Command palette search"
            class="w-full h-9 px-2 bg-transparent text-base focus:outline-none placeholder:text-slate-400"
          />
        </div>
        <ul
          id="cmd-palette-list"
          role="listbox"
          aria-label="Palette results"
          class="cmd-palette-list flex-1 overflow-auto py-1"
        >
          {ranked.length === 0 ? (
            <li class="px-4 py-8 text-center text-sm text-slate-500">
              No matches — try a run id, spec slug, or action.
            </li>
          ) : (
            ranked.map((hit, idx) => (
              <li
                key={hit.item.id}
                // aria-selected as an explicit string so a11y linters
                // and screen readers see the precise WAI-ARIA value.
                role="option"
                aria-selected={idx === cursor ? 'true' : 'false'}
                onMouseEnter={() => setCursor(idx)}
                onClick={(e) => activate(idx, { newTab: e.metaKey || e.ctrlKey })}
                class={[
                  'cmd-row flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer',
                  idx === cursor
                    ? 'bg-emerald-50 dark:bg-emerald-900/30 text-slate-900 dark:text-slate-50'
                    : 'text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700/40',
                ].join(' ')}
              >
                <span class="text-[10px] uppercase tracking-wide text-slate-400 w-16 shrink-0">
                  {hit.item.source}
                </span>
                <span class="font-medium truncate">{hit.item.primary}</span>
                {hit.item.secondary ? (
                  <span class="text-xs text-slate-500 truncate">· {hit.item.secondary}</span>
                ) : null}
                {hit.item.hotkey ? (
                  <span class="ml-auto text-[10px] text-slate-400">{hit.item.hotkey}</span>
                ) : null}
              </li>
            ))
          )}
        </ul>
        <footer class="px-3 py-2 border-t border-slate-200 dark:border-slate-700 text-[10px] text-slate-500 flex items-center gap-3">
          <span>↑↓ navigate</span>
          <span>⏎ open</span>
          <span>⌘⏎ open in new tab</span>
          <span>esc close</span>
          <span class="ml-auto">{ranked.length} results</span>
        </footer>
      </div>
    </div>
  );
}

export default CommandPalette;
