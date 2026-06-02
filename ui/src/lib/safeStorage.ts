/**
 * Three-tier persistent-storage wrapper: localStorage -> sessionStorage
 * -> in-memory Map. Every accessor is defensive: quota-exceeded,
 * disabled-storage (private browsing on some browsers), missing-window
 * (SSR / tests) all degrade gracefully without throwing.
 *
 * Used by ``useProjectPref`` (per-project preferences in W23d) and
 * any other consumer that wants persistence-with-fallback. Centralised
 * here so the catch logic exists in one place.
 */

const memory = new Map<string, string>();

/**
 * Resolve a ``Storage`` reference defensively.
 *
 * In some browser privacy modes the property access on
 * ``window.localStorage`` / ``window.sessionStorage`` itself throws
 * (SecurityError) before any method is called, so wrapping just the
 * subsequent ``.getItem`` / ``.setItem`` / ``.removeItem`` call is
 * not enough. Capture the ref inside a try/catch here and hand a
 * possibly-null Storage to the read/write helpers, which short-circuit
 * on null.
 */
function getLocal(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function getSession(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

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
    // QuotaExceededError / SecurityError: caller falls through
    return false;
  }
}

function tryRemove(s: Storage | null, key: string): void {
  if (!s) return;
  try {
    s.removeItem(key);
  } catch {
    // best-effort cleanup; ignore
  }
}

export const safeStorage = {
  getItem(key: string): string | null {
    if (typeof window === 'undefined') return memory.get(key) ?? null;
    const local = tryRead(getLocal(), key);
    if (local != null) return local;
    const session = tryRead(getSession(), key);
    if (session != null) return session;
    return memory.get(key) ?? null;
  },
  setItem(key: string, value: string): void {
    if (typeof window === 'undefined') {
      memory.set(key, value);
      return;
    }
    if (tryWrite(getLocal(), key, value)) return;
    if (tryWrite(getSession(), key, value)) return;
    memory.set(key, value);
  },
  removeItem(key: string): void {
    if (typeof window !== 'undefined') {
      tryRemove(getLocal(), key);
      tryRemove(getSession(), key);
    }
    memory.delete(key);
  },
};
