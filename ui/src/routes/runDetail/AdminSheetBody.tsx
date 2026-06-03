/**
 * Body component for the run-detail Admin sheet.
 *
 * Rendered inside :class:`SheetHost` when the operator opens the
 * "Admin" button in the run-detail header. The body is a vertical
 * stack of six :class:`CollapsibleSection` panels — Journal, Lock,
 * Drift, Commit signatures, Allowed tools, Tool calls — each a
 * self-contained component that owns its own initial fetch + state.
 *
 * Why each section owns its own fetch (rather than this body driving
 * a single ``Promise.allSettled``):
 *   - one slow endpoint should not block the others from rendering;
 *   - the planned PR 6 SSE-debounced refetch is per-section (the same
 *     channel never refreshes every panel simultaneously);
 *   - bodies are easier to unit-test in isolation when state is local.
 *
 * The existing right-column Admin Card on /runs/:id is NOT removed in
 * this PR — operators have both surfaces simultaneously so nothing
 * breaks during rollout. PR 4 (or later) deletes the inline card once
 * the sheet has soaked.
 */

import type { JSX } from 'preact';

import type { RunManifest } from '../../lib/api';
import { JournalSection } from './admin/JournalSection';
import { LockSection } from './admin/LockSection';
import { DriftSection } from './admin/DriftSection';
import { SignaturesSection } from './admin/SignaturesSection';
import { AllowedToolsSection } from './admin/AllowedToolsSection';
import { ToolCallsSection } from './admin/ToolCallsSection';

export interface AdminSheetBodyProps {
  runId: string;
  manifest: RunManifest;
}

export function AdminSheetBody({
  runId,
  manifest,
}: AdminSheetBodyProps): JSX.Element {
  return (
    <div
      class="flex flex-col gap-3"
      data-testid="admin-sheet-body"
      data-run-id={runId}
    >
      <JournalSection runId={runId} />
      <LockSection runId={runId} />
      <DriftSection runId={runId} />
      <SignaturesSection runId={runId} />
      <AllowedToolsSection manifest={manifest} />
      <ToolCallsSection runId={runId} />
    </div>
  );
}

export default AdminSheetBody;
