/**
 * /settings — project metadata, infra placeholders, theme toggle.
 *
 * Metadata (DB path, alembic revision, sqlite ``user_version``) is read
 * from ``POST /api/v1/admin/db/doctor``. Port assignments + drift
 * threshold are deliberate W11 / W12 placeholders. The theme toggle is
 * fully wired — persisted to ``localStorage`` and applied by toggling
 * the ``dark`` class on ``<html>``, honouring ``prefers-color-scheme``
 * when the user picks "System".
 */

import type { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import { Button, Card, Dialog, Pill, Spinner, useToast } from '../components';
import { api, ApiError, type DoctorReport } from '../lib/api';
import { lastPortChangeRequest, projectMetadata } from '../lib/store';

type Theme = 'light' | 'dark' | 'system';

const THEME_STORAGE_KEY = 'fsm-ui:theme';
const THEME_OPTIONS: { value: Theme; label: string }[] = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
  { value: 'system', label: 'System' },
];

function readStoredTheme(): Theme {
  if (typeof localStorage === 'undefined') return 'system';
  const raw = localStorage.getItem(THEME_STORAGE_KEY);
  return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : 'system';
}

function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  const prefersDark =
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches;
  const dark = theme === 'dark' || (theme === 'system' && prefersDark);
  document.documentElement.classList.toggle('dark', dark);
}

const pragma = (r: DoctorReport, key: string): string => {
  const v = r.pragmas[key];
  return v === undefined || v === null ? '—' : String(v);
};

export function SettingsRoute(): JSX.Element {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme());

  useEffect(() => {
    let cancelled = false;
    api.doctor()
      .then((d) => { if (!cancelled) setReport(d); })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    applyTheme(theme);
    if (typeof localStorage !== 'undefined') localStorage.setItem(THEME_STORAGE_KEY, theme);
    if (theme !== 'system' || typeof window === 'undefined' ||
        typeof window.matchMedia !== 'function') return undefined;
    const mql = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => applyTheme('system');
    mql.addEventListener('change', handler);
    return () => mql.removeEventListener('change', handler);
  }, [theme]);

  return (
    <div class="p-6 space-y-6 max-w-3xl">
      <header>
        <h1 class="text-2xl font-semibold">Settings</h1>
        <p class="text-sm text-slate-600 dark:text-slate-400">
          Project metadata, infrastructure placeholders, and personal preferences.
        </p>
      </header>

      <Card title="Project metadata">
        {error ? (
          <p class="text-sm text-red-700 dark:text-red-300">{error}</p>
        ) : !report ? (
          <div class="flex items-center justify-center py-6"><Spinner label="Loading project metadata" /></div>
        ) : (
          <dl class="grid grid-cols-1 sm:grid-cols-[10rem_1fr] gap-x-4 gap-y-2 text-sm">
            {/* W22 Fix 5: display DB path RELATIVE to the project root
                so the value stays portable across machines and can be
                committed to shared configs. The absolute db_path is
                ALSO surfaced as a small slate-tinted line below for
                operators who genuinely need it (debugging a non-
                canonical --db location). Falls back to the absolute
                form alone when the server is older than W22 (the new
                field is optional on the wire). */}
            <dt class="text-slate-500 dark:text-slate-400">DB path</dt>
            <dd>
              <code class="font-mono text-xs break-all text-emerald-700 dark:text-emerald-300">
                {report.db_path_relative ?? report.db_path}
              </code>
              {report.db_path_relative ? (
                <div class="text-[10px] text-slate-400 dark:text-slate-500 mt-0.5 break-all">
                  absolute: <code class="font-mono">{report.db_path}</code>
                  {report.project_root ? (
                    <>
                      {' · root: '}
                      <code class="font-mono">{report.project_root}</code>
                    </>
                  ) : null}
                </div>
              ) : null}
            </dd>
            <dt class="text-slate-500 dark:text-slate-400">SQLite user_version</dt>
            <dd><code class="font-mono text-xs">{pragma(report, 'user_version')}</code></dd>
            <dt class="text-slate-500 dark:text-slate-400">Alembic revision</dt>
            <dd><code class="font-mono text-xs">{report.alembic_revision ?? '—'}</code></dd>
            <dt class="text-slate-500 dark:text-slate-400">Lock count</dt>
            <dd>{report.lock_count}</dd>
          </dl>
        )}
      </Card>

      <PortAssignmentsCard />

      <Card title="Drift detector">
        <div class="flex items-center justify-between gap-3">
          <p class="text-sm text-slate-600 dark:text-slate-400">
            Threshold tuning for the drift-signal aggregator lands with W12.
          </p>
          <Pill variant="warning">W12 placeholder</Pill>
        </div>
      </Card>

      <Card title="Appearance">
        <fieldset>
          <legend class="sr-only">Theme</legend>
          <div role="radiogroup" aria-label="Theme" class="inline-flex gap-2">
            {THEME_OPTIONS.map((opt) => (
              <Button
                key={opt.value}
                variant={theme === opt.value ? 'primary' : 'secondary'}
                size="sm"
                onClick={() => setTheme(opt.value)}
                aria-label={`Use ${opt.label.toLowerCase()} theme`}
              >
                {opt.label}
              </Button>
            ))}
          </div>
        </fieldset>
        <p class="mt-2 text-xs text-slate-500 dark:text-slate-400">
          "System" follows your operating-system colour scheme.
        </p>
      </Card>
    </div>
  );
}

