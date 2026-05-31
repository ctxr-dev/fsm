/**
 * Cross-environment clipboard write with toast feedback.
 *
 * The async `navigator.clipboard.writeText` is the modern path. It is
 * rejected in two real-world cases we have to handle:
 *
 *   1. **Document not focused** — Chrome / Firefox throw a SecurityError
 *      when the call happens during a context where the page lost focus
 *      (e.g. an iframe just stole it, or a test runner's headless mode).
 *   2. **Permission denied / API unavailable** — Older browsers or a
 *      `Permissions-Policy: clipboard-write=()` header. `navigator.clipboard`
 *      may be undefined entirely.
 *
 * Both fall back to the legacy `document.execCommand('copy')` trick:
 * create a hidden textarea, select its content, run execCommand. This
 * works in every browser jsdom emulates, including the test runner.
 *
 * Caller contract:
 *
 *   const ok = await copyText('payload');
 *   // ok === true  → clipboard now holds the text
 *   // ok === false → both paths failed; caller should show user feedback
 *
 * We deliberately do NOT swallow + log silently — the boolean return
 * lets the caller decide whether to toast "copied" or "copy failed".
 */

export async function copyText(text: string): Promise<boolean> {
  // Path A: modern async clipboard.
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through to legacy path; do NOT bail out — the modern API
    // throws under perfectly normal conditions (lost focus etc.) and
    // the legacy path is the documented fallback.
  }

  // Path B: legacy execCommand. Requires a DOM, so guard for SSR / Node.
  if (typeof document === 'undefined') return false;
  const ta = document.createElement('textarea');
  ta.value = text;
  // Style it off-screen but NOT display:none — execCommand requires
  // the textarea to be part of the layout tree.
  ta.setAttribute('readonly', 'true');
  ta.style.position = 'fixed';
  ta.style.top = '-1000px';
  ta.style.left = '-1000px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  try {
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand('copy');
    return ok;
  } catch {
    return false;
  } finally {
    document.body.removeChild(ta);
  }
}

/**
 * Convenience: copy + return a (text, ok) tuple. Used by tests that
 * want to assert both the input and the outcome in one expression.
 */
export async function copyTextWithResult(
  text: string,
): Promise<{ text: string; ok: boolean }> {
  const ok = await copyText(text);
  return { text, ok };
}
