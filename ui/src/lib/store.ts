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

import { signal, type Signal } from '@preact/signals';

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
