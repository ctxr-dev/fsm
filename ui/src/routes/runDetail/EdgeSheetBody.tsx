/**
 * Body component for the run-detail edge Sheet.
 *
 * Rendered inside :class:`SheetHost` when the operator clicks an edge
 * in the run progress graph. The body lays out:
 *
 *   - Source state KeyValueTable (id, kind, status of the matching
 *     run entry if any).
 *   - Target state KeyValueTable (same shape).
 *   - The transition's predicate text, syntax-highlighted via the
 *     shared :func:`tokenisePredicate` helper.
 *   - The ``transition_taken`` event for this edge if it landed in the
 *     loaded event slice, with its ``when_taken_at`` timestamp.
 *   - A "Not taken in this run" banner when neither the state tree nor
 *     the events show the edge firing.
 */

import type { JSX } from 'preact';
import { useMemo } from 'preact/hooks';

import { JsonViewer } from '../../components/JsonViewer';
import { KeyValueTable, type KvRow } from '../../components/KeyValueTable';
import { Pill, type PillVariant } from '../../components/Pill';
import { EmptyState } from '../../components/EmptyState';
import type {
  Event as FsmEvent,
  SpecDetail,
  StateNode,
} from '../../lib/api';
import {
  classForPredicateKind,
  tokenisePredicate,
} from '../../lib/predicateTokens';

export interface EdgeSheetBodyProps {
  fromStateId: string;
  toStateId: string;
  runId: string;
  stateTree: StateNode | null;
  spec: SpecDetail | null;
  events: FsmEvent[];
}

/** Variant for a state's run status — matches the pattern used in
 *  StateEntrySheetBody / runDetail.tsx so the same colour means the
 *  same thing across the run-detail surface. */
function variantForStatus(status: string | null | undefined): PillVariant {
  if (!status) return 'neutral';
  const s = status.toLowerCase();
  if (s === 'completed' || s === 'exited' || s === 'succeeded') return 'success';
  if (s === 'faulted' || s === 'failed' || s === 'aborted') return 'danger';
  if (s === 'paused' || s === 'waiting') return 'warning';
  if (s === 'running' || s === 'entered' || s === 'in_progress') return 'info';
  return 'neutral';
}

/** Format an ISO timestamp the same way the rest of run-detail does. */
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

/** Walk the state tree and emit every entry. */
function collectEntries(root: StateNode | null): StateNode[] {
  if (!root) return [];
  const out: StateNode[] = [];
  const walk = (n: StateNode): void => {
    out.push(n);
    for (const child of n.children) walk(child);
  };
  walk(root);
  return out;
}

/** Find the most recently-entered run entry for ``state_id``. We
 *  prefer the highest ``entry_seq`` so a state that was visited
 *  multiple times (loop iteration) reports its current snapshot. */
function findLatestEntry(
  entries: StateNode[],
  stateId: string,
): StateNode | null {
  let latest: StateNode | null = null;
  for (const e of entries) {
    if (e.state_id !== stateId) continue;
    if (!latest || e.entry_seq > latest.entry_seq) {
      latest = e;
    }
  }
  return latest;
}

/** Find the spec-side state object for ``state_id`` (mirrors the same
 *  helper in StateEntrySheetBody but kept private here so the two
 *  sheets stay independently importable for testing). */
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

/** Extract the human-readable predicate text from a transition's
 *  ``when`` payload. Mirrors specGraph.predicateLabel() so the edge
 *  inspector text matches the on-graph edge label byte-for-byte. */
function predicateOf(
  transition: Record<string, unknown> | null,
): string {
  if (!transition) return '';
  const when = transition.when;
  if (typeof when === 'string') return when;
  if (when && typeof when === 'object') {
    const obj = when as Record<string, unknown>;
    if (typeof obj.predicate === 'string') return obj.predicate;
    if (typeof obj.expression === 'string') return obj.expression;
    if (typeof obj.criteria === 'string') return obj.criteria;
  }
  return '';
}

/** Find the declared transition spec from ``fromState`` to ``toStateId``.
 *  Returns the first matching transition since the spec allows multiple
 *  declared transitions between the same endpoint pair (e.g. a
 *  predicate + otherwise fallback). The edge sheet shows the predicate
 *  on the first match; future iterations may extend to show all
 *  parallel transitions. */