export default SettingsRoute;


// ---------------------------------------------------------------------------
// W23e: PortAssignmentsCard — read current ports from project metadata,
// edit + apply via POST /admin/ports; the global ReconnectingOverlay
// (mounted in app.tsx) takes over once a request is in flight.
// ---------------------------------------------------------------------------

type Subsystem = 'mcp' | 'api' | 'ui';

function PortAssignmentsCard(): JSX.Element {
  const metadata = projectMetadata.value;
  const subsystems = metadata?.subsystems ?? {};
  return (
    <Card title="Port assignments">
      <div class="space-y-3">
        <p class="text-sm text-slate-600 dark:text-slate-400">
          Change a subsystem's port. The supervisor drains and respawns the
          named process; the UI shows a reconnecting overlay and redirects
          automatically when the new port is healthy.
        </p>
        {(['mcp', 'api', 'ui'] as const).map((sub) => (
          <PortRow key={sub} subsystem={sub} info={subsystems[sub] ?? null} />
        ))}
      </div>
    </Card>
  );
}

function portFromUrl(url: string | undefined | null): number | null {
  if (!url) return null;
  try {
    return Number(new URL(url).port) || null;
  } catch {
    return null;
  }
}

interface PortRowProps {
  subsystem: Subsystem;
  info: { base_url: string; healthz_url: string | null; pid: number | null } | null;
}

function PortRow({ subsystem, info }: PortRowProps): JSX.Element {
  const currentPort = portFromUrl(info?.base_url);
  const [draft, setDraft] = useState<number | ''>(currentPort ?? '');
  const [confirming, setConfirming] = useState<boolean>(false);
  const [applying, setApplying] = useState<boolean>(false);
  const toast = useToast();

  // When metadata refreshes (e.g. after a successful port change), pull
  // the new port into the draft so the input reflects reality.
  useEffect(() => {
    if (currentPort != null) setDraft(currentPort);
  }, [currentPort]);

  const valid = typeof draft === 'number' && draft >= 1024 && draft <= 65535;
  const disabled = !valid || draft === currentPort || applying;

  const onApply = async () => {
    if (!valid || draft === currentPort) return;
    setApplying(true);
    try {
      const accepted = await api.changePort({ subsystem, new_port: draft });
      lastPortChangeRequest.value = {
        requestId: accepted.request_id,
        subsystem,
        newPort: draft,
        newUrlWhenReady: accepted.new_url_when_ready,
        startedAt: new Date().toISOString(),
        estimatedRestartMs: accepted.estimated_restart_ms,
      };
      // Overlay takes over; we don't need to do anything else.
    } catch (err) {
      if (err instanceof ApiError) {
        const msg = typeof err.detail === 'string' ? err.detail : err.message;
        toast.danger(`Port change rejected: ${msg}`);
      } else {
        toast.danger(`Port change failed: ${err instanceof Error ? err.message : String(err)}`);
      }
    } finally {
      setApplying(false);
      setConfirming(false);
    }
  };

  return (
    <div class="flex items-center gap-3 text-sm">
      <span class="w-20 font-mono text-slate-700 dark:text-slate-300">{subsystem}</span>
      <span class="text-xs text-slate-500 dark:text-slate-400">
        current: <code class="font-mono">{currentPort ?? '—'}</code>
      </span>
      <input
        type="number"
        min={1024}
        max={65535}
        value={draft}
        onInput={(e) => {
          const v = (e.currentTarget as HTMLInputElement).value;
          setDraft(v === '' ? '' : Number(v));
        }}
        aria-label={`New port for ${subsystem}`}
        class={[
          'w-24 rounded border px-2 py-1 text-sm font-mono',
          'border-slate-300 dark:border-slate-600',
          'bg-white dark:bg-slate-800 text-slate-700 dark:text-slate-200',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
        ].join(' ')}
      />
      <Button
        variant={subsystem === 'ui' ? 'danger' : 'primary'}
        size="sm"
        disabled={disabled}
        onClick={() => setConfirming(true)}
      >
        {applying ? 'Applying…' : 'Apply'}
      </Button>
      <Dialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Restart ${subsystem} on port ${draft}?`}
        widthClassName="max-w-md"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirming(false)} disabled={applying}>
              Cancel
            </Button>
            <Button
              variant={subsystem === 'ui' ? 'danger' : 'primary'}
              onClick={onApply}
              loading={applying}
            >
              {subsystem === 'ui' ? 'Confirm and redirect' : 'Confirm'}
            </Button>
          </>
        }
      >
        <p class="text-sm">
          The {subsystem} subsystem will drain (SIGTERM, 5s budget) and respawn
          on port {draft}. Other subsystems are unaffected.
        </p>
        {subsystem === 'ui' ? (
          <p class="mt-2 text-sm font-medium">
            Heads-up: this tab will redirect to the new UI URL once the new
            server is ready. Unsaved work in browser tabs pointing at the
            current URL will be lost.
          </p>
        ) : (
          <p class="mt-2 text-sm">
            Active SSE connections will drop and reconnect automatically.
          </p>
        )}
      </Dialog>
    </div>
  );
}
