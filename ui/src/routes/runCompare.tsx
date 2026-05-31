/**
 * /runs/:id/compare/:otherId — side-by-side run comparison.
 *
 * Three tabs (Tabs from W18b):
 *   - Args & Metadata : Diff over canonicalJson of both run.args
 *                       (and metadata) sets.
 *   - State sequence  : two state-tree lists side-by-side, aligned
 *                       on (state_id, iteration_n).
 *   - Event timeline  : two event lists side-by-side, aligned on
 *                       (seq, kind).
 *
 * Header carries swap-A↔B + same-spec / different-spec indicator.
 */

import type { JSX } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';
import { useRoute } from 'preact-iso';

import {
  Card,
  Diff,
  EmptyState,
  Pill,
  Spinner,
  Tabs,
  type TabSpec,
} from '../components';
import {
  api,
  ApiError,
  type Event as FsmEvent,
  type RunDetail,
  type StateNode,
} from '../lib/api';
import { canonicalJson } from '../lib/canonicalJson';

interface Loaded {
  run: RunDetail;
  events: FsmEvent[];
}

async function loadRun(id: string): Promise<Loaded> {
  const [run, events] = await Promise.all([
    api.getRun(id),
    api.getEvents(id, { limit: 500 }),
  ]);
  return { run, events };
}

interface FlatState {
  state_id: string;
  iteration_n: number | null;
  status: string;
  entry_seq: number;
}

function flattenStateTree(root: StateNode | null): FlatState[] {
  if (!root) return [];
  const out: FlatState[] = [];
  const walk = (n: StateNode) => {
    out.push({
      state_id: n.state_id,
      iteration_n: n.iteration_n,
      status: n.status,
      entry_seq: n.entry_seq,
    });
    for (const c of n.children ?? []) walk(c);
  };
  walk(root);
  out.sort((a, b) => a.entry_seq - b.entry_seq);
  return out;
}

interface PairedRow<T> { a: T | null; b: T | null; same: boolean; }

function pairStates(aList: FlatState[], bList: FlatState[]): PairedRow<FlatState>[] {
  // Simple alignment: walk both lists step-by-step. Where they match
  // by (state_id, iteration_n), pair them; otherwise emit one-sided
  // rows.
  const rows: PairedRow<FlatState>[] = [];
  let i = 0;
  let j = 0;
  while (i < aList.length && j < bList.length) {
    const a = aList[i];
    const b = bList[j];
    if (a.state_id === b.state_id && a.iteration_n === b.iteration_n) {
      rows.push({ a, b, same: a.status === b.status });
      i++; j++;
    } else if (a.state_id === b.state_id) {
      // Different iteration; advance the smaller.
      const ai = a.iteration_n ?? -1;
      const bi = b.iteration_n ?? -1;
      if (ai < bi) { rows.push({ a, b: null, same: false }); i++; }
      else { rows.push({ a: null, b, same: false }); j++; }
    } else {
      rows.push({ a, b: null, same: false });
      i++;
    }
  }
  while (i < aList.length) { rows.push({ a: aList[i++], b: null, same: false }); }
  while (j < bList.length) { rows.push({ a: null, b: bList[j++], same: false }); }
  return rows;
}

function pairEvents(aList: FsmEvent[], bList: FsmEvent[]): PairedRow<FsmEvent>[] {
  const rows: PairedRow<FsmEvent>[] = [];
  const max = Math.max(aList.length, bList.length);
  for (let i = 0; i < max; i++) {
    const a = aList[i] ?? null;
    const b = bList[i] ?? null;
    const same =
      a !== null && b !== null &&
      a.kind === b.kind &&
      canonicalJson(a.payload) === canonicalJson(b.payload);
    rows.push({ a, b, same });
  }
  return rows;
}

interface SideHeaderProps { run: RunDetail; label: string; }
function SideHeader({ run, label }: SideHeaderProps): JSX.Element {
  return (
    <div class="text-xs space-y-1">
      <div class="flex items-baseline gap-1">
        <span class="text-[10px] uppercase text-slate-500">{label}</span>
        <code class="font-mono">{run.manifest.id.slice(0, 7)}</code>
      </div>
      <div class="flex flex-wrap gap-1">
        <Pill variant="info" size="sm">{run.manifest.status}</Pill>
        {run.manifest.verdict ? <Pill variant="success" size="sm">{run.manifest.verdict}</Pill> : null}
      </div>
    </div>
  );
}

