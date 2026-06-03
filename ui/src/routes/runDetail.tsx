/**
 * /runs/:runId — full per-run operator view.
 *
 * Three-pane layout (one column on mobile, three on desktop):
 *
 *   1. **Left** — :class:`Tree` of state entries (root = first entry the
 *      engine recorded, descendants from ``StateNode.children``). Clicking
 *      a node selects it and reveals its ``inputs`` / ``outputs`` blobs in
 *      a panel below the tree.
 *   2. **Middle** — :class:`Timeline` of FSM events. Seeded from
 *      ``GET /runs/{id}/events`` and kept live by a per-run SSE
 *      subscription (``GET /api/v1/events/stream?filter_run_id=…``). The
 *      timeline shows the most-recent 200 events newest-first.
 *   3. **Right** — admin / journal panel showing the current journal-txn
 *      status, the lock row (if any), the last three commit signatures,
 *      a drift-score gauge (W12 substrate), and the allowed_tools list
 *      for the current state when surfaced by the state node payload.
 *
 * A collapsible bottom panel reads the last 50 rows from
 * ``GET /admin/tool_calls?run_id=…`` as a structured audit log.
 *
 * Header
 * ------
 *
 * The header shows the full run id (so it can be copied), the spec
 * slug+version (fetched lazily from ``GET /specs/{spec_id}``), a status
 * pill, a verdict pill (when set), and the started_at / ended_at
 * timestamps. The right side hosts the operator actions — Abort,
 * Resume, Journal Discard, Journal Replay — each of which opens a
 * confirmation :class:`Dialog` before firing the underlying request.
 * Successful actions emit a success toast via :func:`useToast`; errors
 * become danger toasts that carry the API error message.
 *
 * Lifecycle
 * ---------
 *
 * On mount, the route fires getRun, getStateTree, getEvents (limit=200),
 * listToolCalls, listDriftSignals, and listCommitSignatures in parallel
 * via ``Promise.allSettled`` — a single panel failure should not block
 * the rest of the page from rendering. The SSE stream is opened in a
 * separate effect so reconnect logic lives alongside its own teardown.
 * Every effect tracks a ``cancelled`` flag so a fast navigation between
 * runs never paints data from the previous one.
 */

import type { JSX, VNode } from 'preact';
import { useCallback, useEffect, useMemo, useState } from 'preact/hooks';
import { useRoute } from 'preact-iso';

import {
  Button,
  Card,
  Dialog,
  DriftGauge,
  EmptyState,
  FilterChips,
  JsonViewer,
  Pill,
  RunProgressGraph,
  Spinner,
  Timeline,
  Tree,
  useToast,
  type FilterChip,
  type PillVariant,
  type TimelineItem,
  type TreeNode,
} from '../components';
import {
  clearAllFilters,
  clearFilter,
  eventPassesFilters,
  filtersToChips,
  runDetailFilters,
  signaturePassesFilters,
  toggleFilter,
  toolCallPassesFilters,
} from '../lib/runDetailStore';
import {
  api,
  ApiError,
  type CommitSignatureRecord,
  type DriftSignalsResponse,
  type Event as FsmEvent,
  type JsonObject,
  type RunDetail,
  type RunManifest,
  type SpecDetail,
  type StateNode,
  type ToolCall,
} from '../lib/api';
import { openSheet } from '../lib/store';
import {
  openEdgeSheet as openEdgeSheetOpener,
  openStateEntrySheet as openStateEntrySheetOpener,
} from '../lib/runDetailSheets';
import { EventStream } from '../lib/sse';
import { AdminSheetBody } from './runDetail/AdminSheetBody';
import { EdgeSheetBody } from './runDetail/EdgeSheetBody';
import { StateEntrySheetBody } from './runDetail/StateEntrySheetBody';

/**
 * Helper: open the run-admin sheet for ``runId``.
 *
 * Exposed as a route-local function so the header button stays a thin
 * onClick handler and the SheetHost contract (``{ id, title, content }``)
 * lives next to the body component it routes to.
 */
