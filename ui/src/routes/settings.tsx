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

import { Button, Card, Pill, Spinner } from '../components';
import { api, ApiError, type DoctorReport } from '../lib/api';

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

      <Card title="Port assignments">
        <div class="flex items-center justify-between gap-3">
          <p class="text-sm text-slate-600 dark:text-slate-400">
            API port and memory-injection wiring will be surfaced here once the W11 infrastructure ships.
          </p>
          <Pill variant="warning">W11 placeholder</Pill>
        </div>
      </Card>

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
