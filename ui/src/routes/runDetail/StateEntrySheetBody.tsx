/**
 * Body component for the run-detail state-entry Sheet.
 *
 * Rendered inside :class:`SheetHost` when the operator clicks a node in
 * the run progress graph. The body is a three-tab Tabs composite:
 *
 *   1. "Run values" — flat metadata about the actual entry the run
 *      recorded (state_id, status, entered/exited timestamps, iteration
 *      counter) plus the JsonViewer for the inputs + outputs blobs, and
 *      a brief / cosignature info row when the matching events are
 *      present in the loaded slice.
 *   2. "Spec definition" — mounts the existing :class:`StateInspectorBody`
 *      so the operator sees the same spec-side metadata they would on
 *      the spec route, without leaving the run page.
 *   3. "Events for this state" — a deduped Timeline of events whose
 *      payload references this ``entry_id`` (or whose canonical
 *      ``state_entry_id`` / ``entry_id`` field matches).
 *
 * The component is intentionally stateful at the tab-id level only —
 * every data lookup is a pure derivation from the props, so the same
 * sheet can be re-rendered with fresh event slices without losing the
 * operator's tab selection.
 */

import type { JSX } from 'preact';
import { useMemo, useState } from 'preact/hooks';

import { JsonViewer } from '../../components/JsonViewer';
import { KeyValueTable, type KvRow } from '../../components/KeyValueTable';
import { StateInspectorBody } from '../../components/StateInspectorBody';
import { Tabs, type TabSpec } from '../../components/Tabs';
import { Timeline, type TimelineItem } from '../../components/Timeline';
import { EmptyState } from '../../components/EmptyState';
import { Pill, type PillVariant } from '../../components/Pill';
import type {
  Event as FsmEvent,
  SpecDetail,
  StateNode,
} from '../../lib/api';

export interface StateEntrySheetBodyProps {
  entryId: string;
  runId: string;
  stateTree: StateNode | null;
  /** The full spec record loaded by the run-detail route. ``null`` is
   *  tolerated so the sheet can render the "Run values" tab even when
   *  the spec fetch failed (the "Spec definition" tab falls back to a
   *  short EmptyState in that case). */
  spec: SpecDetail | null;
  /** Recent event slice (the same ``events`` the route already loaded).
   *  We dedupe + filter inside the sheet rather than ask the parent to
   *  pre-filter so the sheet stays self-contained for testing. */
  events: FsmEvent[];
}

/** Tab ids — exported so tests can target them by string instead of
 *  re-implementing the tab list in the test file. */
export const STATE_ENTRY_TAB_IDS = ['run', 'spec', 'events'] as const;
export type StateEntryTabId = (typeof STATE_ENTRY_TAB_IDS)[number];

/** Walk the state tree once to find the entry by id. Returns ``null``
 *  when not found so the caller can render an EmptyState. */
function findEntry(root: StateNode | null, entryId: string): StateNode | null {
  if (!root) return null;
  if (root.entry_id === entryId) return root;
  for (const child of root.children) {
    const hit = findEntry(child, entryId);
    if (hit !== null) return hit;
  }
  return null;
}

/** Find the spec-side state object for ``state_id``. The spec
 *  definition's ``states`` array is the loose JSON shape FsmSpec dumps
 *  on register — we type it loosely so the lookup stays robust if a
 *  future field migration adds keys. */
function findSpecState(
  spec: SpecDetail | null,
  stateId: string,
): Record<string, unknown> | null {
  if (!spec) return null;
  const def = spec.definition as { states?: unknown };
  const states = Array.isArray(def?.states) ? def.states : [];
  for (const s of states) {
    if (s && typeof s === 'object') {
      const obj = s as Record<string, unknown>;
      if (obj.id === stateId) return obj;
    }
  }
  return null;
}

/** Status -> Pill variant — mirrors the run-detail header's palette. */
function variantForStatus(status: string | null | undefined): PillVariant {
  if (!status) return 'neutral';
  const s = status.toLowerCase();
  if (s === 'completed' || s === 'exited' || s === 'succeeded') return 'success';
  if (s === 'faulted' || s === 'failed' || s === 'aborted') return 'danger';
  if (s === 'paused' || s === 'waiting') return 'warning';
  if (s === 'running' || s === 'entered' || s === 'in_progress') return 'info';
  return 'neutral';
}

/** Render an ISO timestamp as a locale-friendly string, or '—' when missing. */
function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      hour12: false,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return iso ?? '—';
  }
}

