/**
 * Per-route store for the Run Detail page.
 *
 * `runDetailFilters` is the cross-pane coordination bus. Every label
 * click in the State Tree / Event Timeline / Tool Calls / Signature
 * Ledger writes into this signal; every pane reads it to filter its
 * rows. The FilterChipBar renders one chip per non-empty key.
 *
 * Why a separate file from the global `store.ts`: this state is
 * scoped to ONE route. Lifting it into the global store would risk
 * other routes accidentally subscribing.
 */

import { signal, type Signal } from '@preact/signals';

import type { FilterChip } from '../components/FilterChips';

export interface RunDetailFilters {
  /** Filter all panes to events / tool-calls / signatures touching this state. */
  stateId?: string;
  /** Filter the event timeline to a specific kind (e.g. run_started). */
  eventKind?: string;
  /** Filter to a single producer. */
  producerId?: string;
  /** Filter tool-calls to a single tool_name. */
  toolName?: string;
  /** Filter drift signals to a kind. */
  signalKind?: string;
}

export const runDetailFilters: Signal<RunDetailFilters> = signal<RunDetailFilters>({});

/**
 * Toggle a filter key. If the value already equals what's about to be
 * written, the key is cleared instead (click-same-twice = toggle off).
 * Used for every label click handler: a user clicking the SAME state
 * id twice clears it rather than re-confirming.
 */
export function toggleFilter<K extends keyof RunDetailFilters>(
  key: K,
  value: RunDetailFilters[K],
): void {
  const prev = runDetailFilters.value;
  if (prev[key] === value) {
    const { [key]: _drop, ...rest } = prev;
    runDetailFilters.value = rest as RunDetailFilters;
  } else {
    runDetailFilters.value = { ...prev, [key]: value };
  }
}

/** Set a filter key unconditionally (used by the cmd palette / deep links). */
export function setFilter<K extends keyof RunDetailFilters>(
  key: K,
  value: RunDetailFilters[K] | undefined,
): void {
  const prev = runDetailFilters.value;
  if (value === undefined) {
    const { [key]: _drop, ...rest } = prev;
    runDetailFilters.value = rest as RunDetailFilters;
  } else {
    runDetailFilters.value = { ...prev, [key]: value };
  }
}

/** Remove a single filter key. */
export function clearFilter(key: keyof RunDetailFilters): void {
  setFilter(key, undefined);
}

/** Reset the entire filter set. */
export function clearAllFilters(): void {
  runDetailFilters.value = {};
}

/**
 * Convert the active filter set into a FilterChip[] for rendering.
 *
 * Each chip's `id` is `<key>:<value>` so the chip remove handler can
 * unambiguously identify which key to clear.
 */
export function filtersToChips(filters: RunDetailFilters): FilterChip[] {
  const chips: FilterChip[] = [];
  if (filters.stateId) {
    chips.push({
      id: `state:${filters.stateId}`,
      kind: 'state',
      label: `state: ${filters.stateId}`,
    });
  }
  if (filters.eventKind) {
    chips.push({
      id: `kind:${filters.eventKind}`,
      kind: 'event',
      label: `kind: ${filters.eventKind}`,
    });
  }
  if (filters.producerId) {
    chips.push({
      id: `producer:${filters.producerId}`,
      kind: 'producer',
      label: `producer: ${filters.producerId.slice(0, 12)}…`,
    });
  }
  if (filters.toolName) {
    chips.push({
      id: `tool:${filters.toolName}`,
      kind: 'tool',
      label: `tool: ${filters.toolName}`,
    });
  }
  if (filters.signalKind) {
    chips.push({
      id: `signal:${filters.signalKind}`,
      kind: 'signal',
      label: `signal: ${filters.signalKind}`,
    });
  }
  return chips;
}

/**
 * Predicate: does an event survive the active filter set?
 *
 * Filters are AND-composed. A filter for which the event has no
 * corresponding field is treated as a non-match (so a kind=X filter
 * excludes events whose kind is undefined / different).
 */
export function eventPassesFilters(
  event: { kind?: string; producer_id?: string; payload?: unknown },
  filters: RunDetailFilters,
): boolean {
  if (filters.eventKind && event.kind !== filters.eventKind) return false;
  if (filters.producerId && event.producer_id !== filters.producerId) return false;
  if (filters.stateId) {
    // The event's payload may reference a state via `state_id`,
    // `from_state`, or `to_state`. Match any of them.
    const p = (event.payload ?? {}) as Record<string, unknown>;
    const refs = [p.state_id, p.from_state, p.to_state];
    if (!refs.includes(filters.stateId)) return false;
  }
  return true;
}

/**
 * Predicate: does a tool-call survive the active filter set?
 *
 * A `toolName` filter narrows to that exact tool_name. A `stateId`
 * filter narrows by the call's producer_id (close-enough heuristic
 * without a state-correlation column on the call row).
 */
export function toolCallPassesFilters(
  call: { tool_name?: string; producer_id?: string },
  filters: RunDetailFilters,
): boolean {
  if (filters.toolName && call.tool_name !== filters.toolName) return false;
  if (filters.producerId && call.producer_id !== filters.producerId) return false;
  return true;
}

/**
 * Predicate: does a commit signature survive the active filter set?
 *
 * State filter narrows by `state_id`.
 */
export function signaturePassesFilters(
  sig: { state_id?: string },
  filters: RunDetailFilters,
): boolean {
  if (filters.stateId && sig.state_id !== filters.stateId) return false;
  return true;
}