export function RunCompareRoute(): JSX.Element {
  const { params } = useRoute();
  const idA = params.id ?? '';
  const idB = params.otherId ?? '';
  const [a, setA] = useState<Loaded | null>(null);
  const [b, setB] = useState<Loaded | null>(null);
  const [aErr, setAErr] = useState<string | null>(null);
  const [bErr, setBErr] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState('args');

  useEffect(() => {
    let cancelled = false;
    loadRun(idA)
      .then((r) => { if (!cancelled) setA(r); })
      .catch((err: unknown) => {
        if (!cancelled) setAErr(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [idA]);

  useEffect(() => {
    let cancelled = false;
    loadRun(idB)
      .then((r) => { if (!cancelled) setB(r); })
      .catch((err: unknown) => {
        if (!cancelled) setBErr(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [idB]);

  const swap = () => {
    if (typeof window !== 'undefined') {
      window.history.pushState(null, '', `/runs/${idB}/compare/${idA}`);
      window.dispatchEvent(new PopStateEvent('popstate'));
    }
  };

  const sameSpec = useMemo(() => {
    if (!a || !b) return null;
    return a.run.manifest.fsm_spec_hash === b.run.manifest.fsm_spec_hash;
  }, [a, b]);

  const argsBefore = a ? canonicalJson({ args: a.run.manifest.args, metadata: a.run.manifest.metadata }) : '';
  const argsAfter = b ? canonicalJson({ args: b.run.manifest.args, metadata: b.run.manifest.metadata }) : '';

  const aStates = useMemo(() => flattenStateTree(a?.run.state_tree ?? null), [a]);
  const bStates = useMemo(() => flattenStateTree(b?.run.state_tree ?? null), [b]);
  const statePairs = useMemo(() => pairStates(aStates, bStates), [aStates, bStates]);
  const eventPairs = useMemo(() => pairEvents(a?.events ?? [], b?.events ?? []), [a, b]);

  if (aErr || bErr) {
    return (
      <div class="p-4 md:p-6">
        <Card>
          <EmptyState
            title="Failed to load runs"
            message={[aErr, bErr].filter(Boolean).join(' / ')}
          />
        </Card>
      </div>
    );
  }
  if (!a || !b) {
    return (
      <div class="p-4 md:p-6">
        <Card>
          <div class="flex items-center justify-center py-12">
            <Spinner label="Loading runs" />
          </div>
        </Card>
      </div>
    );
  }

  const tabs: TabSpec[] = [
    { id: 'args', label: 'Args & Metadata' },
    { id: 'states', label: 'State sequence', badge: <Pill variant="neutral" size="sm">{statePairs.length}</Pill> },
    { id: 'events', label: 'Event timeline', badge: <Pill variant="neutral" size="sm">{eventPairs.length}</Pill> },
  ];

  return (
    <div class="p-4 md:p-6 space-y-4 flex flex-col h-full min-h-0">
      <header class="flex flex-wrap items-center justify-between gap-4">
        <div class="flex items-center gap-4">
          <SideHeader run={a.run} label="A" />
          <div class="text-xl">↔</div>
          <SideHeader run={b.run} label="B" />
        </div>
        <div class="flex items-center gap-2">
          {sameSpec !== null ? (
            <Pill variant={sameSpec ? 'success' : 'warning'} size="sm">
              {sameSpec ? 'same spec' : 'different spec'}
            </Pill>
          ) : null}
          <button
            type="button"
            onClick={swap}
            class="h-8 px-3 text-xs rounded-md bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
          >
            Swap A ↔ B
          </button>
        </div>
      </header>
      <Card className="flex-1 min-h-0 p-0">
        <Tabs
          tabs={tabs}
          activeTab={activeTab}
          onChange={setActiveTab}
          panels={{
            args: (
              <div class="p-3">
                <Diff before={argsBefore} after={argsAfter} label="A → B" />
              </div>
            ),
            states: (
              <div class="p-3 overflow-auto">
                <table class="w-full text-xs font-mono">
                  <thead>
                    <tr class="text-slate-500 text-[10px] uppercase">
                      <th class="text-left py-1">A: state · iter · status</th>
                      <th class="text-left py-1">B: state · iter · status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {statePairs.map((p, idx) => (
                      <tr key={idx} class={p.same ? '' : 'bg-red-50 dark:bg-red-900/20'}>
                        <td class="py-0.5 pr-2">{p.a ? `${p.a.state_id} · ${p.a.iteration_n ?? '-'} · ${p.a.status}` : ''}</td>
                        <td class="py-0.5">{p.b ? `${p.b.state_id} · ${p.b.iteration_n ?? '-'} · ${p.b.status}` : ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ),
            events: (
              <div class="p-3 overflow-auto">
                <table class="w-full text-xs font-mono">
                  <thead>
                    <tr class="text-slate-500 text-[10px] uppercase">
                      <th class="text-left py-1">A: seq · kind</th>
                      <th class="text-left py-1">B: seq · kind</th>
                    </tr>
                  </thead>
                  <tbody>
                    {eventPairs.map((p, idx) => (
                      <tr key={idx} class={p.same ? '' : 'bg-amber-50 dark:bg-amber-900/20'}>
                        <td class="py-0.5 pr-2">{p.a ? `${p.a.seq} · ${p.a.kind}` : ''}</td>
                        <td class="py-0.5">{p.b ? `${p.b.seq} · ${p.b.kind}` : ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ),
          }}
        />
      </Card>
    </div>
  );
}

export default RunCompareRoute;
