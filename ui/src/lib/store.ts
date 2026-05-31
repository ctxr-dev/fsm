/**
 * Process-wide application state, backed by ``@preact/signals``.
 *
 * Signals — not Context, not Zustand, not Redux — are the right choice
 * here because:
 *
 * * The dashboard's "live" surface (runs list, selected run, SSE
 *   connection status, recent event tape) is read in many leaves and
 *   written from a small number of choke points (the API client, the
 *   :class:`EventStream` handler, the router). Signals collapse the
 *   provider tree and the selector boilerplate to a single import.
 * * Preact + ``@preact/preset-vite`` already wires the JSX runtime so
 *   ``signal.value`` accesses inside a component re-render only that
 *   component when the value changes. No memoisation gymnastics.
 *
 * Cap on :data:`eventLog`
 * -----------------------
 *
 * The event tape is bounded to the most-recent 200 events. The SSE
 * stream is push-driven and a busy run can fire dozens of events per
 * second; an unbounded list would balloon the JSON heap and stutter
 * the tape view (Preact has to diff every row). 200 is a deliberately
 * round number — large enough to show the last few state transitions
 * plus their fan-out, small enough that diffing is sub-millisecond
 * even on a phone. Callers that need the full journal read
 * ``GET /runs/{id}/events`` from :class:`ApiClient` directly.
 */

import { signal, type Signal, effect } from '@preact/signals';

import type { ConnectionState } from './sse';
import type { Event as FsmEvent, RunSummary } from './api';

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

/**
 * Max length of :data:`eventLog`.
 *
 * Exported so tests can assert the trim behaviour without hard-coding
 * the magic number in two places.
 */
export const EVENT_LOG_CAP = 200;

/** Maximum entries kept in ``recentRuns`` / ``recentSpecs`` / ``recentQueries``. */
export const RECENT_CAP = 30;

/** Maximum entries kept in the ``notifications`` queue. */
export const NOTIFICATIONS_CAP = 50;

/** localStorage key for persisted preferences (W18a). */
const PREFS_STORAGE_KEY = 'fsm-ui:prefs';

// ---------------------------------------------------------------------------
// W18a types (chrome + persistence)
// ---------------------------------------------------------------------------

export type DensityMode = 'compact' | 'comfortable' | 'spacious';
export type ThemeMode = 'auto' | 'light' | 'dark';

export interface SheetEntry {
  /** Stable id; used as the keyed-render key and the URL fragment marker. */
  id: string;
  /** Visible title in the sheet's header. */
  title: string;
  /** Width preset; the sheet header renders a cycle button. */
  width?: 'right-third' | 'right-half' | 'fullscreen';
  /** Sheet body. Arbitrary VNode-shaped value; typed `unknown` here to keep
   *  store.ts free of preact runtime imports for tree-shaking. */
  content: unknown;
  /** Optional onClose hook. Sheet host calls this BEFORE popping the stack. */
  onClose?: () => void;
  /** When set, the top sheet's identity mirrors to ``?sheet=<id>``. */
  urlFragment?: string;
  /** Pin: when true, Esc does NOT close this sheet (Brief rail uses it). */
  pinned?: boolean;
}

export interface RunRecency {
  id: string;
  status: string;
  spec?: string;
  lastSeenAt: string; // ISO 8601 UTC
}

export interface SpecRecency {
  slug: string;
  version: number;
  lastSeenAt: string; // ISO 8601 UTC
}

export interface NotificationEntry {
  id: string;
  kind: string;
  title: string;
  body?: string;
  runId?: string;
  timestamp: string; // ISO 8601 UTC
  read: boolean;
}

export interface SseSubInfo {
  consumerName: string;
  url: string;
  state: ConnectionState;
  lastFrameAt: string | null;
  frameCount: number;
  reconnectCount: number;
}

// ---------------------------------------------------------------------------
// Signals — the canonical app state
// ---------------------------------------------------------------------------

/**
 * Runs grouped by their ``status`` field, keyed by the status string
 * (``"running"``, ``"paused"``, ``"completed"``, …).
 *
 * The router-level loader populates this signal after every
 * ``listRuns`` call; per-status leaf views read the slice they care
 * about. Keying by status (vs. a flat list with a per-component
 * ``.filter``) keeps each list view's read O(1) and the diff bounded
 * to its own group on update.
 */
export const runsByStatus: Signal<Record<string, RunSummary[]>> = signal<
  Record<string, RunSummary[]>
>({});

/**
 * Currently-selected run id, or ``null`` when no run is selected.
 *
 * Held in a signal (rather than the URL alone) so background views
 * — the live tape pill, the topology badge — can react to selection
 * changes without re-parsing ``window.location`` on every render.
 * The router keeps this signal in sync with the URL.
 */
export const selectedRunId: Signal<string | null> = signal<string | null>(null);

/**
 * SSE connection status surfaced by :class:`EventStream`.
 *
 * Mirrors the stream's own internal signal so the dashboard chrome
 * can render a status pill without holding a reference to the
 * :class:`EventStream` instance itself. The app shell wires the two
 * together at boot.
 *
 * Defaults to ``'connecting'`` because the stream opens on construction
 * — there is no "not yet started" state in this UI.
 */