/** True when the event's payload references this entry_id. The engine
 *  encodes the linkage under one of several keys depending on the
 *  event kind (worker_committed uses ``state_entry_id``, transition
 *  events use ``entry_id``, brief events use ``state_entry_id``); we
 *  probe all three so the filter doesn't silently drop a relevant
 *  event when a future event kind picks the alternate spelling. */
function eventReferencesEntry(event: FsmEvent, entryId: string): boolean {
  const p = event.payload;
  if (!p || typeof p !== 'object') return false;
  const candidates = [
    (p as Record<string, unknown>).state_entry_id,
    (p as Record<string, unknown>).entry_id,
    (p as Record<string, unknown>).entryId,
  ];
  return candidates.some((v) => typeof v === 'string' && v === entryId);
}

/** Pick the most recent ``worker_committed`` event for this entry so
 *  we can surface its cosignature + brief id in the run-values tab.
 *  Returns ``null`` when no committing event has landed yet. */
function findCommitEvent(
  events: FsmEvent[],
  entryId: string,
): FsmEvent | null {
  for (const event of events) {
    if (!eventReferencesEntry(event, entryId)) continue;
    const k = event.kind.toLowerCase();
    if (k.includes('commit')) return event;
  }
  return null;
}

/** Extract the cosignature scalar from a commit event payload. The
 *  field name is ``signature`` per the engine's
 *  ``commit_outputs`` wire format. */
function pickSignature(event: FsmEvent | null): string | null {
  if (!event) return null;
  const p = event.payload as Record<string, unknown>;
  const sig = p.signature ?? p.cosignature;
  return typeof sig === 'string' ? sig : null;
}

/** Extract the brief id from a commit event payload. */
function pickBriefId(event: FsmEvent | null): string | null {
  if (!event) return null;
  const p = event.payload as Record<string, unknown>;
  const b = p.brief_id ?? p.briefId;
  return typeof b === 'string' ? b : null;
}

/** Variant lookup for event-kind pills shown in the timeline. */
function variantForEventKind(kind: string): PillVariant {
  const k = kind.toLowerCase();
  if (k.includes('error') || k.includes('fault') || k.includes('abort')) {
    return 'danger';
  }
  if (k.includes('warn') || k.includes('retry') || k.includes('pause')) {
    return 'warning';
  }
  if (k.includes('complete') || k.includes('success') || k.includes('commit')) {
    return 'success';
  }
  if (k.includes('state') || k.includes('transition') || k.includes('enter')) {
    return 'info';
  }
  return 'neutral';
}

