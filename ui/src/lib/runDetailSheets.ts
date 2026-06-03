/**
 * Openers for the three run-detail inspection sheets — state-entry,
 * edge, and admin. Each opener computes a stable id from its payload
 * and short-circuits if a sheet with that id is already on the stack.
 *
 * Why these live in a dedicated module rather than each call site
 * inlining ``openSheet({...})``:
 *
 *   - The id-shape contract (``state:<entryId>``, ``edge:<from>-<to>``,
 *     ``admin:<runId>``) is shared by the run graph nodes, the journal
 *     row context menu, and the run-detail header buttons. A single
 *     authoritative computation prevents two call sites from producing
 *     mismatched ids that look the same to a user but stack twice.
 *   - The dedup semantics (skip when an entry with the same id is
 *     already present) belong with the opener, not the global
 *     ``openSheet`` mutator. Other sheets — Brief, command palette
 *     detail panels — deliberately allow multiple stacked copies, so
 *     the store cannot bake dedup in unconditionally.
 *
 * The opener returns the resolved sheet id so callers can mirror it
 * to a URL fragment or focus the corresponding aside imperatively
 * without re-deriving the string.
 */

import { openSheet, sheetStack, type SheetEntry } from './store';

export interface StateEntrySheetPayload {
  entryId: string;
  runId: string;
  title?: string;
  /** Optional pre-built body. Defaults to ``null`` — the SheetHost
   * tolerates a null body (renders an empty panel) so infra tests can
   * exercise stacking without pulling in the inspector component. */
  content?: SheetEntry['content'];
}

export interface EdgeSheetPayload {
  runId: string;
  fromStateId: string;
  toStateId: string;
  title?: string;
  content?: SheetEntry['content'];
}

export interface AdminSheetPayload {
  runId: string;
  title?: string;
  content?: SheetEntry['content'];
}

/** Stable id for a state-entry sheet. */
export function stateEntrySheetId(entryId: string): string {
  return `state:${entryId}`;
}

/** Stable id for an edge sheet (direction-sensitive). */
export function edgeSheetId(fromStateId: string, toStateId: string): string {
  return `edge:${fromStateId}-${toStateId}`;
}

/** Stable id for the admin sheet of a given run. */
export function adminSheetId(runId: string): string {
  return `admin:${runId}`;
}

/**
 * Push ``entry`` onto the sheet stack ONLY if no entry with the same
 * id is already present. Returns the resolved id either way so the
 * caller can chain a "scroll into view" or URL-mirror without
 * branching on whether the open was a no-op.
 *
 * Idempotency rule: the FIRST opener wins. A second opener with the
 * same id is a no-op — we do not re-order the stack and we do not
 * replace the existing entry's title or body. This is the contract
 * the run graph relies on: rapidly hover-clicking the same node must
 * not flash the panel.
 */
function pushUnique(entry: SheetEntry): string {
  const exists = sheetStack.value.some((e) => e.id === entry.id);
  if (!exists) openSheet(entry);
  return entry.id;
}

export function openStateEntrySheet(payload: StateEntrySheetPayload): string {
  const id = stateEntrySheetId(payload.entryId);
  return pushUnique({
    id,
    title: payload.title ?? `State entry ${payload.entryId}`,
    width: 'right-third',
    content: payload.content ?? null,
    urlFragment: id,
  });
}

export function openEdgeSheet(payload: EdgeSheetPayload): string {
  const id = edgeSheetId(payload.fromStateId, payload.toStateId);
  return pushUnique({
    id,
    title:
      payload.title ?? `Edge ${payload.fromStateId} → ${payload.toStateId}`,
    width: 'right-third',
    content: payload.content ?? null,
    urlFragment: id,
  });
}

export function openAdminSheet(payload: AdminSheetPayload): string {
  const id = adminSheetId(payload.runId);
  return pushUnique({
    id,
    title: payload.title ?? `Run admin · ${payload.runId}`,
    width: 'right-half',
    content: payload.content ?? null,
    urlFragment: id,
  });
}
