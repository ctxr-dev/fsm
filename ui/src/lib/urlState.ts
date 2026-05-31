/**
 * Per-route URL state schemas + `useUrlState` hook.
 *
 * Every filter / selection / tab choice / open-Sheet identity in the
 * W18 redesign roundtrips through `?key=value` query parameters so
 * the view is shareable: send a teammate a link and they land on the
 * exact same panel with the exact same filters.
 *
 * Design:
 *
 *   - A `UrlStateSchema<T>` is a pure {fromQuery, toQuery} pair that
 *     knows how to (de)serialise ONE route's shape against URLSearchParams.
 *   - `useUrlState(schema, initial)` hydrates a Preact signal from
 *     `location.search` on mount, mirrors signal changes back to the
 *     URL via `history.replaceState` (debounced 50ms to coalesce
 *     scroll-driven filter updates), and re-reads on `popstate` so
 *     browser-back works as expected.
 *
 * Why a hook rather than putting URL logic directly in each route:
 * the cmd palette (W18c) needs to construct deep links to any route
 * without re-implementing per-route formatting. Centralising the
 * schemas here gives the palette a `schema.toQuery(state) → '?…'`
 * helper that's guaranteed to match what the route reads back.
 *
 * `replaceState` rather than `pushState`: filter changes are
 * transient navigations within the same route (e.g. selecting a
 * different state in the waterfall). They should NOT pollute the
 * browser back stack — that's reserved for actual route changes
 * (which preact-iso's router owns).
 *
 * Edge cases:
 *
 *   - Browser back: a `popstate` listener re-reads `location.search`
 *     and pushes the parsed state back into the signal.
 *   - Multiple `useUrlState` instances on the same page: each owns
 *     its own subset of params; the schemas should declare disjoint
 *     key namespaces. If two schemas share a key, the second one to
 *     write wins, which is wrong; lint by convention rather than
 *     enforcement.
 *   - SSR (no `window`): the hook returns the initial state and skips
 *     the read/write side effects. Safe to import in a tree-shaken
 *     build for SSG.
 */

import { signal, type Signal, effect } from '@preact/signals';
import { useEffect, useMemo } from 'preact/hooks';

export interface UrlStateSchema<T> {
  /** Parse the current query string into the typed state. */
  fromQuery: (params: URLSearchParams) => T;
  /** Serialise the typed state into a `?…` fragment (or '' if empty). */
  toQuery: (state: T) => string;
}

const SSR = typeof window === 'undefined';

/**
 * Create a schema from a per-key {parse,serialise} map plus an
 * initial. The helper covers the 90% case (each field maps to one
 * query param, primitive or comma-list). Routes with weird needs
 * (compound keys, nested objects) can hand-roll their schema.
 */
export interface KeyMap<T> {
  [K: string]: {
    /** Read this param value from URLSearchParams; return the parsed value or undefined. */
    parse: (raw: string | null) => unknown;
    /** Inverse of parse; return undefined to omit the param. */
    serialise: (value: unknown) => string | undefined;
    /** Which state field this query param maps to. Defaults to the param key. */
    field?: keyof T;
  };
}

export function buildSchema<T extends Record<string, unknown>>(
  initial: T,
  keys: KeyMap<T>,
): UrlStateSchema<T> {
  return {
    fromQuery(params) {
      const next = { ...initial };
      for (const queryKey of Object.keys(keys)) {
        const spec = keys[queryKey];
        const field = (spec.field ?? queryKey) as keyof T;
        const raw = params.get(queryKey);
        const parsed = spec.parse(raw);
        if (parsed !== undefined) {
          (next as Record<string, unknown>)[field as string] = parsed;
        }
      }
      return next;
    },
    toQuery(state) {
      const params = new URLSearchParams();
      for (const queryKey of Object.keys(keys)) {
        const spec = keys[queryKey];
        const field = (spec.field ?? queryKey) as keyof T;
        const ser = spec.serialise((state as Record<string, unknown>)[field as string]);
        if (ser !== undefined && ser !== '') params.set(queryKey, ser);
      }
      const out = params.toString();
      return out ? `?${out}` : '';
    },
  };
}

// Common parse/serialise pairs for one-off use.
export const codecs = {
  string: {
    parse: (raw: string | null) => (raw == null ? undefined : raw),
    serialise: (v: unknown) => (typeof v === 'string' && v.length > 0 ? v : undefined),
  },
  number: {
    parse: (raw: string | null) => {
      if (raw == null) return undefined;
      const n = Number(raw);
      return Number.isFinite(n) ? n : undefined;
    },
    serialise: (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? String(v) : undefined),
  },
  boolean: {
    parse: (raw: string | null) => (raw === '1' || raw === 'true' ? true : raw == null ? undefined : false),
    serialise: (v: unknown) => (v === true ? '1' : undefined),
  },
  /** Comma-separated list of strings; empty list → undefined. */
  csv: {
    parse: (raw: string | null) => {
      if (raw == null) return undefined;
      const list = raw.split(',').map((s) => s.trim()).filter(Boolean);
      return list.length === 0 ? undefined : list;
    },
    serialise: (v: unknown) => {
      if (!Array.isArray(v)) return undefined;
      const list = v.filter((s): s is string => typeof s === 'string' && s.length > 0);
      return list.length === 0 ? undefined : list.join(',');
    },
  },
};

const DEBOUNCE_MS = 50;

/**
 * Bind a Preact signal to URLSearchParams via the supplied schema.
 *
 * Returns the signal. The hook also writes back to the URL whenever
 * the signal changes, debounced 50ms.
 *
 * On mount: reads `window.location.search` once, runs `fromQuery`,
 * mutates the signal to match. If the user navigates back/forward,
 * `popstate` re-runs the read.
 *
 * @example
 *   const filtersSignal = useUrlState(myRouteSchema, defaultFilters);
 *   // ... mutate filtersSignal.value freely; URL stays in sync.
 */
export function useUrlState<T>(
  schema: UrlStateSchema<T>,
  initial: T,
): Signal<T> {
  const state = useMemo(() => signal<T>(initial), []);

  // Initial read from URL.
  useEffect(() => {
    if (SSR) return undefined;
    state.value = schema.fromQuery(new URLSearchParams(window.location.search));
    const onPop = () => {
      state.value = schema.fromQuery(new URLSearchParams(window.location.search));
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
    // schema + initial are intentionally NOT in deps: a new schema
    // instance on every render would loop. Callers must pass a stable
    // schema; the typed signature reminds them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Debounced write back to URL.
  useEffect(() => {
    if (SSR) return undefined;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const dispose = effect(() => {
      const next = schema.toQuery(state.value);
      if (timer != null) clearTimeout(timer);
      timer = setTimeout(() => {
        const url = `${window.location.pathname}${next}${window.location.hash}`;
        window.history.replaceState(window.history.state, '', url);
      }, DEBOUNCE_MS);
    });
    return () => {
      if (timer != null) clearTimeout(timer);
      dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return state;
}
