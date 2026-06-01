/**
 * /specs/:specId — per-spec dashboard.
 *
 * This is the post-W19 spec-first IA: the user lands on /specs,
 * picks a spec, lands here. From this page they see EVERYTHING
 * scoped to that spec — graph, runs of this spec, schemas,
 * definition, version timeline.
 *
 * Tabs: Graph · Runs · Schemas · Definition · Versions.
 *
 * "Runs" is the spec-scoped equivalent of the global /runs list,
 * filtered to runs whose `fsm_spec_id` matches this spec (or any
 * sibling version of the same slug).
 */

import type { JSX } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';
import { useRoute } from 'preact-iso';

import {
  Card,
  Diff,
  EmptyState,
  FlowGraph,
  JsonViewer,
  Pill,
  Spinner,
  Table,
  Tabs,
  type PillVariant,
  type TabSpec,
  type TableColumn,
} from '../components';
import {
  api,
  ApiError,
  type RunSummary,
  type SpecDetail,
  type SpecSummary,
} from '../lib/api';
import { canonicalJson } from '../lib/canonicalJson';
import { specToGraph } from '../lib/specGraph';

const shortHash = (h: string): string => (h.length > 12 ? h.slice(0, 12) : h);
const shortId = (id: string): string => (id.length > 7 ? id.slice(0, 7) : id);

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function statusVariant(status: string): PillVariant {
  switch (status) {
    case 'completed': return 'success';
    case 'in_progress': return 'info';
    case 'paused': return 'warning';
    case 'faulted':
    case 'aborted':
    case 'drift_paused': return 'danger';
    default: return 'neutral';
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface SpecSiblings {
  /** The selected spec (full detail). */
  detail: SpecDetail;
  /** All registered specs (used to find sibling versions of this slug). */
  all: SpecSummary[];
}

function siblingsOf(detail: SpecDetail, all: SpecSummary[]): SpecSummary[] {
  return all
    .filter((s) => s.slug === detail.slug)
    .sort((a, b) => b.version - a.version);
}

// ---------------------------------------------------------------------------
// Tab panels
// ---------------------------------------------------------------------------

function GraphPanel({ detail }: { detail: SpecDetail }): JSX.Element {
  const graph = useMemo(() => specToGraph(detail.definition), [detail.definition]);
  return (
    <div class="h-[70vh] p-3">
      <FlowGraph
        nodes={graph.nodes}
        edges={graph.edges}
        autoLayout={true}
        direction="TB"
      />
    </div>
  );
}

interface RunsPanelProps {
  spec: SpecSiblings;
  navigate: (path: string) => void;
}

function RunsPanel({ spec, navigate }: RunsPanelProps): JSX.Element {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Match against ALL sibling versions so the user sees every run
  // of the slug, not just the currently-selected version.
  const matchIds = useMemo(
    () => new Set(spec.all.filter((s) => s.slug === spec.detail.slug).map((s) => s.id)),
    [spec],
  );

  useEffect(() => {
    let cancelled = false;
    api.listRuns({ limit: 500 })
      .then((all) => {
        if (cancelled) return;
        setRuns(all.filter((r) => matchIds.has(r.fsm_spec_id)));
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [matchIds]);

  if (error) return <EmptyState title="Failed to load runs" message={error} />;
  if (runs === null) {
    return (
      <div class="flex items-center justify-center py-12">
        <Spinner label="Loading runs" />
      </div>
    );
  }
  if (runs.length === 0) {
    return (
      <EmptyState
        title="No runs for this spec yet"
        message={`Start a run of ${spec.detail.slug} via the CLI or MCP server to see it here.`}
      />
    );
  }

  const versionLabel = (id: string): string => {
    const v = spec.all.find((s) => s.id === id);
    return v ? `v${v.version}` : id.slice(0, 7);
  };

  const cols: TableColumn<RunSummary>[] = [
    {
      key: 'id', label: 'ID',
      render: (r) => (
        <code class="font-mono text-xs text-slate-700 dark:text-slate-300">{shortId(r.id)}</code>
      ),
    },
    {
      key: 'fsm_spec_id', label: 'Version', width: '5rem',
      render: (r) => <Pill variant="info" size="sm">{versionLabel(r.fsm_spec_id)}</Pill>,
    },
    {
      key: 'status', label: 'Status', width: '8rem',
      render: (r) => <Pill variant={statusVariant(r.status)} size="sm">{r.status}</Pill>,
    },
    {
      key: 'current_state', label: 'Current state',
      render: (r) => <code class="text-xs text-slate-700 dark:text-slate-300">{r.current_state ?? '—'}</code>,
    },
    {
      key: 'started_at', label: 'Started', width: '12rem',
      render: (r) => <span class="text-xs text-slate-600 dark:text-slate-400">{formatTimestamp(r.started_at)}</span>,
    },
    {
      key: 'last_update_at', label: 'Last update', width: '12rem',
      render: (r) => <span class="text-xs text-slate-600 dark:text-slate-400">{formatTimestamp(r.last_update_at)}</span>,
    },
  ];

  return (
    <Table<RunSummary>
      columns={cols}
      rows={runs.sort((a, b) => (b.last_update_at ?? '').localeCompare(a.last_update_at ?? ''))}
      rowKey={(r) => r.id}
      onRowClick={(r) => navigate(`/runs/${encodeURIComponent(r.id)}`)}
      caption={`Runs of ${spec.detail.slug}`}
    />
  );
}

function SchemasPanel({ detail }: { detail: SpecDetail }): JSX.Element {
  const def = (detail.definition as Record<string, unknown>) ?? {};
  const states = Array.isArray(def.states) ? def.states : [];
  const withSchemas = (states as Record<string, unknown>[]).filter((s) => {
    const w = s.worker as Record<string, unknown> | undefined;
    return w && w.response_schema;
  });
  if (withSchemas.length === 0) {
    return <EmptyState title="No worker schemas" message="This spec has no worker response schemas declared." />;
  }
  return (
    <div class="space-y-4">
      {withSchemas.map((s) => {
        const sid = String(s.id);
        const worker = s.worker as Record<string, unknown>;
        return (
          <div key={sid}>
            <h4 class="text-xs uppercase tracking-wide text-slate-600 dark:text-slate-400 mb-1">
              {sid}
            </h4>
            <JsonViewer
              value={worker.response_schema}
              rootLabel="response_schema"
              mode="inline"
              maxInlineHeight="max-h-48"
            />
          </div>
        );
      })}
    </div>
  );
}

function DefinitionPanel({ detail }: { detail: SpecDetail }): JSX.Element {
  return (
    <JsonViewer
      value={detail.definition}
      rootLabel="definition"
      mode="expanded"
      defaultExpandDepth={3}
      downloadFilename={`spec-${detail.slug}-v${detail.version}.json`}
    />
  );
}

interface VersionsPanelProps {
  spec: SpecSiblings;
  navigate: (path: string) => void;
}

function VersionsPanel({ spec, navigate }: VersionsPanelProps): JSX.Element {
  const [compareWith, setCompareWith] = useState<string | null>(null);
  const [compareDetail, setCompareDetail] = useState<SpecDetail | null>(null);
  const siblings = useMemo(() => siblingsOf(spec.detail, spec.all), [spec]);

  useEffect(() => {
    setCompareWith(null);
    setCompareDetail(null);
  }, [spec.detail.id]);

  useEffect(() => {
    if (!compareWith) return;
    let cancelled = false;
    api.getSpec(compareWith)
      .then((d) => { if (!cancelled) setCompareDetail(d); })
      .catch(() => { if (!cancelled) setCompareDetail(null); });
    return () => { cancelled = true; };
  }, [compareWith]);

  if (siblings.length <= 1) {
    return (
      <p class="text-sm text-slate-600 dark:text-slate-400 py-2">
        Only one version registered for this slug.
      </p>
    );
  }

  return (
    <div class="space-y-3">
      <div class="text-xs text-slate-600 dark:text-slate-400">
        {siblings.length} versions of <code class="font-mono">{spec.detail.slug}</code>. Click a row to switch; pick another from the dropdown to diff against the current selection.
      </div>
      <ul class="divide-y divide-slate-100 dark:divide-slate-800 text-xs font-mono">
        {siblings.map((s) => (
          <li key={s.id} class="py-1.5 flex items-center gap-2">
            <button
              type="button"
              onClick={() => navigate(`/specs/${encodeURIComponent(s.id)}`)}
              class={[
                'text-left flex-1 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm',
                s.id === spec.detail.id ? 'text-emerald-700 dark:text-emerald-400 font-semibold' : '',
              ].join(' ')}
            >
              v{s.version}
            </button>
            <code class="text-slate-500 dark:text-slate-400">{shortHash(s.hash)}</code>
            <span class="text-slate-500 dark:text-slate-400">{formatTimestamp(s.created_at)}</span>
          </li>
        ))}
      </ul>
      <div class="flex items-center gap-2 text-sm pt-2 border-t border-slate-200 dark:border-slate-700">
        <label for="spec-diff-target">Diff against:</label>
        <select
          id="spec-diff-target"
          aria-label="Pick a version to diff against"
          value={compareWith ?? ''}
          onChange={(e) => setCompareWith((e.target as HTMLSelectElement).value || null)}
          class="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 text-slate-900 dark:text-slate-100 rounded-md px-2 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
        >
          <option value="">— pick a version —</option>
          {siblings.filter((s) => s.id !== spec.detail.id).map((s) => (
            <option value={s.id} key={s.id}>v{s.version} ({shortHash(s.hash)})</option>
          ))}
        </select>
      </div>
      {compareDetail ? (
        <Diff
          before={canonicalJson(compareDetail.definition)}
          after={canonicalJson(spec.detail.definition)}
          label={`v${compareDetail.version} → v${spec.detail.version}`}
        />
      ) : compareWith ? (
        <div class="flex items-center gap-2 text-xs text-slate-500"><Spinner size="sm" /> Loading...</div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function SpecDetailRoute(): JSX.Element {
  const { params } = useRoute();
  const specId = params.specId ?? '';
  const [activeTab, setActiveTab] = useState<string>('graph');
  const [detail, setDetail] = useState<SpecDetail | null>(null);
  const [allSpecs, setAllSpecs] = useState<SpecSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runCount, setRunCount] = useState<number | null>(null);

  const navigate = (path: string): void => {
    if (typeof window === 'undefined') return;
    window.history.pushState(null, '', path);
    window.dispatchEvent(new PopStateEvent('popstate'));
  };

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    api.getSpec(specId)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [specId]);

  useEffect(() => {
    let cancelled = false;
    api.listSpecs()
      .then((all) => { if (!cancelled) setAllSpecs(all); })
      .catch(() => { if (!cancelled) setAllSpecs([]); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    // Compute run count across all sibling versions for the header badge.
    if (!detail || !allSpecs) return;
    let cancelled = false;
    const matchIds = new Set(allSpecs.filter((s) => s.slug === detail.slug).map((s) => s.id));
    api.listRuns({ limit: 500 })
      .then((rs) => { if (!cancelled) setRunCount(rs.filter((r) => matchIds.has(r.fsm_spec_id)).length); })
      .catch(() => { if (!cancelled) setRunCount(null); });
    return () => { cancelled = true; };
  }, [detail, allSpecs]);

  if (error) {
    return (
      <div class="p-4 md:p-6">
        <Card>
          <EmptyState title="Failed to load spec" message={error} />
        </Card>
      </div>
    );
  }
  if (!detail || allSpecs === null) {
    return (
      <div class="p-4 md:p-6">
        <Card>
          <div class="flex items-center justify-center py-12"><Spinner label="Loading spec" /></div>
        </Card>
      </div>
    );
  }

  const spec: SpecSiblings = { detail, all: allSpecs };
  const siblings = siblingsOf(detail, allSpecs);

  const tabs: TabSpec[] = [
    { id: 'graph', label: 'Graph' },
    { id: 'runs', label: 'Runs', badge: runCount !== null ? <Pill variant="neutral" size="sm">{runCount}</Pill> : undefined },
    { id: 'schemas', label: 'Schemas' },
    { id: 'definition', label: 'Definition' },
    { id: 'versions', label: 'Versions', badge: <Pill variant="neutral" size="sm">{siblings.length}</Pill> },
  ];

  return (
    <div class="p-4 md:p-6 space-y-4 flex flex-col h-full min-h-0">
      <header class="space-y-2">
        <div class="flex items-center gap-2 text-xs">
          <a
            href="/specs"
            class="text-slate-500 dark:text-slate-400 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
          >
            ← All specs
          </a>
        </div>
        <div class="flex flex-wrap items-baseline gap-2">
          <h1 class="text-2xl font-semibold text-slate-900 dark:text-slate-100">{detail.slug}</h1>
          <Pill variant="info">v{detail.version}</Pill>
          <code class="text-xs font-mono text-slate-600 dark:text-slate-400" title={detail.hash}>
            {shortHash(detail.hash)}
          </code>
          <span class="ml-auto text-xs text-slate-600 dark:text-slate-400">
            Registered {formatTimestamp(detail.registered_at)}
          </span>
        </div>
      </header>
      <Card className="flex-1 min-h-0 p-0">
        <Tabs
          tabs={tabs}
          activeTab={activeTab}
          onChange={setActiveTab}
          panels={{
            graph: <GraphPanel detail={detail} />,
            runs: <div class="p-3"><RunsPanel spec={spec} navigate={navigate} /></div>,
            schemas: <div class="p-3"><SchemasPanel detail={detail} /></div>,
            definition: <div class="p-3"><DefinitionPanel detail={detail} /></div>,
            versions: <div class="p-3"><VersionsPanel spec={spec} navigate={navigate} /></div>,
          }}
        />
      </Card>
    </div>
  );
}

export default SpecDetailRoute;
