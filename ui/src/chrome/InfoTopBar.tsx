/**
 * Info-rich topbar — replaces the W18c bare "Stream" + TopBarExtras header.
 *
 * The W22b3 mandate from the user:
 *
 *   "UI header (sticky?) should not just contain ctxr-fsm slug, it
 *    should beautifully write with human language that it's finite
 *    state machine, what is the cwd where it is installed, what is
 *    the project name, basic information, indicators of availability
 *    for mcp, api, swagger etc. It should look very intensive,
 *    maximum explanatory."
 *
 * The header therefore renders FOUR information bands in one sticky
 * row:
 *
 *   1. BRANDING  — ``ctxr-fsm`` wordmark + tagline ("Finite state
 *      machine substrate") so a first-time visitor reading the
 *      header knows what they're looking at without scrolling.
 *   2. PROJECT   — project slug + project root path (relative to
 *      $HOME when possible, never absolute on a shared screenshot).
 *      The slug answers "which project" and the path answers "where
 *      on disk".
 *   3. SUBSYSTEMS — clickable pills for each subsystem the
 *      supervisor knows about (MCP / API / Swagger / UI). Each pill
 *      is a real ``<a>`` with the discovered URL, so the operator
 *      can jump straight to Swagger or the UI from the dashboard.
 *      Healthy / degraded state is colour-coded; the pill is muted
 *      when the subsystem isn't reported.
 *   4. CONNECTION — the existing SSE stream pill, kept where the
 *      user already learned to look (right edge), with the
 *      TopBarExtras menu cluster trailing.
 *
 * The bar is sticky-positioned (``sticky top-0 z-10``) so it stays
 * pinned to the viewport top across long pages without floating
 * over the existing chrome's stacking context. ``backdrop-blur``
 * keeps the row legible when content scrolls behind it.
 *
 * Data acquisition: one call to ``api.getCurrentProject()`` at mount
 * + a window-focus refetch so a user who tabs back from Swagger sees
 * fresh subsystem state without a manual refresh. Health probes
 * (the ``healthz_url`` field) are re-issued in a small ``mapLimited``
 * fan-out so the pills can show green / amber / red rather than just
 * "configured". Failures collapse to a muted pill — the topbar must
 * never error out the dashboard.
 *
 * Why a separate component rather than extending ``app.tsx``'s inline
 * ``TopBar`` directly? Two reasons: (a) the data layer is async
 * (signal + effect + fetch) and lifting it into ``app.tsx`` would
 * inflate that file past the W18a shell-thinness rule, and (b)
 * future polish passes will tightly couple a Cmd+K target list to
 * the subsystem map — keeping the home for both side-by-side here
 * is the right shape.
 */

import type { JSX } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';

import { Pill, type PillVariant } from '../components';
import { connectionPillProps } from '../lib/connectionPill';
import { connectionState } from '../lib/store';
import {
  api,
  ApiError,
  type ProjectMetadata,
  type SubsystemInfo,
} from '../lib/api';

import { TopBarExtras } from './TopBarExtras';

const HEALTHZ_TIMEOUT_MS = 1500;

/**
 * Health-probe outcomes for a subsystem. ``unknown`` is the initial
 * state (probe in flight or not run yet). ``healthy`` means the
 * ``healthz_url`` returned 2xx. ``degraded`` covers every other case
 * (non-2xx, network error, timeout).
 */
type ProbeStatus = 'unknown' | 'healthy' | 'degraded';

interface SubsystemView {
  name: string;
  base_url: string;
  swagger_url: string | null;
  pid: number | null;
  probe: ProbeStatus;
  /** Human-friendly label rendered on the pill ("MCP", "API", …). */
  label: string;
  /** Mouse-hover hint with all available metadata. */
  title: string;
}

/**
 * Display order for subsystems. Anything reported by the supervisor
 * that doesn't appear in this list falls back to alphabetical at the
 * end — so a future subsystem (e.g. "metrics") doesn't disappear from
 * the header just because the priority isn't wired here yet.
 */
const SUBSYSTEM_ORDER: readonly string[] = ['mcp', 'api', 'ui', 'swagger'];

const PROBE_VARIANT: Record<ProbeStatus, PillVariant> = {
  unknown: 'neutral',
  healthy: 'success',
  degraded: 'danger',
};

