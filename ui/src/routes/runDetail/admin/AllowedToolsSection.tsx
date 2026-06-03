/**
 * Admin-sheet section: allowed_tools surfaced by the current state.
 *
 * Derives the list from ``manifest.current_state`` by fetching the
 * spec (lazily, once) and probing its state map for an
 * ``allowed_tools`` / ``tools`` / ``allowedTools`` array under the
 * current state. Falling back across key variants keeps the section
 * tolerant of W12-substrate naming differences (the same probe pattern
 * runDetail.tsx uses against StateNode payloads).
 *
 * When the manifest has no current_state (run hasn't entered any state
 * yet, or has terminated), the section renders a "not surfaced" hint
 * rather than fetching a spec it won't index.
 */

import type { JSX } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';

import {
  EmptyState,
  Pill,
  Spinner,
} from '../../../components';
import {
  api,
  ApiError,
  type RunManifest,
  type SpecDetail,
} from '../../../lib/api';
import { CollapsibleSection } from './CollapsibleSection';

const KEYS = ['allowed_tools', 'allowedTools', 'tools', 'tool_allowlist'];

function extractAllowedToolsForState(
  spec: SpecDetail | null,
  stateId: string | null,
): string[] | null {
  if (!spec || !stateId) return null;
  const def = spec.definition as Record<string, unknown> | undefined;
  if (!def) return null;
  const states = def.states as Record<string, unknown> | undefined;
  if (!states) return null;
  const node = states[stateId] as Record<string, unknown> | undefined;
  if (!node) return null;
  for (const key of KEYS) {
    const value = node[key];
    if (Array.isArray(value) && value.every((v) => typeof v === 'string')) {
      return value as string[];
    }
  }
  return null;
}

export interface AllowedToolsSectionProps {
  manifest: RunManifest;
}

export function AllowedToolsSection({
  manifest,
}: AllowedToolsSectionProps): JSX.Element {
  const [spec, setSpec] = useState<SpecDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;
    setSpec(null);
    setError(null);
    setLoading(true);
    api
      .getSpec(manifest.fsm_spec_id)
      .then((s) => {
        if (cancelled) return;
        setSpec(s);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : String(err));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [manifest.fsm_spec_id]);

  const allowed = useMemo(
    () => extractAllowedToolsForState(spec, manifest.current_state),
    [spec, manifest.current_state],
  );

  const trailing = allowed ? (
    <Pill variant="neutral" size="sm">
      {allowed.length}
    </Pill>
  ) : undefined;

  return (
    <CollapsibleSection
      id="admin-allowed-tools"
      title={
        manifest.current_state
          ? `Allowed tools (${manifest.current_state})`
          : 'Allowed tools'
      }
      trailing={trailing}
    >
      {error ? (
        <EmptyState title="Failed to load spec" message={error} />
      ) : loading ? (
        <Spinner label="Loading allowed tools" />
      ) : !manifest.current_state ? (
        <p class="text-sm text-slate-500 dark:text-slate-400">
          Run has no current state — allow-list not applicable.
        </p>
      ) : allowed === null ? (
        <p class="text-sm text-slate-500 dark:text-slate-400">
          Not surfaced by the current state's spec entry.
        </p>
      ) : allowed.length === 0 ? (
        <p class="text-sm text-slate-500 dark:text-slate-400">
          Allow-list is empty — no tools are permitted.
        </p>
      ) : (
        <ul class="flex flex-wrap gap-1.5">
          {allowed.map((tool) => (
            <li key={tool}>
              <Pill variant="neutral" size="sm">
                {tool}
              </Pill>
            </li>
          ))}
        </ul>
      )}
    </CollapsibleSection>
  );
}

export default AllowedToolsSection;
