/**
 * Derive the human-readable project name from a project root path.
 *
 * The user said: "project name should be the final path section
 * (e.g. ``dummy-fsm-test``). Project name should also be in the
 * header of all pages."
 *
 * Splitting rules:
 * - Normalise backslashes to forward slashes (Windows paths land here
 *   from time to time when an operator hits the dashboard from a
 *   Windows shell).
 * - Strip trailing slashes (``/Users/x/proj/`` → ``/Users/x/proj``).
 * - Return ``null`` for an empty / root-only path so the consumer can
 *   render an explicit "no project bound" affordance instead of
 *   silently falling through to whitespace.
 *
 * Centralised here so every consumer (InfoTopBar, Settings,
 * PageHeader, localStorage namespace key in W23d, graphViewport key
 * tail in W23b) reaches the same answer for the same input.
 */

export function projectNameFromRoot(root: string | null | undefined): string | null {
  if (!root) return null;
  // Normalise + strip trailing separators so ``/Users/x/proj`` and
  // ``/Users/x/proj/`` resolve identically.
  const normalised = root.replace(/\\/g, '/').replace(/\/+$/u, '');
  if (normalised === '' || normalised === '/') return null;
  // Windows drive-root inputs (``C:\``, ``D:\\``) collapse to ``C:`` /
  // ``D:`` after the slash-strip above. Treat those as root-only and
  // return null, matching the documented contract — the operator is
  // not bound to a real project, the dashboard should render the "no
  // project bound" affordance rather than label the eyebrow with the
  // drive letter.
  if (/^[A-Za-z]:$/u.test(normalised)) return null;
  const segments = normalised.split('/').filter(Boolean);
  return segments.length === 0 ? null : segments[segments.length - 1];
}
