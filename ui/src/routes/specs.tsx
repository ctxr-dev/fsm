/**
 * /specs v2 — master / detail with FSM graph + version timeline.
 *
 * Layout: 40/60 split (stacks on mobile). Master = master list of
 * specs grouped by slug; detail = selected spec rendered as a
 * tabbed view (Graph / Schemas / Definition / Versions). The graph
 * uses the W18b FlowGraph primitive to render the state diagram
 * (n8n/Dify-style nodes + edges with predicate labels) so a non-
 * trivial spec is comprehensible at a glance.
 */

import type { JSX } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';

import {
  Card,
  Diff,
  EmptyState,
  FlowGraph,
  JsonViewer,
  Pill,
  Spinner,
  Tabs,
  type TabSpec,
} from '../components';
import { api, ApiError, type SpecDetail, type SpecSummary } from '../lib/api';
import { canonicalJson } from '../lib/canonicalJson';
import { specToGraph } from '../lib/specGraph';

const shortHash = (h: string): string => (h.length > 12 ? h.slice(0, 12) : h);

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// ---------------------------------------------------------------------------
// Master list grouped by slug
// ---------------------------------------------------------------------------

interface SpecGroup {
  slug: string;
  rows: SpecSummary[]; // sorted by version desc
}

function groupBySlug(specs: readonly SpecSummary[]): SpecGroup[] {
  const map = new Map<string, SpecSummary[]>();
  for (const s of specs) {
    if (!map.has(s.slug)) map.set(s.slug, []);
    map.get(s.slug)!.push(s);
  }
  const groups: SpecGroup[] = [];
  for (const [slug, rows] of map.entries()) {
    rows.sort((a, b) => b.version - a.version);
    groups.push({ slug, rows });
  }
  groups.sort((a, b) => a.slug.localeCompare(b.slug));
  return groups;
}