export const connectionState: Signal<ConnectionState> = signal<ConnectionState>(
  'connecting',
);

/**
 * Rolling tape of the most-recent SSE events (cap: :data:`EVENT_LOG_CAP`).
 *
 * Newest events appended at the tail; the head is dropped when the cap
 * is exceeded. Powers the "live activity" strip on the dashboard and
 * the per-run mini-tape in the run detail view.
 */
export const eventLog: Signal<FsmEvent[]> = signal<FsmEvent[]>([]);

// ---------------------------------------------------------------------------
// Mutators
// ---------------------------------------------------------------------------

/**
 * Append ``event`` to :data:`eventLog`, dropping the head when the cap
 * is exceeded.
 *
 * We allocate a fresh array (rather than mutating in place) so the
 * signal sees an identity change and notifies subscribers. The slice
 * keeps at most ``EVENT_LOG_CAP - 1`` of the existing tail so the
 * post-append length is exactly :data:`EVENT_LOG_CAP`.
 */
export function appendEvent(event: FsmEvent): void {
  const tail = eventLog.value.slice(-(EVENT_LOG_CAP - 1));
  eventLog.value = [...tail, event];
}

/**
 * Replace the whole :data:`runsByStatus` map with ``next``.
 *
 * Exposed as a named helper so the call site reads as intent
 * (``setRunsByStatus(grouped)``) rather than a raw signal write — the
 * router's data-loader uses this after every ``listRuns`` refresh.
 */
export function setRunsByStatus(next: Record<string, RunSummary[]>): void {
  runsByStatus.value = next;
}

/**
 * Update :data:`selectedRunId` from the router.
 *
 * Accepting ``null`` makes "no selection" a first-class state — the
 * dashboard's empty hero renders against ``selectedRunId.value ===
 * null``.
 */
export function setSelectedRunId(id: string | null): void {
  selectedRunId.value = id;
}

/**
 * Mirror :class:`EventStream.connectionState` into :data:`connectionState`.
 *
 * Wired in the app shell:
 *
 * ```ts
 * effect(() => setConnectionState(stream.connectionState.value));
 * ```
 *
 * Kept as a helper rather than inlined so the wiring intent is obvious
 * and the call site reads as a one-liner.
 */
export function setConnectionState(state: ConnectionState): void {
  connectionState.value = state;
}

/**
 * Drop every entry from :data:`eventLog`.
 *
 * Used when the user navigates between runs — the old tape is no
 * longer relevant and would otherwise mix events from the previous
 * selection with the new one.
 */
export function clearEventLog(): void {
  eventLog.value = [];
}

// ---------------------------------------------------------------------------
// W18a: persisted preference signals
// ---------------------------------------------------------------------------

export const densityMode: Signal<DensityMode> = signal<DensityMode>('comfortable');
export const theme: Signal<ThemeMode> = signal<ThemeMode>('auto');
export const urlStateEnabled: Signal<boolean> = signal<boolean>(true);

export const recentRuns: Signal<RunRecency[]> = signal<RunRecency[]>([]);
export const recentSpecs: Signal<SpecRecency[]> = signal<SpecRecency[]>([]);
export const recentQueries: Signal<string[]> = signal<string[]>([]);

// ---------------------------------------------------------------------------
// W18a: chrome / transient (NOT persisted)
// ---------------------------------------------------------------------------

export const commandPaletteOpen: Signal<boolean> = signal<boolean>(false);
export const commandPaletteSeed: Signal<string> = signal<string>('');
export const keyboardHelpOpen: Signal<boolean> = signal<boolean>(false);
export const notificationCentreOpen: Signal<boolean> = signal<boolean>(false);

/** Stack of open sheets. Top of the stack is the rightmost / topmost rendered. */
export const sheetStack: Signal<SheetEntry[]> = signal<SheetEntry[]>([]);

export const notifications: Signal<NotificationEntry[]> = signal<NotificationEntry[]>([]);

/** Map of active SSE subscriptions for the Settings / Topology diagnostics view. */
export const sseSubscriptions: Signal<Map<string, SseSubInfo>> = signal<
  Map<string, SseSubInfo>
>(new Map());

/** Compare-context: cmd palette + run-detail header surface a Compare action when set. */
export const runComparisonContext: Signal<{ a: string; b: string | null } | null> = signal<{
  a: string;
  b: string | null;
} | null>(null);

// ---------------------------------------------------------------------------
// W18a mutators
// ---------------------------------------------------------------------------

export function openSheet(entry: SheetEntry): void {
  sheetStack.value = [...sheetStack.value, entry];
}

export function closeTopSheet(): void {
  const stack = sheetStack.value;
  if (stack.length === 0) return;
  const top = stack[stack.length - 1];
  top.onClose?.();
  sheetStack.value = stack.slice(0, -1);
}

export function closeSheet(id: string): void {
  const stack = sheetStack.value;
  const idx = stack.findIndex((e) => e.id === id);
  if (idx === -1) return;
  stack[idx].onClose?.();
  sheetStack.value = stack.filter((_, i) => i !== idx);
}

