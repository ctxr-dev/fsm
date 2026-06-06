/**
 * /runs/:runId — full per-run operator view (PR 7 layout cutover).
 *
 * Layout
 * ------
 *
 *   - Header — run id, spec slug+version, status/verdict pills,
 *     timestamps, operator action buttons (Abort, Resume, Journal
 *     Discard / Replay) plus an "Admin" button that opens the right-
 *     half AdminSheet (PR 3) carrying the old admin card's six
 *     collapsible sections.
 *   - Filter chip bar — surfaces the cross-pane filter set written by
 *     label clicks elsewhere in the app (the chips are still wired to
 *     the runDetailFilters signal so legacy click sources keep
 *     working).
 *   - 50 / 50 grid — single column below ``lg``, two equal columns at
 *     ``lg``+:
 *       LEFT  — :class:`RunProgressGraph` (the spec topology overlaid
 *               with the run's traversal). Graph node / edge / loop
 *               iteration clicks open the StateEntrySheet (PR 4) or
 *               EdgeSheet (PR 3) respectively.
 *       RIGHT — :class:`RunEventTimeline` (vertical event log). The
 *               SSE stream below keeps it live.
 *
 * What this file used to do, and where it went
 * --------------------------------------------
 *
 * The pre-cutover route owned a left "States" tree, a selected-node
 * inputs/outputs inspector, an inline three-section right "Admin"
 * Card, and a bottom collapsible Tool Calls audit. Those have all
 * migrated:
 *
 *   - States tree + per-node inspector → StateEntrySheetBody (PR 4).
 *     Graph node clicks open the sheet with the same three tabs
 *     (Run values / Spec definition / Events) that the inline panel
 *     used to flatten into one column.
 *   - Right "Admin" Card (Journal / Lock / Drift / Signatures /
 *     Allowed tools / Tool Calls) → AdminSheetBody (PR 3) and its
 *     ToolCallsSection (PR 6 plumbing). The bottom collapsible audit
 *     log lives inside the AdminSheet too.
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
 * runs never paints data from the previous one. tool-calls / signatures
 * are still fetched here so AdminSheetBody (which is mounted lazily by
 * the SheetHost) gets a populated manifest the first time the operator
 * opens it.
 */

import type { JSX } from 'preact';
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { useRoute } from 'preact-iso';

