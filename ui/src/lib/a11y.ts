/**
 * Shared a11y hooks: focus trap + escape-to-close + scroll lock.
 *
 * Extracted from `components/Dialog.tsx` (W18a). The W18 `Sheet`
 * primitive consumes the SAME hooks so the two surfaces never drift in
 * their focus / escape / scroll-lock contract. A single source of
 * truth here is what keeps every modal-class component obeying the
 * one universal rule we ship to users: Escape closes, Tab cycles
 * inside, focus restores to the trigger on close.
 */

import { useCallback, useEffect, useRef } from 'preact/hooks';
import type { MutableRef } from 'preact/hooks';

/**
 * CSS selector covering every element a keyboard user can reach with
 * Tab. Same shape as Dialog's original constant; deliberately wide so
 * a third-party widget that uses a non-standard tabindex still
 * participates in the cycle.
 */
export const FOCUSABLE_SELECTOR =
  'a[href], area[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Focus-trap hook: while `active` is true, Tab / Shift+Tab cycle inside
 * the `panelRef`, focus moves to the first focusable child on activation,
 * and the previously-focused element is restored on deactivation.
 *
 * Behaviour:
 *
 *   - When `active` flips true: remember `document.activeElement`,
 *     `queueMicrotask(() => focus the first focusable child)`. The
 *     microtask gives Preact a chance to commit the panel's children
 *     before we query the DOM for them — a synchronous focus call on the
 *     same tick would race the render.
 *   - While active: a `keydown` listener on `document` intercepts Tab /
 *     Shift+Tab and forces the focus to wrap inside the panel.
 *   - When `active` flips false (or the panel unmounts): focus is
 *     restored to the remembered element. We guard with `?.focus?.()`
 *     because the trigger may have been removed (route change, list
 *     filter, etc.) in which case we silently no-op.
 *
 * Edge cases handled:
 *
 *   - Panel with no focusable children: focus falls back to the panel
 *     itself (which gets `tabIndex={-1}` from the consumer).
 *   - Focus currently outside the panel during a Tab press: wraps to the
 *     last focusable on Shift+Tab, otherwise lets the browser handle.
 *   - Stale ref between renders: each invocation re-queries
 *     `panelRef.current`, so a child reorder is fine.
 *   - Disabled focusables: filtered out (the selector excludes the
 *     `:disabled` pseudo for inputs/buttons, but a manually-disabled
 *     element with the attribute still needs the explicit filter).
 */
export function useFocusTrap<T extends HTMLElement>(
  panelRef: MutableRef<T | null>,
  active: boolean,
): void {
  const lastActiveRef = useRef<HTMLElement | null>(null);

  const focusFirst = useCallback(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const focusables = panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
    const target = focusables[0] ?? panel;
    target.focus();
  }, [panelRef]);

  useEffect(() => {
    if (!active) return undefined;
    lastActiveRef.current = (document.activeElement as HTMLElement) ?? null;
    queueMicrotask(focusFirst);
    return () => {
      lastActiveRef.current?.focus?.();
    };
  }, [active, focusFirst]);

  useEffect(() => {
    if (!active) return undefined;
    const handler = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusables = Array.from(
        panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter((el) => !el.hasAttribute('disabled'));
      if (focusables.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const activeEl = document.activeElement as HTMLElement | null;
      if (event.shiftKey) {
        if (activeEl === first || !panel.contains(activeEl)) {
          event.preventDefault();
          last.focus();
        }
      } else {
        if (activeEl === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [active, panelRef]);
}

/**
 * Escape-to-close hook: while `active` is true, a `keydown` listener on
 * `document` calls `onClose()` when Escape is pressed.
 *
 * `event.preventDefault()` is intentional: in browsers Escape can also
 * cancel form submissions / clear native search inputs, but a modal
 * surface that swallows Escape needs to claim the keystroke first so
 * the cancellation chain doesn't double-fire.
 */
export function useEscapeToClose(active: boolean, onClose: () => void): void {
  useEffect(() => {
    if (!active) return undefined;
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [active, onClose]);
}

/**
 * Body-scroll-lock hook: while `active` is true, sets
 * `document.body.style.overflow = 'hidden'` and restores the prior value
 * on deactivation.
 *
 * Why a hook rather than a Tailwind class: the body element is owned by
 * `index.html`, not by any Preact component. We need an imperative
 * mutation that survives across renders. The save-and-restore pattern
 * handles the case where the page already has overflow restrictions
 * (e.g. a parent shell that sets `overflow: hidden` for its own
 * layout): we restore the EXACT prior value rather than blindly
 * resetting to `''`.
 *
 * Edge case: two modals open simultaneously would race; the LAST one to
 * close restores `''` which may be wrong. We accept the cost — the only
 * places this hook runs are Dialog and Sheet, and the project's UX
 * never opens both at once (Sheet stacks via the SheetHost, see W18a's
 * sheetStack signal).
 */
export function useBodyScrollLock(active: boolean): void {
  useEffect(() => {
    if (!active) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [active]);
}