export function openAdminSheet(params: {
  runId: string;
  manifest: RunManifest;
}): void {
  const { runId, manifest } = params;
  openSheet({
    id: `admin:${runId}`,
    title: 'Run admin',
    width: 'right-half',
    urlFragment: `admin-${runId}`,
    content: <AdminSheetBody runId={runId} manifest={manifest} />,
  });
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

/** Render an ISO 8601 timestamp as a locale-friendly ``YYYY-MM-DD HH:MM:SS``. */
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

/** Truncate a hex hash to its first 12 chars for table-style display. */
function shortHash(hash: string): string {
  return hash.length > 12 ? hash.slice(0, 12) : hash;
}

// W18d: prettyJson helper removed — every JSON-render site now uses
// the JsonViewer primitive (which carries its own copy / download /
// full-screen / search toolbar instead of producing a raw string).

// ---------------------------------------------------------------------------
// Variant maps — semantic colours per status / verdict / event kind
// ---------------------------------------------------------------------------

const STATUS_VARIANTS: Record<string, PillVariant> = {
  running: 'info',
  in_progress: 'info',
  paused: 'warning',
  waiting: 'warning',
  faulted: 'danger',
  failed: 'danger',
  aborted: 'danger',
  completed: 'success',
  succeeded: 'success',
};

const VERDICT_VARIANTS: Record<string, PillVariant> = {
  pass: 'success',
  passed: 'success',
  ok: 'success',
  fail: 'danger',
  failed: 'danger',
  error: 'danger',
  inconclusive: 'warning',
  pending: 'neutral',
};

const JOURNAL_VARIANTS: Record<string, PillVariant> = {
  pending: 'warning',
  ready_to_finalise: 'info',
  finalised: 'success',
};

function variantForStatus(status: string | null | undefined): PillVariant {
  if (!status) return 'neutral';
  return STATUS_VARIANTS[status.toLowerCase()] ?? 'neutral';
}

function variantForVerdict(verdict: string | null | undefined): PillVariant {
  if (!verdict) return 'neutral';
  return VERDICT_VARIANTS[verdict.toLowerCase()] ?? 'neutral';
}

function variantForJournal(status: string | null | undefined): PillVariant {
  if (!status) return 'neutral';
  return JOURNAL_VARIANTS[status.toLowerCase()] ?? 'neutral';
}

/** Pick a pill colour for an event row in the timeline. */
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

// ---------------------------------------------------------------------------
// State-tree → TreeNode adaptation
// ---------------------------------------------------------------------------

/**
 * Flatten ``StateNode.entry_id`` lookup for the selected-node panel.
 *
 * Walks the tree once and emits ``[entry_id, node]`` pairs so the
 * detail pane can look up the freshly-selected node in O(1).
 */
function indexStateNodes(
  root: StateNode | null,
  out: Map<string, StateNode> = new Map(),
): Map<string, StateNode> {
  if (!root) return out;
  out.set(root.entry_id, root);
  for (const child of root.children) {
    indexStateNodes(child, out);
  }
  return out;
}

/** Build a label like ``connect_db [iter 3] – completed``. */
function stateNodeLabel(node: StateNode): string {
  const iter = node.iteration_n != null ? ` [iter ${node.iteration_n}]` : '';
  const status = node.status ? ` – ${node.status}` : '';
  return `${node.state_id}${iter}${status}`;
}

/** Adapt a :class:`StateNode` subtree into the :class:`Tree`'s node shape. */
function toTreeNode(node: StateNode): TreeNode {
  return {
    id: node.entry_id,
    label: stateNodeLabel(node),
    defaultExpanded: true,
    children: node.children.map(toTreeNode),
  };
}

// ---------------------------------------------------------------------------
// Allowed-tools extraction
// ---------------------------------------------------------------------------

/**
 * Pull an ``allowed_tools`` list out of a :class:`StateNode`'s payload.
 *
 * The contract is loose by design — different W12 substrates pin the
 * allow-list under different keys (``allowed_tools``, ``tools``,
 * ``allowedTools``). We probe outputs first (the state's *current*
 * truth), then inputs (the seed list at entry time). Returning
 * ``null`` lets the panel render a "not surfaced" placeholder rather
 * than mis-presenting an empty array.
 */
function extractAllowedTools(node: StateNode | null): string[] | null {
  if (!node) return null;
  const probes: JsonObject[] = [node.outputs, node.inputs];
  const keys = ['allowed_tools', 'allowedTools', 'tools', 'tool_allowlist'];
  for (const probe of probes) {
    for (const key of keys) {
      const value = probe[key];
      if (Array.isArray(value) && value.every((v) => typeof v === 'string')) {
        return value as string[];
      }
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Event-log → TimelineItem adaptation
// ---------------------------------------------------------------------------

/** Render an FSM event as a :class:`TimelineItem`. */
function eventToTimelineItem(event: FsmEvent): TimelineItem {
  const variant = variantForEventKind(event.kind);
  const payload: VNode = (
    <JsonViewer
      value={event.payload}
      rootLabel="payload"
      mode="inline"
      maxInlineHeight="max-h-40"
      ariaLabel={`Payload of event ${event.id}`}
    />
  );
  return {
    id: event.id,
    timestamp: event.created_at,
    title: event.producer_id,
    kind: event.kind,
    variant,
    payload,
  };
}

// ---------------------------------------------------------------------------
// Pending-action discriminator
// ---------------------------------------------------------------------------

/**
 * Operator actions that need a confirmation dialog before firing.
 *
 * Kept as a discriminated union so the dialog's title / body / button
 * label all derive from a single switch — no chance of an "Abort" body
 * paired with a "Resume" handler.
 */
type PendingAction =
  | { kind: 'abort' }
  | { kind: 'resume' }
  | { kind: 'journal-discard' }
  | { kind: 'journal-replay' };

interface ActionMeta {
  title: string;
  body: string;
  confirmLabel: string;
  confirmVariant: 'primary' | 'danger';
}

const ACTION_META: Record<PendingAction['kind'], ActionMeta> = {
  abort: {
    title: 'Abort run?',
    body: 'Marks the run as aborted. Any in-flight work is left where it is — the engine will not retry.',
    confirmLabel: 'Abort run',
    confirmVariant: 'danger',
  },
  resume: {
    title: 'Resume run?',
    body: 'Resumes a paused or faulted run from its last persisted state.',
    confirmLabel: 'Resume',
    confirmVariant: 'primary',
  },
  'journal-discard': {
    title: 'Discard pending journal txn?',
    body: 'Throws away any staged writes the engine never finalised. Safe when the writes are known to be stale.',
    confirmLabel: 'Discard journal',
    confirmVariant: 'danger',
  },
  'journal-replay': {
    title: 'Replay pending journal txn?',
    body: 'Re-applies the staged writes to the run. Use when the original commit was interrupted mid-flight.',
    confirmLabel: 'Replay journal',
    confirmVariant: 'primary',
  },
};

// ---------------------------------------------------------------------------
// Main route component
// ---------------------------------------------------------------------------

export function RunDetailRoute(): JSX.Element {
  const { params } = useRoute();
  const runId = params.runId ?? params.id ?? '';
  const toast = useToast();

  // --- Loaded data --------------------------------------------------------
  const [run, setRun] = useState<RunDetail | null>(null);
  const [stateTree, setStateTree] = useState<StateNode | null>(null);
  const [events, setEvents] = useState<FsmEvent[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [drift, setDrift] = useState<DriftSignalsResponse | null>(null);
  const [signatures, setSignatures] = useState<CommitSignatureRecord[]>([]);
  const [spec, setSpec] = useState<SpecDetail | null>(null);

  // --- Loading / error envelopes -----------------------------------------
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // --- UI state -----------------------------------------------------------
  const [selectedEntryId, setSelectedEntryId] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [actionBusy, setActionBusy] = useState<boolean>(false);
  const [auditOpen, setAuditOpen] = useState<boolean>(false);

  // -----------------------------------------------------------------------
  // Initial parallel fetch
  // -----------------------------------------------------------------------

  const loadAll = useCallback(
    (id: string) => {
      let cancelled = false;
      setLoading(true);
      setError(null);
      // ``allSettled`` so one failed panel does not blank the page — the
      // header always lands as long as ``getRun`` resolves; the others
      // degrade to empty states.
      Promise.allSettled([
        api.getRun(id),
        api.getStateTree(id),
        api.getEvents(id, { page_size: 200 }),
        api.listToolCalls({ run_id: id, page_size: 50 }),
        api.listDriftSignals(id),
        // page_size=200 is the MAX_PAGE_SIZE cap; the "last 3" UI slice
        // below operates on .items so this header-sized page is plenty.
        api.listCommitSignatures(id, { page_size: 200 }),
      ]).then((results) => {
        if (cancelled) return;
        const [runRes, treeRes, eventsRes, toolsRes, driftRes, sigsRes] =
          results;
        if (runRes.status === 'fulfilled') {
          setRun(runRes.value);
        } else {
          const reason = runRes.reason;
          setError(
            reason instanceof ApiError ? reason.message : String(reason),
          );
        }
        if (treeRes.status === 'fulfilled') {
          setStateTree(treeRes.value);
        } else if (runRes.status === 'fulfilled') {
          // Fall back to the embedded state tree from the run manifest
          // when the dedicated endpoint failed (or the run has not yet
          // produced any state entries).
          setStateTree(runRes.value.state_tree);
        } else {
          setStateTree(null);
        }
        setEvents(
          eventsRes.status === 'fulfilled' ? eventsRes.value.items : [],
        );
        setToolCalls(
          toolsRes.status === 'fulfilled' ? toolsRes.value.items : [],
        );
        setDrift(driftRes.status === 'fulfilled' ? driftRes.value : null);
        setSignatures(
          sigsRes.status === 'fulfilled' ? sigsRes.value.items : [],
        );
        setLoading(false);
      });
      return () => {
        cancelled = true;
      };
    },
    [],
  );

  useEffect(() => {
    if (!runId) {
      setLoading(false);
      setError('No run id supplied in the URL.');
      return undefined;
    }
    const cancel = loadAll(runId);
    return cancel;
  }, [runId, loadAll]);

  // -----------------------------------------------------------------------
  // Lazy spec fetch (header chip)
  // -----------------------------------------------------------------------

  useEffect(() => {
    const specId = run?.manifest.fsm_spec_id;
    if (!specId) {
      setSpec(null);
      return undefined;
    }
    let cancelled = false;
    api
      .getSpec(specId)
      .then((s) => {
        if (!cancelled) setSpec(s);
      })
      .catch(() => {
        // Spec lookup is best-effort — failure just suppresses the chip.
        if (!cancelled) setSpec(null);
      });
    return () => {
      cancelled = true;
    };
  }, [run?.manifest.fsm_spec_id]);

  // -----------------------------------------------------------------------
  // SSE subscription — keeps the timeline live
  // -----------------------------------------------------------------------

  useEffect(() => {
    if (!runId) return undefined;
    const stream = new EventStream('/api/v1/events/stream', {
      consumer_name: `dashboard-run-${runId}`,
      filter_run_id: runId,
    });
    const unsubscribe = stream.on((event) => {
      // Prepend so newest renders at the top of the timeline; cap at 200
      // so the DOM stays bounded on chatty runs.
      setEvents((prev) => {
        // Skip duplicates — the seed fetch and the SSE stream can overlap
        // briefly on subscription, especially after a reconnect.
        if (prev.some((e) => e.id === event.id)) return prev;
        const next = [event, ...prev];
        return next.length > 200 ? next.slice(0, 200) : next;
      });
    });
    return () => {
      unsubscribe();
      stream.close();
    };
  }, [runId]);

  // -----------------------------------------------------------------------
  // Derived state
  // -----------------------------------------------------------------------

  const nodeIndex = useMemo(() => indexStateNodes(stateTree), [stateTree]);
  const treeNodes: TreeNode[] = useMemo(
    () => (stateTree ? [toTreeNode(stateTree)] : []),
    [stateTree],
  );
  const selectedNode: StateNode | null = useMemo(() => {
    if (!selectedEntryId) return null;
    return nodeIndex.get(selectedEntryId) ?? null;
  }, [selectedEntryId, nodeIndex]);

  // Find the "current" node for the allowed-tools panel — prefer the
  // explicit ``current_state`` from the manifest, fall back to the
  // most-recently-entered node walking the tree depth-first.
  const currentNode: StateNode | null = useMemo(() => {
    const current = run?.manifest.current_state;
    if (!current) return null;
    for (const node of nodeIndex.values()) {
      if (node.state_id === current && node.exited_at === null) {
        return node;
      }
    }
    // Fall back to any node with this state id (even if exited) so the
    // panel surfaces something on completed runs.
    for (const node of nodeIndex.values()) {
      if (node.state_id === current) return node;
    }
    return null;
  }, [nodeIndex, run?.manifest.current_state]);

  const allowedTools = useMemo(
    () => extractAllowedTools(selectedNode ?? currentNode),
    [selectedNode, currentNode],
  );

  // W18d: subscribe to the cross-pane filter set so the timeline /
  // tool calls / signatures rerender when chips change.
  const activeFilters = runDetailFilters.value;
  const filterChips: FilterChip[] = useMemo(
    () => filtersToChips(activeFilters),
    [activeFilters],
  );

  const filteredEvents = useMemo(
    () => events.filter((e) => eventPassesFilters(e, activeFilters)),
    [events, activeFilters],
  );
  const filteredToolCalls = useMemo(
    () => toolCalls.filter((c) => toolCallPassesFilters(c, activeFilters)),
    [toolCalls, activeFilters],
  );
  const filteredSignatures = useMemo(
    () => signatures.filter((s) => signaturePassesFilters(s, activeFilters)),
    [signatures, activeFilters],
  );

  const timelineItems = useMemo(
    () => filteredEvents.map(eventToTimelineItem),
    [filteredEvents],
  );

  // Reset filters whenever the user navigates between runs.
  useEffect(() => {
    clearAllFilters();
  }, [runId]);

  // -----------------------------------------------------------------------
  // Action handler — closes dialog, fires the API, toasts, reloads
  // -----------------------------------------------------------------------

  const performAction = useCallback(async () => {
    if (!pendingAction || !runId) return;
    setActionBusy(true);
    try {
      switch (pendingAction.kind) {
        case 'abort':
          await api.abortRun(runId);
          toast.success('Run aborted.');
          break;
        case 'resume':
          await api.resumeRun(runId);
          toast.success('Run resumed.');
          break;
        case 'journal-discard':
          await api.recoverJournal(runId, 'discard');
          toast.success('Journal txn discarded.');
          break;
        case 'journal-replay':
          await api.recoverJournal(runId, 'replay');
          toast.success('Journal txn replayed.');
          break;
      }
      setPendingAction(null);
      // Reload everything so the header pill, journal status, and event
      // tape all reflect the post-action world.
      loadAll(runId);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : String(err);
      toast.danger(message);
    } finally {
      setActionBusy(false);
    }
  }, [pendingAction, runId, toast, loadAll]);

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  if (!runId) {
    return (
      <div class="p-6">
        <EmptyState
          title="No run selected"
          message="Open a run from the dashboard to see its detail view."
        />
      </div>
    );
  }

  if (loading) {
    return (
      <div class="p-6 flex items-center justify-center min-h-[60vh]">
        <Spinner label="Loading run" />
      </div>
    );
  }

  if (error || !run) {
    return (
      <div class="p-6">
        <EmptyState
          title="Failed to load run"
          message={error ?? 'The API did not return a run for this id.'}
        />
      </div>
    );
  }

  const manifest = run.manifest;
  const journal = run.journal;
  const lock = run.lock;

  // Action affordances — gate Resume / Journal actions on the manifest
  // and journal shape so the operator does not fire pointless requests.
  const canAbort =
    manifest.status !== 'completed' && manifest.status !== 'aborted';
  const canResume =
    manifest.status === 'paused' || manifest.status === 'faulted';
  const hasPendingJournal =
    journal !== null && journal.status !== 'finalised';

  const meta = pendingAction ? ACTION_META[pendingAction.kind] : null;
  const lastSignatures = filteredSignatures.slice(0, 3);

  // FilterChipBar handlers: chip-remove maps back to the right filter key.
  const onChipRemove = useCallback((chip: FilterChip) => {
    const [k] = chip.id.split(':');
    if (k === 'state') clearFilter('stateId');
    else if (k === 'kind') clearFilter('eventKind');
    else if (k === 'producer') clearFilter('producerId');
    else if (k === 'tool') clearFilter('toolName');
    else if (k === 'signal') clearFilter('signalKind');
  }, []);

  return (
    <div class="p-4 md:p-6 space-y-4">
      {/* ----------------------------------------------------------------
          Header
          ---------------------------------------------------------------- */}
      <header class="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div class="min-w-0 space-y-2">
          <div class="flex flex-wrap items-center gap-2">
            <h1 class="text-2xl font-semibold text-slate-900 dark:text-slate-100">
              Run
            </h1>
            <code
              class="font-mono text-sm text-slate-600 dark:text-slate-300 bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded-md break-all"
              title={manifest.id}
            >
              {manifest.id}
            </code>
          </div>
          <div class="flex flex-wrap items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
            {spec ? (
              <Pill variant="info">
                {spec.slug} v{spec.version}
              </Pill>
            ) : (
              <Pill variant="neutral" title={manifest.fsm_spec_id}>
                spec {shortHash(manifest.fsm_spec_id)}
              </Pill>
            )}
            <Pill variant={variantForStatus(manifest.status)}>
              {manifest.status}
            </Pill>
            {manifest.verdict ? (
              <Pill variant={variantForVerdict(manifest.verdict)}>
                verdict: {manifest.verdict}
              </Pill>
            ) : null}
            {manifest.current_state ? (
              <span class="text-xs text-slate-500 dark:text-slate-400">
                current state:{' '}
                <code class="font-mono">{manifest.current_state}</code>
              </span>
            ) : null}
          </div>
          <dl class="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-slate-500 dark:text-slate-400 sm:grid-cols-4 max-w-2xl">
            <div>
              <dt class="uppercase tracking-wide">Started</dt>
              <dd class="font-mono text-slate-700 dark:text-slate-200">
                {formatTimestamp(manifest.started_at)}
              </dd>
            </div>
            <div>
              <dt class="uppercase tracking-wide">Ended</dt>
              <dd class="font-mono text-slate-700 dark:text-slate-200">
                {formatTimestamp(manifest.ended_at)}
              </dd>
            </div>
            <div>
              <dt class="uppercase tracking-wide">Updated</dt>
              <dd class="font-mono text-slate-700 dark:text-slate-200">
                {formatTimestamp(manifest.last_update_at)}
              </dd>
            </div>
            <div>
              <dt class="uppercase tracking-wide">Transitions</dt>
              <dd class="font-mono text-slate-700 dark:text-slate-200">
                {manifest.transitions_count}
              </dd>
            </div>
          </dl>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <Button
            variant="danger"
            size="sm"
            disabled={!canAbort}
            onClick={() => setPendingAction({ kind: 'abort' })}
          >
            Abort
          </Button>
          <Button
            variant="primary"
            size="sm"
            disabled={!canResume}
            onClick={() => setPendingAction({ kind: 'resume' })}
          >
            Resume
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={!hasPendingJournal}
            onClick={() => setPendingAction({ kind: 'journal-discard' })}
          >
            Discard journal
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={!hasPendingJournal}
            onClick={() => setPendingAction({ kind: 'journal-replay' })}
          >
            Replay journal
          </Button>
          {/* W?? — admin sheet trigger. The existing right-column Admin
              Card stays during this PR so operators have both surfaces
              simultaneously; a follow-up PR removes the inline card
              once the sheet has soaked. */}
          <Button
            variant="secondary"
            size="sm"
            onClick={() =>
              openAdminSheet({ runId: manifest.id, manifest })
            }
          >
            Admin
          </Button>
        </div>
      </header>

      {/* ----------------------------------------------------------------
          Filter chip bar (W18d): every label click in the panes below
          writes into the runDetailFilters signal; chips render here so
          the user can see + remove active filters.
          ---------------------------------------------------------------- */}
      <FilterChips
        chips={filterChips}
        onRemove={onChipRemove}
        onClear={clearAllFilters}
        ariaLabel="Run filters"
      />

      {/* ----------------------------------------------------------------
          Progress graph (W22b4) — spec topology overlaid with the
          run's actual traversal. Lives above the three-pane grid so it
          gets full viewport width on every breakpoint. When the run
          hasn't entered any states yet, the graph renders the full
          spec topology with every node tagged ``not_visited`` (greyed)
          so the operator sees what the run is ABOUT to do rather
          than a blank pane. The only EmptyState branch inside
          RunProgressGraph fires when the spec itself declares zero
          states.
          ---------------------------------------------------------------- */}
      {run ? (
        <RunProgressGraph
          manifest={run.manifest}
          stateTree={stateTree}
          events={events}
          onNodeClick={(stateId) => {
            // The graph emits the spec-state-id (FlowGraph node id ==
            // spec state id). Resolve it to a concrete state ENTRY in
            // the loaded tree: prefer the most-recently-entered entry
            // for that state so a loop iteration opens the active
            // entry rather than the first one. Falls back to the
            // state id itself when no entry has landed yet so the
            // sheet can render the "Entry not in tree" EmptyState
            // instead of swallowing the click.
            let target: string = stateId;
            for (const node of nodeIndex.values()) {
              if (node.state_id !== stateId) continue;
              const existing = nodeIndex.get(target);
              if (!existing || existing.state_id !== stateId || node.entry_seq > existing.entry_seq) {
                target = node.entry_id;
              }
            }
            openStateEntrySheetOpener({
              entryId: target,
              runId: runId,
              title: `State entry · ${stateId}`,
              content: (
                <StateEntrySheetBody
                  entryId={target}
                  runId={runId}
                  stateTree={stateTree}
                  spec={spec}
                  events={events}
                />
              ),
            });
          }}
          onEdgeClick={(fromId, toId) => {
            openEdgeSheetOpener({
              runId: runId,
              fromStateId: fromId,
              toStateId: toId,
              title: `Edge · ${fromId} → ${toId}`,
              content: (
                <EdgeSheetBody
                  fromStateId={fromId}
                  toStateId={toId}
                  runId={runId}
                  stateTree={stateTree}
                  spec={spec}
                  events={events}
                />
              ),
            });
          }}
        />
      ) : null}

      {/* ----------------------------------------------------------------
          Three-pane grid
          ---------------------------------------------------------------- */}
      <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Left — state tree + selected-node inspector */}
        <Card title="States" className="lg:col-span-1">
          {treeNodes.length === 0 ? (
            <EmptyState
              title="No state entries yet"
              message="The engine has not entered any state for this run."
            />
          ) : (
            <Tree
              nodes={treeNodes}
              label="State entry tree"
              onActivate={(node) => setSelectedEntryId(node.id)}
            />
          )}
          {selectedNode ? (
            <div class="mt-4 space-y-3 border-t border-slate-200 dark:border-slate-700 pt-3">
              <div class="flex flex-wrap items-baseline gap-2">
                <button
                  type="button"
                  onClick={() => toggleFilter('stateId', selectedNode.state_id)}
                  title={`Filter all panes to state ${selectedNode.state_id}`}
                  class="text-sm font-semibold text-slate-900 dark:text-slate-100 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
                >
                  {selectedNode.state_id}
                </button>
                <Pill variant={variantForStatus(selectedNode.status)} size="sm">
                  {selectedNode.status}
                </Pill>
                <span class="text-xs text-slate-500 dark:text-slate-400">
                  seq {selectedNode.entry_seq}
                </span>
              </div>
              <div>
                <h4 class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400 mb-1">
                  Inputs
                </h4>
                <JsonViewer
                  value={selectedNode.inputs}
                  rootLabel="inputs"
                  mode="inline"
                  maxInlineHeight="max-h-48"
                  downloadFilename={`run-${runId}-${selectedNode.state_id}-inputs.json`}
                  ariaLabel={`Inputs for state ${selectedNode.state_id}`}
                />
              </div>
              <div>
                <h4 class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400 mb-1">
                  Outputs
                </h4>
                <JsonViewer
                  value={selectedNode.outputs}
                  rootLabel="outputs"
                  mode="inline"
                  maxInlineHeight="max-h-48"
                  downloadFilename={`run-${runId}-${selectedNode.state_id}-outputs.json`}
                  ariaLabel={`Outputs for state ${selectedNode.state_id}`}
                />
              </div>
            </div>
          ) : null}
        </Card>

        {/* Middle — live event timeline */}
        <Card title="Events" className="lg:col-span-1">
          {timelineItems.length === 0 ? (
            <EmptyState
              title="No events yet"
              message="Events will appear here as the run produces them."
            />
          ) : (
            <div class="max-h-[70vh] overflow-auto pr-1">
              <Timeline items={timelineItems} label="Run event timeline" />
            </div>
          )}
        </Card>

        {/* Right — admin / journal panel */}
        <Card title="Admin" className="lg:col-span-1 space-y-4">
          <section class="space-y-1">
            <h3 class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Journal
            </h3>
            {journal ? (
              <div class="flex flex-wrap items-baseline gap-2">
                <Pill variant={variantForJournal(journal.status)}>
                  {journal.status}
                </Pill>
                <span class="text-xs font-mono text-slate-500 dark:text-slate-400">
                  {journal.staged_writes.length} staged write
                  {journal.staged_writes.length === 1 ? '' : 's'}
                </span>
                <span class="text-xs text-slate-500 dark:text-slate-400">
                  started {formatTimestamp(journal.started_at)}
                </span>
              </div>
            ) : (
              <p class="text-sm text-slate-500 dark:text-slate-400">
                No pending journal txn.
              </p>
            )}
          </section>

          <section class="space-y-1">
            <h3 class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Lock
            </h3>
            {lock ? (
              <div class="flex flex-wrap items-baseline gap-2 text-sm">
                <Pill variant={lock.is_stale ? 'danger' : 'info'}>
                  {lock.is_stale ? 'stale' : 'held'}
                </Pill>
                <code class="font-mono text-xs text-slate-600 dark:text-slate-300">
                  {lock.holder_session_id}
                </code>
                <span class="text-xs text-slate-500 dark:text-slate-400">
                  expires {formatTimestamp(lock.expires_at)}
                </span>
              </div>
            ) : (
              <p class="text-sm text-slate-500 dark:text-slate-400">
                No lock currently held.
              </p>
            )}
          </section>

          <section class="space-y-1">
            <h3 class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Drift
            </h3>
            {drift ? (
              <>
                <DriftGauge score={drift.score} />
                <p class="text-xs text-slate-500 dark:text-slate-400">
                  {drift.signals.length} signal
                  {drift.signals.length === 1 ? '' : 's'} recorded.
                </p>
              </>
            ) : (
              <p class="text-sm text-slate-500 dark:text-slate-400">
                Drift score unavailable.
              </p>
            )}
          </section>

          <section class="space-y-2">
            <h3 class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Last commit signatures
            </h3>
            {lastSignatures.length === 0 ? (
              <p class="text-sm text-slate-500 dark:text-slate-400">
                No commit signatures recorded yet.
              </p>
            ) : (
              <ul class="space-y-2">
                {lastSignatures.map((sig) => (
                  <li
                    key={sig.id}
                    class="text-xs text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-slate-700 rounded-md px-3 py-2 space-y-1"
                  >
                    <div class="flex flex-wrap items-baseline gap-2">
                      <code class="font-mono text-slate-700 dark:text-slate-200">
                        {sig.state_id}
                      </code>
                      <Pill
                        variant={sig.verified ? 'success' : 'danger'}
                        size="sm"
                      >
                        {sig.verified ? 'verified' : 'unverified'}
                      </Pill>
                      {sig.iteration_n != null ? (
                        <span class="text-slate-500 dark:text-slate-400">
                          iter {sig.iteration_n}
                        </span>
                      ) : null}
                    </div>
                    <div class="font-mono">
                      sig <span title={sig.signature}>{shortHash(sig.signature)}</span>
                    </div>
                    <div class="text-slate-500 dark:text-slate-400">
                      {formatTimestamp(sig.created_at)}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section class="space-y-1">
            <h3 class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Allowed tools
              {currentNode ? (
                <span class="ml-1 font-mono normal-case text-slate-400 dark:text-slate-500">
                  ({(selectedNode ?? currentNode).state_id})
                </span>
              ) : null}
            </h3>
            {allowedTools === null ? (
              <p class="text-sm text-slate-500 dark:text-slate-400">
                Not surfaced by the current state payload.
              </p>
            ) : allowedTools.length === 0 ? (
              <p class="text-sm text-slate-500 dark:text-slate-400">
                Allow-list is empty — no tools are permitted.
              </p>
            ) : (
              <ul class="flex flex-wrap gap-1.5">
                {allowedTools.map((tool) => (
                  <li key={tool}>
                    <Pill variant="neutral" size="sm">
                      {tool}
                    </Pill>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </Card>
      </div>

      {/* ----------------------------------------------------------------
          Bottom — collapsible tool-call audit log
          ---------------------------------------------------------------- */}
      <Card className="p-0">
        <button
          type="button"
          onClick={() => setAuditOpen((v) => !v)}
          aria-expanded={auditOpen}
          aria-controls="tool-call-audit-log"
          class="w-full flex items-center justify-between gap-2 px-4 py-3 text-left text-sm font-semibold text-slate-900 dark:text-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-500 rounded-xl"
        >
          <span>
            Tool calls audit log{' '}
            <span class="ml-1 text-xs font-normal text-slate-500 dark:text-slate-400">
              ({filteredToolCalls.length}{filteredToolCalls.length !== toolCalls.length ? ` of ${toolCalls.length}` : ''} most recent)
            </span>
          </span>
          <span
            aria-hidden="true"
            class={[
              'inline-flex items-center justify-center w-5 h-5',
              'text-slate-400 dark:text-slate-500',
              'motion-safe:transition-transform',
              auditOpen ? 'rotate-90' : '',
            ].join(' ')}
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 16 16"
              fill="currentColor"
              class="w-3 h-3"
            >
              <path d="M6 3.5L10.5 8 6 12.5V3.5z" />
            </svg>
          </span>
        </button>
        {auditOpen ? (
          <div
            id="tool-call-audit-log"
            class="border-t border-slate-200 dark:border-slate-700 px-4 py-3"
          >
            {filteredToolCalls.length === 0 ? (
              <EmptyState
                title="No tool calls recorded"
                message="The audit log is empty for this run."
              />
            ) : (
              <ul class="divide-y divide-slate-200 dark:divide-slate-700">
                {filteredToolCalls.map((call) => (
                  <li key={call.id} class="py-3 space-y-1">
                    <div class="flex flex-wrap items-baseline gap-2">
                      <Pill
                        variant={call.succeeded ? 'success' : 'danger'}
                        size="sm"
                      >
                        {call.succeeded ? 'ok' : 'failed'}
                      </Pill>
                      <code class="font-mono text-sm text-slate-800 dark:text-slate-100">
                        {call.tool_name}
                      </code>
                      <span class="text-xs text-slate-500 dark:text-slate-400">
                        {formatTimestamp(call.created_at)}
                      </span>
                      <span
                        class="text-xs font-mono text-slate-400 dark:text-slate-500"
                        title={call.producer_id}
                      >
                        {shortHash(call.producer_id)}
                      </span>
                    </div>
                    <JsonViewer
                      value={call.args_redacted}
                      rootLabel="args"
                      mode="inline"
                      maxInlineHeight="max-h-40"
                      ariaLabel={`Tool call args ${call.tool_name}`}
                    />
                    <div class="mt-1 flex gap-1">
                      <button
                        type="button"
                        onClick={() => toggleFilter('toolName', call.tool_name)}
                        title={`Filter tool calls to ${call.tool_name}`}
                        class="text-[10px] underline text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
                      >
                        filter by tool
                      </button>
                      <button
                        type="button"
                        onClick={() => toggleFilter('producerId', call.producer_id)}
                        title={`Filter to producer ${call.producer_id}`}
                        class="text-[10px] underline text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
                      >
                        filter by producer
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : null}
      </Card>

      {/* ----------------------------------------------------------------
          Confirmation dialog (shared for all four actions)
          ---------------------------------------------------------------- */}
      <Dialog
        open={pendingAction !== null}
        onClose={() => {
          if (!actionBusy) setPendingAction(null);
        }}
        title={meta?.title ?? ''}
        footer={
          <>
            <Button
              variant="ghost"
              size="sm"
              disabled={actionBusy}
              onClick={() => setPendingAction(null)}
            >
              Cancel
            </Button>
            <Button
              variant={meta?.confirmVariant ?? 'primary'}
              size="sm"
              loading={actionBusy}
              onClick={() => {
                void performAction();
              }}
            >
              {meta?.confirmLabel ?? 'Confirm'}
            </Button>
          </>
        }
      >
        <p class="text-sm text-slate-700 dark:text-slate-200">
          {meta?.body ?? ''}
        </p>
      </Dialog>
    </div>
  );
}

export default RunDetailRoute;