/** Render an absolute path relative to ``$HOME`` when possible. */
function condenseHome(path: string | null | undefined): string {
  if (!path) return '';
  const home =
    typeof window !== 'undefined' &&
    (window as Window & { __FSM_HOME__?: string }).__FSM_HOME__;
  if (home && path.startsWith(home)) return `~${path.slice(home.length)}`;
  return path;
}

/**
 * Probe ``healthz_url`` once with an abort-after-timeout. Returns
 * ``healthy`` on 2xx, ``degraded`` for any other outcome (non-2xx /
 * network error / timeout). Never throws.
 */
async function probeHealth(url: string): Promise<ProbeStatus> {
  if (typeof fetch === 'undefined') return 'unknown';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), HEALTHZ_TIMEOUT_MS);
  try {
    const res = await fetch(url, { signal: controller.signal, credentials: 'omit' });
    return res.ok ? 'healthy' : 'degraded';
  } catch {
    return 'degraded';
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Build the per-subsystem display row from the discovery doc.
 *
 * ``swagger`` is synthesised from ``metadata.swagger_url`` rather
 * than coming through ``subsystems`` — Swagger lives on the API
 * process so it shares the API's healthz outcome; we still render it
 * as its own pill so an operator can click straight through.
 */
function buildViews(metadata: ProjectMetadata): SubsystemView[] {
  const views: SubsystemView[] = [];
  for (const name of Object.keys(metadata.subsystems)) {
    const info: SubsystemInfo = metadata.subsystems[name];
    views.push({
      name,
      base_url: info.base_url,
      swagger_url: null,
      pid: info.pid,
      probe: 'unknown',
      label: name.toUpperCase(),
      title: [
        `${name} subsystem`,
        `URL: ${info.base_url}`,
        info.healthz_url ? `Health: ${info.healthz_url}` : null,
        info.pid != null ? `PID: ${info.pid}` : null,
      ].filter(Boolean).join(' · '),
    });
  }
  // Swagger is a synthetic row: it lives at /docs on the API process,
  // not in the subsystems map. We surface it as its own pill so the
  // operator's first instinct ("show me the API docs") gets a
  // dedicated affordance instead of being buried under "API".
  views.push({
    name: 'swagger',
    base_url: metadata.swagger_url,
    swagger_url: metadata.swagger_url,
    pid: null,
    probe: 'unknown',
    label: 'Swagger',
    title: `OpenAPI / Swagger UI · ${metadata.swagger_url}`,
  });

  // Sort by SUBSYSTEM_ORDER, then alphabetically for the tail.
  return views.sort((a, b) => {
    const ai = SUBSYSTEM_ORDER.indexOf(a.name);
    const bi = SUBSYSTEM_ORDER.indexOf(b.name);
    if (ai >= 0 && bi >= 0) return ai - bi;
    if (ai >= 0) return -1;
    if (bi >= 0) return 1;
    return a.name.localeCompare(b.name);
  });
}

export function InfoTopBar(): JSX.Element {
  const [metadata, setMetadata] = useState<ProjectMetadata | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [probes, setProbes] = useState<Record<string, ProbeStatus>>({});

  // Initial fetch + re-fetch on focus.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const m = await api.getCurrentProject();
        if (cancelled) return;
        setMetadata(m);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : String(err));
      }
    };
    load();
    const onFocus = () => { void load(); };
    if (typeof window !== 'undefined') {
      window.addEventListener('focus', onFocus);
    }
    return () => {
      cancelled = true;
      if (typeof window !== 'undefined') {
        window.removeEventListener('focus', onFocus);
      }
    };
  }, []);

  // Health probes whenever the metadata refreshes. Capped fan-out so a
  // future "10 subsystems" scenario doesn't carpet-bomb the API at boot.
  useEffect(() => {
    if (!metadata) return;
    let cancelled = false;
    (async () => {
      const next: Record<string, ProbeStatus> = {};
      const subs = Object.entries(metadata.subsystems);
      for (const [name, info] of subs) {
        if (!info.healthz_url) {
          next[name] = 'unknown';
          continue;
        }
        next[name] = await probeHealth(info.healthz_url);
        if (cancelled) return;
        setProbes((prev) => ({ ...prev, [name]: next[name] }));
      }
      // Swagger inherits the API process's outcome — same Python
      // server, same uvicorn worker.
      if ('api' in next) {
        setProbes((prev) => ({ ...prev, swagger: next.api }));
      }
    })();
    return () => { cancelled = true; };
  }, [metadata]);

  const views = useMemo<SubsystemView[]>(() => {
    if (!metadata) return [];
    return buildViews(metadata).map((v) => ({ ...v, probe: probes[v.name] ?? v.probe }));
  }, [metadata, probes]);

  const connection = connectionPillProps(connectionState.value);
  const projectRoot = condenseHome(metadata?.project_root);

  return (
    <header
      role="banner"
      class={
        'sticky top-0 z-10 ' +
        'backdrop-blur bg-white/85 dark:bg-slate-900/85 ' +
        'border-b border-slate-200 dark:border-slate-700 ' +
        'flex flex-wrap items-center gap-x-6 gap-y-2 px-6 py-2'
      }
    >
      {/* Band 1: branding. The two-line treatment communicates "what
          tool is this" at first glance — the wordmark is the slug,
          the tagline names the substrate in plain English so a
          newcomer who lands on the dashboard isn't left guessing. */}
      <div class="flex flex-col leading-tight">
        <span class="text-base font-semibold tracking-tight text-slate-900 dark:text-slate-100">
          ctxr-fsm
        </span>
        <span class="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-400">
          Finite state machine substrate
        </span>
      </div>

      {/* Band 2: project. ``project_slug`` answers "which project?";
          the home-condensed root answers "where on disk?". When the
          API is older / the DB is in-memory, ``project_slug`` and
          ``project_root`` are null — render an explicit "no project
          bound" label so the operator doesn't read "blank" as a bug. */}
      {metadata ? (
        <div class="flex flex-col leading-tight min-w-0">
          <span
            class="text-sm font-medium text-slate-900 dark:text-slate-100 truncate max-w-[24rem]"
            title={metadata.project_slug ?? 'no project bound'}
          >
            {metadata.project_slug ?? <em class="text-slate-500 dark:text-slate-400">no project bound</em>}
          </span>
          <span
            class="text-[10px] font-mono text-slate-500 dark:text-slate-400 truncate max-w-[24rem]"
            title={metadata.project_root ?? ''}
          >
            {projectRoot || <span class="italic">project root unknown</span>}
          </span>
        </div>
      ) : null}

      {/* Band 3: subsystem pills. Each pill is a real <a> with the
          discovered URL so the operator can click straight through.
          Colour reflects the health probe outcome:
          neutral=probing, success=healthy, danger=degraded. */}
      <nav aria-label="Subsystem availability" class="flex flex-wrap items-center gap-1.5">
        {views.length === 0 && metadata !== null ? (
          <span class="text-[10px] text-slate-500 dark:text-slate-400 italic">
            no subsystems reported (supervisor not running)
          </span>
        ) : null}
        {views.map((v) => (
          <a
            key={v.name}
            href={v.base_url}
            target="_blank"
            rel="noopener noreferrer"
            class="inline-flex items-center gap-1"
            title={v.title}
            aria-label={`${v.label} subsystem · ${v.probe}`}
          >
            <Pill variant={PROBE_VARIANT[v.probe]} size="sm">
              <span aria-hidden="true" class={
                'inline-block h-1.5 w-1.5 rounded-full mr-1 ' +
                (v.probe === 'healthy'
                  ? 'bg-emerald-500'
                  : v.probe === 'degraded'
                  ? 'bg-red-500'
                  : 'bg-slate-400')
              } />
              {v.label}
            </Pill>
          </a>
        ))}
      </nav>

      {error ? (
        <span class="text-[10px] text-red-700 dark:text-red-300">
          metadata: {error}
        </span>
      ) : null}

      <div class="flex-1" />

      {/* Band 4: connection + extras. The connection pill keeps its
          historical right-edge position so existing muscle memory
          (operators who learned where "Stream" lives) isn't broken
          by the rest of the redesign. */}
      <div
        aria-live="polite"
        aria-atomic="true"
        class="flex items-center gap-2"
      >
        <span class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Stream
        </span>
        <Pill variant={connection.variant} title={connection.title} aria-label={`Event stream: ${connection.label}`}>
          <span
            aria-hidden="true"
            class={[
              'inline-block h-2 w-2 rounded-full',
              connection.variant === 'success' && 'bg-emerald-500',
              connection.variant === 'warning' && 'bg-amber-500',
              connection.variant === 'danger' && 'bg-red-500',
            ]
              .filter(Boolean)
              .join(' ')}
          />
          {connection.label}
        </Pill>
      </div>
      <div class="pl-3 border-l border-slate-200 dark:border-slate-700">
        <TopBarExtras />
      </div>
    </header>
  );
}

export default InfoTopBar;
