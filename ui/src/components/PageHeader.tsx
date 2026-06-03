/**
 * ``<PageHeader>`` — every route's top heading + project-aware eyebrow.
 *
 * W23c contract: the project name (last segment of ``project_root``)
 * appears on EVERY page. Pre-W23c every route hand-rolled its own
 * ``<h1>`` and project context was only visible in the InfoTopBar.
 * This component is the single place that owns the "Project: <name>"
 * eyebrow + h1 + optional subtitle + right-aligned action slot.
 *
 * Renders:
 *
 *   Project: dummy-fsm-test                          [right slot]
 *   Runs
 *   Operator's primary "what's happening right now" surface.
 *
 * The eyebrow renders inline with the right slot (so refresh /
 * status badges live on the same baseline as the project breadcrumb).
 * The h1 is below; the optional subtitle ``<p>`` is below the h1.
 *
 * Props:
 *
 * - ``title`` — h1 text (required).
 * - ``subtitle`` — optional ``<p>`` beneath the h1.
 * - ``projectAware`` — default ``true``; render the "Project: …"
 *   eyebrow. Set ``false`` for the route that doesn't need it (none
 *   today; documented for completeness).
 * - ``rightSlot`` — VNode placed at the right edge of the eyebrow
 *   row (or right of the h1 row when the eyebrow is suppressed).
 *   Typical: a Refresh button, a status pill, a tab control.
 * - ``id`` — optional ``id`` on the h1 so a parent ``aria-labelledby``
 *   can target it.
 */

import type { JSX, VNode } from 'preact';

import { projectMetadata, projectMetadataLoading } from '../lib/store';
import { projectNameFromRoot } from '../lib/projectName';

export interface PageHeaderProps {
  title: string;
  subtitle?: string | VNode;
  projectAware?: boolean;
  rightSlot?: VNode;
  id?: string;
  className?: string;
}

export function PageHeader({
  title,
  subtitle,
  projectAware = true,
  rightSlot,
  id,
  className = '',
}: PageHeaderProps): JSX.Element {
  const metadata = projectMetadata.value;
  const loading = projectMetadataLoading.value;
  // Prefer the path-derived name; fall back to slug; finally null.
  const projectName = metadata
    ? (projectNameFromRoot(metadata.project_root) ?? metadata.project_slug)
    : null;

  // The eyebrow renders in three modes:
  //   1. loading — italic "loading…" placeholder.
  //   2. metadata present + name resolved — bold project name.
  //   3. metadata present but name is null (older API / in-memory
  //      backend with null project_root + null project_slug, or a
  //      drive-root-only path on Windows) — explicit "no project bound"
  //      affordance so the operator sees that the dashboard is running
  //      without a bound project rather than the header silently
  //      dropping the eyebrow.
  const metadataPresentButNoName =
    projectAware && metadata !== null && projectName === null;
  const showEyebrow =
    projectAware && (projectName !== null || loading || metadataPresentButNoName);

  return (
    <header class={`space-y-1.5 ${className}`}>
      {showEyebrow ? (
        <div class="flex items-center justify-between gap-3 text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
          <span>
            Project:{' '}
            <span class="font-semibold normal-case tracking-normal text-slate-700 dark:text-slate-200">
              {projectName !== null ? (
                projectName
              ) : loading ? (
                <span class="italic text-slate-400 dark:text-slate-500">loading…</span>
              ) : (
                <span class="italic text-slate-400 dark:text-slate-500">no project bound</span>
              )}
            </span>
          </span>
          {rightSlot ? <div class="flex items-center gap-2">{rightSlot}</div> : null}
        </div>
      ) : rightSlot ? (
        <div class="flex items-center justify-end">{rightSlot}</div>
      ) : null}
      <h1
        id={id}
        class="text-2xl font-semibold text-slate-900 dark:text-slate-100"
      >
        {title}
      </h1>
      {subtitle ? (
        typeof subtitle === 'string' ? (
          <p class="text-sm text-slate-600 dark:text-slate-400">{subtitle}</p>
        ) : (
          subtitle
        )
      ) : null}
    </header>
  );
}

export default PageHeader;