function findTransition(
  fromState: Record<string, unknown> | null,
  toStateId: string,
): Record<string, unknown> | null {
  if (!fromState) return null;
  const transitions = Array.isArray(fromState.transitions)
    ? (fromState.transitions as unknown[])
    : [];
  for (const t of transitions) {
    if (t && typeof t === 'object') {
      const obj = t as Record<string, unknown>;
      if (obj.to === toStateId) return obj;
    }
  }
  return null;
}

/** True when the event represents the transition from ``fromStateId``
 *  to ``toStateId`` actually firing. The engine's ``transition_taken``
 *  event encodes the endpoints under either ``from``/``to`` or
 *  ``from_state``/``to_state`` depending on the wire-format version
 *  the producer emitted; we probe both pairs so the filter doesn't
 *  silently drop a relevant event. */
function eventIsThisTransition(
  event: FsmEvent,
  fromStateId: string,
  toStateId: string,
): boolean {
  if (!event.kind.toLowerCase().includes('transition')) return false;
  const p = event.payload as Record<string, unknown>;
  const from =
    (typeof p.from === 'string' && p.from) ||
    (typeof p.from_state === 'string' && p.from_state) ||
    null;
  const to =
    (typeof p.to === 'string' && p.to) ||
    (typeof p.to_state === 'string' && p.to_state) ||
    null;
  return from === fromStateId && to === toStateId;
}

/** Extract the ``when_taken_at`` timestamp from a transition_taken
 *  event payload, with fallbacks to common spellings. */
function pickWhenTakenAt(event: FsmEvent | null): string | null {
  if (!event) return null;
  const p = event.payload as Record<string, unknown>;
  const candidates = [p.when_taken_at, p.whenTakenAt, p.taken_at];
  for (const c of candidates) {
    if (typeof c === 'string') return c;
  }
  // Fall back to the event's own created_at so the operator always
  // sees SOME timestamp for a recorded transition.
  return event.created_at;
}