import {
  Button,
  Card,
  Dialog,
  EmptyState,
  FilterChips,
  Pill,
  RunEventTimeline,
  RunProgressGraph,
  Spinner,
  useToast,
  type FilterChip,
  type PillVariant,
} from '../components';
import {
  clearAllFilters,
  clearFilter,
  eventPassesFilters,
  filtersToChips,
  runDetailFilters,
} from '../lib/runDetailStore';
import {
  api,
  ApiError,
  type CommitSignatureRecord,
  type DriftSignalsResponse,
  type Event as FsmEvent,
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
import {
  bumpDriftRefresh,
  bumpSignaturesRefresh,
  bumpStateTreeRefresh,
  bumpToolCallsRefresh,
} from '../lib/runDetailRefresh';
import { useDebouncedRefetch } from '../lib/useDebouncedRefetch';
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

// ---------------------------------------------------------------------------
// Variant maps — semantic colours per status / verdict
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

function variantForStatus(status: string | null | undefined): PillVariant {
  if (!status) return 'neutral';
  return STATUS_VARIANTS[status.toLowerCase()] ?? 'neutral';
}

function variantForVerdict(verdict: string | null | undefined): PillVariant {
  if (!verdict) return 'neutral';
  return VERDICT_VARIANTS[verdict.toLowerCase()] ?? 'neutral';
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
// State-tree → entry-id index (only the most-recent-entry lookup the
// graph node onClick needs; the per-node inspector that used to consume
// the full StateNode lives in StateEntrySheetBody now).
// ---------------------------------------------------------------------------

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
  // toolCalls / drift / signatures are kept in route state so the
  // AdminSheet (which mounts lazily) sees a populated manifest the
  // first time the operator opens it. They are NOT rendered inline by
  // the route any more — that responsibility moved to AdminSheetBody.
  // The setters are also fed by the PR 6 SSE-driven debounced
  // refetchers below; the values themselves are intentionally not read
  // here (AdminSheetBody owns the per-section fetch + render).
  const [, setToolCalls] = useState<ToolCall[]>([]);
  const [, setDrift] = useState<DriftSignalsResponse | null>(null);
  const [, setSignatures] = useState<CommitSignatureRecord[]>([]);
  const [spec, setSpec] = useState<SpecDetail | null>(null);

  // --- Loading / error envelopes -----------------------------------------
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // --- UI state -----------------------------------------------------------
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [actionBusy, setActionBusy] = useState<boolean>(false);

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
        // page_size=200 is the MAX_PAGE_SIZE cap; AdminSheetBody's
        // "last 3" slice operates on .items so this header-sized page
        // is plenty.
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
  // PR 6: SSE-driven debounced refetchers.
  //
  // The route owns the SINGLE EventStream subscription for the page so
  // the server only fans out the run's events once. As each event lands
  // we (a) prepend it to the timeline (handled below) and (b) dispatch
  // a debounced refetch for the relevant data source — state tree on
  // entry/exit/transition/inline events, drift on signal/pause events,
  // signatures on commit verify/mismatch, tool-calls on tool_call
  // events. The debouncer coalesces bursts so a chatty run does not
  // hammer the API with one refetch per frame.
  //
  // The refetchers also bump per-kind refresh nonces (see
  // ``runDetailRefresh.ts``) so lazily-mounted panels (eg. the Admin
  // Sheet's Drift / Signatures / Tool calls sections) can react to the
  // same burst without opening their own redundant EventStream.
  // -----------------------------------------------------------------------

  // Race guard — every refetch closure captures the event seq at
  // trigger time and ignores its response if a newer event has since
  // arrived. The check is a defence-in-depth measure: the debouncer's
  // bounded latency already minimises overlap, but a slow API call can
  // still resolve out of order relative to a fresh burst. ``lastEventSeq``
  // is updated by the SSE handler each time a frame lands so the guard
  // sees the most-recent seq when the API call resolves.
  const lastEventSeqRef = useRef<number>(0);

  const refetchStateTree = useDebouncedRefetch(() => {
    if (!runId) return;
    const seqAtTrigger = lastEventSeqRef.current;
    return api
      .getStateTree(runId)
      .then((tree) => {
        if (seqAtTrigger < lastEventSeqRef.current && lastEventSeqRef.current > 0) {
          // A newer burst overtook us; a follow-up refetch is already
          // queued or in-flight so dropping this response is safe.
          return;
        }
        setStateTree(tree);
        bumpStateTreeRefresh();
      })
      .catch(() => {
        // Stay silent: a single failed refetch should not blank the
        // tree. The next event tick will retry.
      });
  });

  const refetchDrift = useDebouncedRefetch(() => {
    if (!runId) return;
    const seqAtTrigger = lastEventSeqRef.current;
    return api
      .listDriftSignals(runId)
      .then((res) => {
        if (seqAtTrigger < lastEventSeqRef.current && lastEventSeqRef.current > 0) {
          return;
        }
        setDrift(res);
        bumpDriftRefresh();
      })
      .catch(() => {
        /* see refetchStateTree */
      });
  });

  const refetchSignatures = useDebouncedRefetch(() => {
    if (!runId) return;
    const seqAtTrigger = lastEventSeqRef.current;
    return api
      .listCommitSignatures(runId, { page_size: 200 })
      .then((page) => {
        if (seqAtTrigger < lastEventSeqRef.current && lastEventSeqRef.current > 0) {
          return;
        }
        setSignatures(page.items);
        bumpSignaturesRefresh();
      })
      .catch(() => {
        /* see refetchStateTree */
      });
  });

  const refetchToolCalls = useDebouncedRefetch(() => {
    if (!runId) return;
    const seqAtTrigger = lastEventSeqRef.current;
    return api
      .listToolCalls({ run_id: runId, page_size: 50 })
      .then((page) => {
        if (seqAtTrigger < lastEventSeqRef.current && lastEventSeqRef.current > 0) {
          return;
        }
        setToolCalls(page.items);
        bumpToolCallsRefresh();
      })
      .catch(() => {
        /* see refetchStateTree */
      });
  });

  // -----------------------------------------------------------------------
  // SSE subscription — keeps the timeline live and dispatches the
  // debounced per-kind refetches above.
  // -----------------------------------------------------------------------

  useEffect(() => {
    if (!runId) return undefined;
    const stream = new EventStream('/api/v1/events/stream', {
      consumer_name: `dashboard-run-${runId}`,
      filter_run_id: runId,
    });
    const unsubscribe = stream.on((event) => {
      // Track the most-recent seq so each in-flight refetch's race
      // guard sees the latest tick when it resolves.
      if (event.seq != null && event.seq > lastEventSeqRef.current) {
        lastEventSeqRef.current = event.seq;
      }
      // Dispatch debounced refetches BEFORE the prepend so a fast SSE
      // burst still triggers exactly one refetch per kind per debounce
      // window — the timeline prepend just rerenders the rows.
      switch (event.kind) {
        case 'state_entered':
        case 'state_exited':
        case 'state_faulted':
        case 'transition_taken':
        case 'inline_executed':
          refetchStateTree.trigger();
          break;
        case 'drift_signal_recorded':
        case 'drift_pause_triggered':
          refetchDrift.trigger();
          break;
        case 'commit_signature_verified':
        case 'commit_signature_mismatch':
          refetchSignatures.trigger();
          break;
        case 'tool_call_observed':
          refetchToolCalls.trigger();
          break;
        default:
          break;
      }
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
  }, [runId, refetchStateTree, refetchDrift, refetchSignatures, refetchToolCalls]);

  // -----------------------------------------------------------------------
  // Derived state
  // -----------------------------------------------------------------------

  const nodeIndex = useMemo(() => indexStateNodes(stateTree), [stateTree]);

  // Clean-slate rebuild: single sheet opener used by the graph node
  // click, the loop chip click, AND the "Iterations" chip strip inside
  // StateEntrySheetBody. Wiring all three to one helper keeps the
  // per-entry title + content composition in one place; the strip
  // re-uses ``openIteration`` via the ``onSelectIteration`` prop so
  // clicking a sibling iteration replaces the current sheet with a
  // fresh one pointed at the picked entry_id.
  //
  // Important: the sheet content is composed lazily INSIDE the click
  // handler, not captured at the time the operator first opens the
  // sheet. The previous version listed `events / spec / stateTree /
  // nodeIndex` in the useCallback deps and re-baked the content on
  // every redrender of those values — but the sheet content was a
  // ReactNode literal whose closure had ALREADY been snapshotted by
  // SheetHost. By the time the operator clicked a sibling iteration
  // chip, the StateEntrySheetBody it re-rendered was reading from the
  // closure's stale `events` / `stateTree`. Holding the latest values
  // in a ref and dereferencing them at click time lets every iteration
  // jump see the freshest SSE-prepended events without forcing the
  // sheet opener to depend on them.
  const latestSheetDeps = useRef({
    stateTree,
    spec,
    events,
    nodeIndex,
  });
  latestSheetDeps.current = { stateTree, spec, events, nodeIndex };

  const openIteration = useCallback(
    (entryId: string) => {
      const { nodeIndex: latestNodeIndex } = latestSheetDeps.current;
      const entry = latestNodeIndex.get(entryId);
      const stateLabel = entry?.state_id ?? entryId;
      const iterLabel =
        entry?.iteration_n != null ? ` · iter ${entry.iteration_n}` : '';
      openStateEntrySheetOpener({
        entryId,
        runId,
        title: `State entry · ${stateLabel}${iterLabel}`,
        content: (
          <StateEntrySheetBody
            entryId={entryId}
            runId={runId}
            stateTree={latestSheetDeps.current.stateTree}
            spec={latestSheetDeps.current.spec}
            events={latestSheetDeps.current.events}
            onSelectIteration={(next) => openIteration(next)}
          />
        ),
      });
    },
    // Deliberately depend on `runId` only — every other sheet input is
    // resolved through `latestSheetDeps.current` at click time so the
    // body sees the freshest SSE-driven values without re-baking this
    // callback's identity on every event.
    [runId],
  );

  // W18d: subscribe to the cross-pane filter set so the timeline
  // rerenders when chips change. The right column applies the filter
  // before passing events into RunEventTimeline.
  const activeFilters = runDetailFilters.value;
  const filterChips: FilterChip[] = useMemo(
    () => filtersToChips(activeFilters),
    [activeFilters],
  );
  const filteredEvents = useMemo(
    () => events.filter((e) => eventPassesFilters(e, activeFilters)),
    [events, activeFilters],
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

  // Action affordances — gate Resume / Journal actions on the manifest
  // and journal shape so the operator does not fire pointless requests.
  const canAbort =
    manifest.status !== 'completed' && manifest.status !== 'aborted';
  const canResume =
    manifest.status === 'paused' || manifest.status === 'faulted';
  const hasPendingJournal =
    journal !== null && journal.status !== 'finalised';

  const meta = pendingAction ? ACTION_META[pendingAction.kind] : null;

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
    <div
      class="p-4 md:p-6 flex flex-col gap-4 h-full min-h-0"
      data-testid="run-detail-route"
    >
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
          {/* PR 3 + PR 7: the Admin sheet is now the SOLE surface for
              the six admin sections — Journal, Lock, Drift, Signatures,
              Allowed tools, and the Tool Calls audit log. The inline
              Admin Card has been removed in this cutover. */}
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
          50/50 grid: graph on the left, event timeline on the right.
          On <lg viewports both columns stack (single column). The
          ``h-full min-h-0 flex-1`` on the wrapper lets the grid claim
          the leftover vertical space below the header so the graph and
          timeline both stretch to the bottom of the viewport instead
          of collapsing to their intrinsic min-heights.
          ---------------------------------------------------------------- */}
      <div
        class="grid grid-cols-1 lg:grid-cols-2 gap-4 flex-1 min-h-0"
        data-testid="run-detail-grid"
      >
        <RunProgressGraph
          manifest={run.manifest}
          stateTree={stateTree}
          events={events}
          fill
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
              if (
                !existing ||
                existing.state_id !== stateId ||
                node.entry_seq > existing.entry_seq
              ) {
                target = node.entry_id;
              }
            }
            openIteration(target);
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
          onIterationClick={(entryId) => {
            // Clean-slate rebuild: loop chip click still routes through
            // openIteration so a future on-graph affordance can drop in
            // without re-implementing the sheet opener.
            openIteration(entryId);
          }}
        />

        {/* RIGHT — live event timeline */}
        <Card
          title="Events"
          className="flex flex-col h-full min-h-0"
        >
          <RunEventTimeline events={filteredEvents} />
        </Card>
      </div>

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
