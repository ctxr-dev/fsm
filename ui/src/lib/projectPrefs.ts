/**
 * ``useProjectPref`` — per-project preference persistence.
 *
 * W23d mandate: "filterable by spec names (multiselect dropdown with
 * search, saved user choice in local storage per project name)."
 *
 * Storage key shape: ``fsm-ui:proj:<slot>:<prefName>`` where
 * ``<slot>`` is ``encodeURIComponent(projectName)`` when ``projectName``
 * is a non-empty string, and the literal ``'default'`` otherwise (see
 * ``projectPrefKey`` below for the canonical implementation).
 *
 * - Per-project isolation means switching projects (today: pointing
 *   the dashboard at a different `.ctxr-fsm/` via cwd / future
 *   multi-project switcher) gets a clean preference slate; the old
 *   project's prefs sit untouched in localStorage ready for when the
 *   operator switches back.
 * - ``'default'`` fallback ensures preferences work before
 *   ``wireProjectMetadata`` resolves (initial paint) and for in-memory
 *   backends that have no path-derived project name.
 * - The hook re-hydrates whenever ``projectName`` changes (covers the
 *   future multi-project switcher with zero call-site churn).
 */

import { useCallback, useEffect, useMemo, useState } from 'preact/hooks';

import { projectMetadata } from './store';
import { projectNameFromRoot } from './projectName';
import { safeStorage } from './safeStorage';

const KEY_NAMESPACE = 'fsm-ui:proj';

export function projectPrefKey(projectName: string | null, prefName: string): string {
  const slot = projectName == null || projectName === ''
    ? 'default'
    : encodeURIComponent(projectName);
  return `${KEY_NAMESPACE}:${slot}:${prefName}`;
}

function readFromStorage<T>(key: string, initial: T): T {
  const raw = safeStorage.getItem(key);
  if (raw == null) return initial;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return initial;
  }
}

/**
 * Read the current project name (path-derived; falls back to slug).
 * Returns null when metadata hasn't loaded yet OR when neither field
 * is populated.
 */
export function useProjectName(): string | null {
  const metadata = projectMetadata.value;
  return useMemo(() => {
    if (!metadata) return null;
    return projectNameFromRoot(metadata.project_root) ?? metadata.project_slug;
  }, [metadata]);
}

export function useProjectPref<T>(
  prefName: string,
  initial: T,
): [T, (next: T) => void] {
  const projectName = useProjectName();
  const key = projectPrefKey(projectName, prefName);

  const [value, setValue] = useState<T>(() => readFromStorage(key, initial));

  // Re-hydrate when the project changes (future multi-project switcher).
  // Initial is intentionally NOT in deps; we only want a re-read on
  // key change, not on every render where ``initial`` is a fresh object.
  useEffect(() => {
    setValue(readFromStorage(key, initial));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const update = useCallback(
    (next: T) => {
      setValue(next);
      safeStorage.setItem(key, JSON.stringify(next));
    },
    [key],
  );

  return [value, update];
}