export function StateEntrySheetBody({
  entryId,
  runId,
  stateTree,
  spec,
  events,
}: StateEntrySheetBodyProps): JSX.Element {
  const [activeTab, setActiveTab] = useState<StateEntryTabId>('run');

  const entry = useMemo(() => findEntry(stateTree, entryId), [stateTree, entryId]);
  const specState = useMemo(
    () => (entry ? findSpecState(spec, entry.state_id) : null),
    [spec, entry],
  );
  const matchingEvents = useMemo(
    () => events.filter((e) => eventReferencesEntry(e, entryId)),
    [events, entryId],
  );
  const commitEvent = useMemo(
    () => findCommitEvent(matchingEvents, entryId),
    [matchingEvents, entryId],
  );

  // -- Tab 1: run values --------------------------------------------------
  const runRows: KvRow[] = useMemo(() => {
    if (!entry) return [];
    const rows: KvRow[] = [
      { key: 'state_id', value: entry.state_id },
      { key: 'entry_id', value: entry.entry_id },
      { key: 'entry_seq', value: entry.entry_seq },
      { key: 'status', value: entry.status },
      { key: 'entered_at', value: formatTimestamp(entry.entered_at) },
      { key: 'exited_at', value: formatTimestamp(entry.exited_at) },
    ];
    if (entry.iteration_n != null) {
      rows.push({ key: 'iteration_n', value: entry.iteration_n });
    }
    return rows;
  }, [entry]);

  const signature = pickSignature(commitEvent);
  const briefId = pickBriefId(commitEvent);

  // Pre-compute Pill colour for the status row so the tab body shows
  // the same semantic colour the run-detail header uses for this same
  // entry. The header chip lives next to the KeyValueTable rather than
  // inside it so the KV table stays a flat scalar grid.
  const statusVariant = entry ? variantForStatus(entry.status) : 'neutral';

  const runPanel: JSX.Element = entry ? (
    <div class="p-3 space-y-4" data-testid="state-entry-run-panel">
      <div class="flex flex-wrap items-baseline gap-2">
        <code class="font-mono text-sm text-slate-900 dark:text-slate-100">
          {entry.state_id}
        </code>
        <Pill variant={statusVariant} size="sm">
          {entry.status}
        </Pill>
        <span class="text-[10px] font-mono text-slate-400 dark:text-slate-500">
          run {runId}
        </span>
      </div>
      <KeyValueTable rows={runRows} caption="Run-recorded entry metadata" />
      <section>
        <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
          inputs
        </h4>
        <JsonViewer
          value={entry.inputs}
          rootLabel="inputs"
          mode="inline"
          maxInlineHeight="max-h-64"
          downloadFilename={`run-${runId}-${entry.state_id}-inputs.json`}
          ariaLabel={`Inputs for state entry ${entry.entry_id}`}
        />
      </section>
      <section>
        <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
          outputs
        </h4>
        <JsonViewer
          value={entry.outputs}
          rootLabel="outputs"
          mode="inline"
          maxInlineHeight="max-h-64"
          downloadFilename={`run-${runId}-${entry.state_id}-outputs.json`}
          ariaLabel={`Outputs for state entry ${entry.entry_id}`}
        />
      </section>
      {commitEvent ? (
        <section>
          <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
            commit / cosignature
          </h4>
          <KeyValueTable
            rows={[
              ...(briefId ? [{ key: 'brief_id', value: briefId }] : []),
              ...(signature ? [{ key: 'signature', value: signature }] : []),
              { key: 'committed_at', value: formatTimestamp(commitEvent.created_at) },
            ]}
            caption="Cosignature info"
          />
        </section>
      ) : (
        <p class="text-xs text-slate-500 dark:text-slate-400">
          No commit event observed in the loaded slice.
        </p>
      )}
    </div>
  ) : (
    <EmptyState
      title="Entry not in tree"
      message={`No state entry with id ${entryId} found in the loaded state tree.`}
    />
  );

  // -- Tab 2: spec definition --------------------------------------------
  const specPanel: JSX.Element = specState ? (
    <StateInspectorBody
      state={specState}
      isEntry={
        typeof (spec?.definition as { entry?: unknown } | undefined)?.entry === 'string' &&
        (spec?.definition as { entry?: string }).entry === (specState.id as string)
      }
    />
  ) : (
    <EmptyState
      title="Spec state unavailable"
      message={
        spec === null
          ? 'The spec definition for this run is not loaded.'
          : entry === null
          ? 'No entry known; cannot resolve the spec state.'
          : `Spec contains no state with id ${entry.state_id}.`
      }
    />
  );

  // -- Tab 3: events for this state --------------------------------------
  const timelineItems: TimelineItem[] = useMemo(() => {
    const seen = new Set<string>();
    const items: TimelineItem[] = [];
    for (const event of matchingEvents) {
      if (seen.has(event.id)) continue;
      seen.add(event.id);
      items.push({
        id: event.id,
        timestamp: event.created_at,
        title: event.producer_id,
        kind: event.kind,
        variant: variantForEventKind(event.kind),
        payload: (
          <JsonViewer
            value={event.payload}
            rootLabel="payload"
            mode="inline"
            maxInlineHeight="max-h-40"
            ariaLabel={`Payload of event ${event.id}`}
          />
        ),
      });
    }
    return items;
  }, [matchingEvents]);

  const eventsPanel: JSX.Element =
    timelineItems.length === 0 ? (
      <EmptyState
        title="No events for this entry"
        message="The loaded event slice has no rows scoped to this state entry."
      />
    ) : (
      <div class="p-3" data-testid="state-entry-events-panel">
        <Timeline
          items={timelineItems}
          label={`Events for state entry ${entryId}`}
        />
      </div>
    );

  const tabs: TabSpec[] = [
    { id: 'run', label: 'Run values' },
    { id: 'spec', label: 'Spec definition' },
    {
      id: 'events',
      label: 'Events for this state',
      // Surface the count badge so the operator sees how many events
      // belong to this entry without opening the tab.
      badge: (
        <span class="inline-flex items-center justify-center min-w-[18px] h-[18px] text-[10px] font-mono rounded-full bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-200 px-1.5">
          {timelineItems.length}
        </span>
      ),
    },
  ];

  return (
    <Tabs
      tabs={tabs}
      activeTab={activeTab}
      onChange={(id) => setActiveTab(id as StateEntryTabId)}
      panels={{
        run: runPanel,
        spec: specPanel,
        events: eventsPanel,
      }}
      ariaLabel={`State entry ${entryId} inspector`}
    />
  );
}

export default StateEntrySheetBody;