export function EdgeSheetBody({
  fromStateId,
  toStateId,
  runId,
  stateTree,
  spec,
  events,
}: EdgeSheetBodyProps): JSX.Element {
  const entries = useMemo(() => collectEntries(stateTree), [stateTree]);
  const fromEntry = useMemo(
    () => findLatestEntry(entries, fromStateId),
    [entries, fromStateId],
  );
  const toEntry = useMemo(
    () => findLatestEntry(entries, toStateId),
    [entries, toStateId],
  );
  const fromSpec = useMemo(
    () => findSpecState(spec, fromStateId),
    [spec, fromStateId],
  );
  const transition = useMemo(
    () => findTransition(fromSpec, toStateId),
    [fromSpec, toStateId],
  );
  const predicate = predicateOf(transition);
  const tokens = useMemo(
    () => (predicate ? tokenisePredicate(predicate) : []),
    [predicate],
  );

  const transitionEvent = useMemo(() => {
    for (const e of events) {
      if (eventIsThisTransition(e, fromStateId, toStateId)) return e;
    }
    return null;
  }, [events, fromStateId, toStateId]);

  // "Taken" is true when EITHER the run actually traversed parent->child
  // in the state tree (fromEntry has a child whose state_id == toStateId)
  // OR we observed a transition_taken event for this edge. The state-tree
  // signal is authoritative; the event signal lets us reflect transitions
  // that landed via a non-tree edge (loop back-edge) without waiting for
  // the next state tree refresh.
  const takenViaTree =
    fromEntry !== null &&
    fromEntry.children.some((c) => c.state_id === toStateId);
  const taken = takenViaTree || transitionEvent !== null;

  const fromRows: KvRow[] = useMemo(() => {
    const rows: KvRow[] = [
      { key: 'state_id', value: fromStateId },
    ];
    if (fromSpec?.kind) rows.push({ key: 'kind', value: String(fromSpec.kind) });
    if (fromEntry) {
      rows.push({ key: 'status', value: fromEntry.status });
      rows.push({ key: 'entered_at', value: formatTimestamp(fromEntry.entered_at) });
      rows.push({ key: 'exited_at', value: formatTimestamp(fromEntry.exited_at) });
    } else {
      rows.push({ key: 'visited', value: false });
    }
    return rows;
  }, [fromStateId, fromSpec, fromEntry]);

  const toRows: KvRow[] = useMemo(() => {
    const toSpec = findSpecState(spec, toStateId);
    const rows: KvRow[] = [
      { key: 'state_id', value: toStateId },
    ];
    if (toSpec?.kind) rows.push({ key: 'kind', value: String(toSpec.kind) });
    if (toEntry) {
      rows.push({ key: 'status', value: toEntry.status });
      rows.push({ key: 'entered_at', value: formatTimestamp(toEntry.entered_at) });
      rows.push({ key: 'exited_at', value: formatTimestamp(toEntry.exited_at) });
    } else {
      rows.push({ key: 'visited', value: false });
    }
    return rows;
  }, [spec, toStateId, toEntry]);

  return (
    <div
      class="p-3 space-y-4"
      data-testid="edge-sheet-body"
      data-from={fromStateId}
      data-to={toStateId}
    >
      <header class="flex flex-wrap items-baseline gap-2">
        <code class="font-mono text-sm text-slate-900 dark:text-slate-100">
          {fromStateId}
        </code>
        <span class="text-slate-400">→</span>
        <code class="font-mono text-sm text-slate-900 dark:text-slate-100">
          {toStateId}
        </code>
        <Pill variant={taken ? 'success' : 'neutral'} size="sm">
          {taken ? 'taken' : 'not taken'}
        </Pill>
        <span class="text-[10px] font-mono text-slate-400 dark:text-slate-500">
          run {runId}
        </span>
      </header>

      <section>
        <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
          source state
          {fromEntry ? (
            <Pill variant={variantForStatus(fromEntry.status)} size="sm" className="ml-2">
              {fromEntry.status}
            </Pill>
          ) : null}
        </h4>
        <KeyValueTable rows={fromRows} caption={`Source state ${fromStateId}`} />
      </section>

      <section>
        <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
          target state
          {toEntry ? (
            <Pill variant={variantForStatus(toEntry.status)} size="sm" className="ml-2">
              {toEntry.status}
            </Pill>
          ) : null}
        </h4>
        <KeyValueTable rows={toRows} caption={`Target state ${toStateId}`} />
      </section>

      <section>
        <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
          predicate
        </h4>
        {predicate ? (
          <code
            class="block font-mono text-xs leading-relaxed whitespace-pre-wrap break-words px-2 py-1.5 rounded bg-amber-900/80 dark:bg-amber-900/70 border border-amber-500 dark:border-amber-600 text-amber-50"
            title={predicate}
          >
            {tokens.map((t, i) => (
              <span key={i} class={classForPredicateKind(t.kind)}>
                {t.text}
              </span>
            ))}
          </code>
        ) : (
          <p class="text-xs text-slate-500 dark:text-slate-400">
            No predicate declared for this transition (typically ``always`` /
            ``otherwise``).
          </p>
        )}
      </section>

      <section>
        <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
          transition event
        </h4>
        {transitionEvent ? (
          <div class="space-y-2">
            <KeyValueTable
              rows={[
                { key: 'kind', value: transitionEvent.kind },
                { key: 'producer_id', value: transitionEvent.producer_id },
                {
                  key: 'when_taken_at',
                  value: formatTimestamp(pickWhenTakenAt(transitionEvent)),
                },
              ]}
              caption="Transition event metadata"
            />
            <JsonViewer
              value={transitionEvent.payload}
              rootLabel="payload"
              mode="inline"
              maxInlineHeight="max-h-48"
              ariaLabel={`Transition event ${transitionEvent.id} payload`}
            />
          </div>
        ) : taken ? (
          <p class="text-xs text-slate-500 dark:text-slate-400">
            Edge traversal recorded in the state tree, but the
            ``transition_taken`` event has not landed in the loaded slice.
          </p>
        ) : (
          <EmptyState
            title="Not taken in this run"
            message="Neither the state tree nor the loaded events show this edge firing."
          />
        )}
      </section>
    </div>
  );
}

export default EdgeSheetBody;