export function clearSheets(): void {
  for (const entry of sheetStack.value) entry.onClose?.();
  sheetStack.value = [];
}

export function rememberRun(entry: RunRecency): void {
  const existing = recentRuns.value.filter((r) => r.id !== entry.id);
  const next = [entry, ...existing].slice(0, RECENT_CAP);
  recentRuns.value = next;
}

export function rememberSpec(entry: SpecRecency): void {
  const existing = recentSpecs.value.filter(
    (s) => !(s.slug === entry.slug && s.version === entry.version),
  );
  const next = [entry, ...existing].slice(0, RECENT_CAP);
  recentSpecs.value = next;
}

export function rememberQuery(q: string): void {
  if (!q.trim()) return;
  const existing = recentQueries.value.filter((existing) => existing !== q);
  const next = [q, ...existing].slice(0, RECENT_CAP);
  recentQueries.value = next;
}

export function pushNotification(entry: NotificationEntry): void {
  const next = [entry, ...notifications.value].slice(0, NOTIFICATIONS_CAP);
  notifications.value = next;
}

export function markAllNotificationsRead(): void {
  if (notifications.value.every((n) => n.read)) return;
  notifications.value = notifications.value.map((n) => ({ ...n, read: true }));
}

// ---------------------------------------------------------------------------
// W18a: persistence
// ---------------------------------------------------------------------------

interface PersistedPrefs {
  densityMode: DensityMode;
  theme: ThemeMode;
  urlStateEnabled: boolean;
  recentRuns: RunRecency[];
  recentSpecs: SpecRecency[];
  recentQueries: string[];
}

const VALID_DENSITIES: ReadonlySet<DensityMode> = new Set(['compact', 'comfortable', 'spacious']);
const VALID_THEMES: ReadonlySet<ThemeMode> = new Set(['auto', 'light', 'dark']);

/**
 * Read persisted prefs from localStorage. Defended per-field so a
 * corrupted entry doesn't blow up the app on boot. Idempotent.
 * SSR-safe: returns early if `window` is undefined.
 */
export function loadStoredPrefs(): void {
  if (typeof window === 'undefined') return;
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(PREFS_STORAGE_KEY);
  } catch {
    return;
  }
  if (!raw) return;
  let parsed: Partial<PersistedPrefs>;
  try {
    parsed = JSON.parse(raw) as Partial<PersistedPrefs>;
  } catch {
    return;
  }
  if (parsed.densityMode && VALID_DENSITIES.has(parsed.densityMode)) {
    densityMode.value = parsed.densityMode;
  }
  if (parsed.theme && VALID_THEMES.has(parsed.theme)) {
    theme.value = parsed.theme;
  }
  if (typeof parsed.urlStateEnabled === 'boolean') {
    urlStateEnabled.value = parsed.urlStateEnabled;
  }
  if (Array.isArray(parsed.recentRuns)) {
    recentRuns.value = parsed.recentRuns
      .filter(
        (r): r is RunRecency =>
          !!r &&
          typeof r.id === 'string' &&
          typeof r.status === 'string' &&
          typeof r.lastSeenAt === 'string',
      )
      .slice(0, RECENT_CAP);
  }
  if (Array.isArray(parsed.recentSpecs)) {
    recentSpecs.value = parsed.recentSpecs
      .filter(
        (s): s is SpecRecency =>
          !!s &&
          typeof s.slug === 'string' &&
          typeof s.version === 'number' &&
          typeof s.lastSeenAt === 'string',
      )
      .slice(0, RECENT_CAP);
  }
  if (Array.isArray(parsed.recentQueries)) {
    recentQueries.value = parsed.recentQueries
      .filter((q): q is string => typeof q === 'string')
      .slice(0, RECENT_CAP);
  }
}

let _persistenceWired = false;

/**
 * Wire up the debounced persistence effect. Called once from
 * main.tsx. Idempotent — a second call is a no-op so tests can
 * exercise the boot path repeatedly.
 */
export function wirePrefsPersistence(): void {
  if (_persistenceWired) return;
  _persistenceWired = true;
  if (typeof window === 'undefined') return;
  let timer: ReturnType<typeof setTimeout> | null = null;
  effect(() => {
    const snapshot: PersistedPrefs = {
      densityMode: densityMode.value,
      theme: theme.value,
      urlStateEnabled: urlStateEnabled.value,
      recentRuns: recentRuns.value,
      recentSpecs: recentSpecs.value,
      recentQueries: recentQueries.value,
    };
    if (timer != null) clearTimeout(timer);
    timer = setTimeout(() => {
      try {
        window.localStorage.setItem(PREFS_STORAGE_KEY, JSON.stringify(snapshot));
      } catch {
        // Quota exceeded / disabled storage — silently drop.
      }
    }, 200);
  });
}

/** Test-only hook: reset the persistence wiring flag between tests. */
export function _resetPersistenceWiring(): void {
  _persistenceWired = false;
}

/** Test-only hook: clear persisted prefs from storage. */
export function _clearStoredPrefs(): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(PREFS_STORAGE_KEY);
  } catch {
    // ignore
  }
}
