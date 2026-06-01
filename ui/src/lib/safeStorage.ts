/**
 * Three-tier persistent-storage wrapper: localStorage → sessionStorage
 * → in-memory Map. Every accessor is defensive — quota-exceeded,
 * disabled-storage (private browsing on some browsers), missing-window
 * (SSR / tests) all degrade gracefully without throwing.
 *
 * Used by ``useProjectPref`` (per-project preferences in W23d) and
 * any other consumer that wants persistence-with-fallback. Centralised
 * here so the catch logic exists in one place.
 */

const memory = new Map<string, string>();

function tryRead(s: Storage | null, key: string): string | null {
  if (!s) return null;
  try {
    return s.getItem(key);
  } catch {
    // SecurityError under some private-browsing modes
    return null;
  }
}

function tryWrite(s: Storage | null, key: string, value: string): boolean {
  if (!s) return false;
  try {
    s.setItem(key, value);
    return true;
  } catch {
    // QuotaExceededError / SecurityError — caller falls through
    return false;
  }
}

export const safeStorage = {
  getItem(key: string): string | null {
    if (typeof window === 'undefined') return memory.get(key) ?? null;
    const local = tryRead(window.localStorage, key);
    if (local != null) return local;
    const session = tryRead(window.sessionStorage, key);
    if (session != null) return session;
    return memory.get(key) ?? null;
  },
  setItem(key: string, value: string): void {
    if (typeof window === 'undefined') {
      memory.set(key, value);
      return;
    }
    if (tryWrite(window.localStorage, key, value)) return;
    if (tryWrite(window.sessionStorage, key, value)) return;
    memory.set(key, value);
  },
  removeItem(key: string): void {
    if (typeof window !== 'undefined') {
      try {
        window.localStorage.removeItem(key);
      } catch {
        // ignore
      }
      try {
        window.sessionStorage.removeItem(key);
      } catch {
        // ignore
      }
    }
    memory.delete(key);
  },
};