interface MasterListProps {
  groups: readonly SpecGroup[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

function MasterList({ groups, selectedId, onSelect }: MasterListProps): JSX.Element {
  const [expandedSlugs, setExpandedSlugs] = useState<Set<string>>(new Set());
  const toggle = (slug: string) =>
    setExpandedSlugs((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });

  if (groups.length === 0) {
    return (
      <EmptyState
        title="No specs registered"
        message="Register an FSM spec via POST /api/v1/specs."
      />
    );
  }
  return (
    <ul class="divide-y divide-slate-200 dark:divide-slate-700" aria-label="Registered FSM specs">
      {groups.map((g) => {
        const latest = g.rows[0];
        const expanded = expandedSlugs.has(g.slug);
        return (
          <li key={g.slug}>
            <div class="flex items-center gap-1 py-2 px-3">
              <button
                type="button"
                onClick={() => toggle(g.slug)}
                aria-label={expanded ? `Collapse ${g.slug}` : `Expand ${g.slug}`}
                aria-expanded={expanded}
                class="inline-flex w-4 h-4 items-center justify-center text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
              >
                <svg
                  viewBox="0 0 20 20"
                  fill="currentColor"
                  class={['w-3 h-3 transition-transform', expanded ? 'rotate-90' : ''].join(' ')}
                  aria-hidden="true"
                >
                  <path
                    fill-rule="evenodd"
                    d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z"
                    clip-rule="evenodd"
                  />
                </svg>
              </button>
              <button
                type="button"
                onClick={() => onSelect(latest.id)}
                class={[
                  'flex-1 text-left flex items-center gap-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm',
                  selectedId === latest.id
                    ? 'font-semibold text-slate-900 dark:text-slate-100'
                    : 'text-slate-700 dark:text-slate-300',
                ].join(' ')}
              >
                <span class="truncate">{g.slug}</span>
                <Pill variant="info" size="sm">v{latest.version}</Pill>
                <span class="text-[10px] text-slate-500 ml-auto">
                  {g.rows.length} version{g.rows.length === 1 ? '' : 's'}
                </span>
              </button>
            </div>
            {expanded ? (
              <ul class="ml-7 mb-2 space-y-0.5">
                {g.rows.map((r) => (
                  <li key={r.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(r.id)}
                      class={[
                        'block w-full text-left text-xs py-1 px-2 rounded-sm font-mono',
                        selectedId === r.id
                          ? 'bg-emerald-50 dark:bg-emerald-900/30 text-slate-900 dark:text-slate-100'
                          : 'text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800/50',
                      ].join(' ')}
                    >
                      v{r.version} · {shortHash(r.hash)} · {formatTimestamp(r.created_at)}
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Detail pane (tabs: Graph / Schemas / Definition / Versions)
// ---------------------------------------------------------------------------

interface DetailPaneProps {
  detail: SpecDetail | null;
  detailError: string | null;
  /** All versions of the selected slug (for the Versions tab). */
  siblings: SpecSummary[];
}

function DetailPane({ detail, detailError, siblings }: DetailPaneProps): JSX.Element {
  const [activeTab, setActiveTab] = useState('graph');
  const [compareWith, setCompareWith] = useState<string | null>(null);
  const [compareDetail, setCompareDetail] = useState<SpecDetail | null>(null);

  // Reset the diff target when the detail itself changes.
  useEffect(() => {
    setCompareWith(null);
    setCompareDetail(null);
  }, [detail?.id]);

  useEffect(() => {
    if (!compareWith) return;
    let cancelled = false;
    api.getSpec(compareWith)
      .then((d) => { if (!cancelled) setCompareDetail(d); })
      .catch(() => { if (!cancelled) setCompareDetail(null); });
    return () => { cancelled = true; };
  }, [compareWith]);

  if (detailError) {
    return <EmptyState title="Failed to load spec" message={detailError} />;
  }
  if (!detail) {
    return (
      <div class="flex items-center justify-center py-16">
        <Spinner label="Pick a spec on the left" />
      </div>
    );
  }

  const graph = specToGraph(detail.definition);

  const schemasPanel = (() => {
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
              <h4 class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400 mb-1">
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
  })();

  const versionsPanel = (() => {
    if (siblings.length <= 1) {
      return (
        <p class="text-sm text-slate-500 py-2">
          Only one version registered for this slug.
        </p>
      );
    }
    return (
      <div class="space-y-3">
        <div class="text-xs text-slate-500">
          Compare definitions between any two versions of this slug.
        </div>
        <div class="flex items-center gap-2 text-sm">
          <label for="spec-diff-target">Diff against:</label>
          <select
            id="spec-diff-target"
            aria-label="Pick a version to diff against"
            value={compareWith ?? ''}
            onChange={(e) => setCompareWith((e.target as HTMLSelectElement).value || null)}
            class="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-md px-2 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
          >
            <option value="">— pick a version —</option>
            {siblings.filter((s) => s.id !== detail.id).map((s) => (
              <option value={s.id} key={s.id}>v{s.version} ({shortHash(s.hash)})</option>
            ))}
          </select>
        </div>
        {compareDetail ? (
          <Diff
            before={canonicalJson(compareDetail.definition)}
            after={canonicalJson(detail.definition)}
            label={`v${compareDetail.version} → v${detail.version}`}
          />
        ) : compareWith ? (
          <div class="flex items-center gap-2 text-xs text-slate-500">
            <Spinner size="sm" /> Loading...
          </div>
        ) : null}
      </div>
    );
  })();

  const definitionPanel = (
    <JsonViewer
      value={detail.definition}
      rootLabel="definition"
      mode="expanded"
      defaultExpandDepth={3}
      downloadFilename={`spec-${detail.slug}-v${detail.version}.json`}
    />
  );

  const tabs: TabSpec[] = [
    { id: 'graph', label: 'Graph', badge: <Pill variant="neutral" size="sm">{graph.nodes.length}</Pill> },
    { id: 'schemas', label: 'Schemas' },
    { id: 'definition', label: 'Definition' },
    { id: 'versions', label: 'Versions', badge: <Pill variant="neutral" size="sm">{siblings.length}</Pill> },
  ];

  return (
    <div class="flex flex-col h-full min-h-0">
      <header class="px-3 pt-3 pb-2 border-b border-slate-200 dark:border-slate-700">
        <div class="flex items-baseline gap-2">
          <h2 class="text-base font-semibold">{detail.slug}</h2>
          <Pill variant="info">v{detail.version}</Pill>
          <code class="text-xs font-mono text-slate-500" title={detail.hash}>
            {shortHash(detail.hash)}
          </code>
          <span class="ml-auto text-xs text-slate-500">
            Registered {formatTimestamp(detail.registered_at)}
          </span>
        </div>
      </header>
      <div class="flex-1 min-h-0">
        <Tabs
          tabs={tabs}
          activeTab={activeTab}
          onChange={setActiveTab}
          panels={{
            graph: (
              <div class="h-[60vh] p-3">
                <FlowGraph
                  nodes={graph.nodes}
                  edges={graph.edges}
                  autoLayout={true}
                  direction="LR"
                />
              </div>
            ),
            schemas: <div class="p-3">{schemasPanel}</div>,
            definition: <div class="p-3">{definitionPanel}</div>,
            versions: <div class="p-3">{versionsPanel}</div>,
          }}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function SpecsRoute(): JSX.Element {
  const [specs, setSpecs] = useState<SpecSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SpecDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.listSpecs()
      .then((rows) => {
        if (!cancelled) {
          setSpecs(rows);
          // Auto-select the latest version of the first slug for a
          // populated initial paint instead of an empty right pane.
          if (rows.length > 0 && selectedId == null) {
            const first = groupBySlug(rows)[0];
            if (first) setSelectedId(first.rows[0].id);
          }
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selectedId) { setDetail(null); setDetailError(null); return; }
    let cancelled = false;
    setDetail(null); setDetailError(null);
    api.getSpec(selectedId)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch((err: unknown) => {
        if (!cancelled) setDetailError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [selectedId]);

  const groups = useMemo(() => (specs ? groupBySlug(specs) : []), [specs]);

  // Siblings = all versions of the currently-selected slug.
  const siblings = useMemo(() => {
    if (!detail || !specs) return [];
    return specs.filter((s) => s.slug === detail.slug).sort((a, b) => b.version - a.version);
  }, [detail, specs]);

  return (
    <div class="p-4 md:p-6 space-y-4 flex flex-col h-full min-h-0">
      <header>
        <h1 class="text-2xl font-semibold">Specs</h1>
        <p class="text-sm text-slate-600 dark:text-slate-400">
          Registered FSM definitions. Pick a spec to view its graph, schemas, full definition, and version diffs.
        </p>
      </header>
      {error ? (
        <Card>
          <EmptyState title="Failed to load specs" message={error} />
        </Card>
      ) : specs == null ? (
        <Card>
          <div class="flex items-center justify-center py-12"><Spinner label="Loading specs" /></div>
        </Card>
      ) : (
        <div class="grid gap-4 lg:grid-cols-[24rem_1fr] flex-1 min-h-0">
          <Card className="p-0 overflow-auto">
            <MasterList groups={groups} selectedId={selectedId} onSelect={setSelectedId} />
          </Card>
          <Card className="p-0">
            <DetailPane detail={detail} detailError={detailError} siblings={siblings} />
          </Card>
        </div>
      )}
    </div>
  );
}

export default SpecsRoute;
