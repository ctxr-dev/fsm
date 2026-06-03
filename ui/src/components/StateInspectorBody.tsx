/**
 * Shared inspector body for an FSM state node.
 *
 * Rendered inside the right-half Sheet that opens when the operator
 * clicks a state node in either the spec graph or the run graph. The
 * body lays out flat scalar metadata (id, kind, purpose, loop config,
 * worker.role) in a KeyValueTable, then drills into structured sections
 * (worker.prompt_template, worker.response_schema, state.loop,
 * state.outputs, state.post_validations, inline.post_validations,
 * state.verifier, state.transitions, raw state JSON).
 *
 * Headings are the LITERAL FSM library field paths so the operator
 * sees the substrate's field names rather than designer text.
 */

import type { JSX } from 'preact';

import { CodeBlock } from './CodeBlock';
import { JsonViewer } from './JsonViewer';
import { KeyValueTable, type KvRow } from './KeyValueTable';
import { Pill, type PillVariant } from './Pill';
import { transitionKind } from '../lib/specGraph';
import { tokenisePredicate, classForPredicateKind } from '../lib/predicateTokens';

export interface StateInspectorBodyProps {
  state: Record<string, unknown>;
  isEntry: boolean;
}

function transitionKindVariant(kind: string | undefined): PillVariant {
  switch (kind) {
    case 'judgement': return 'success';
    case 'deterministic': return 'info';
    case 'otherwise': return 'warning';
    default: return 'neutral';
  }
}

export function StateInspectorBody({ state, isEntry }: StateInspectorBodyProps): JSX.Element {
  const id = typeof state.id === 'string' ? state.id : '(unknown)';
  const kind = typeof state.kind === 'string' ? state.kind : 'state';
  const worker = state.worker as Record<string, unknown> | undefined;
  const inline = state.inline as Record<string, unknown> | undefined;
  const loop = state.loop as Record<string, unknown> | undefined;
  const verifier = state.verifier as unknown;
  const outputs = Array.isArray(state.outputs) ? (state.outputs as unknown[]) : null;
  const statePostVals = Array.isArray(state.post_validations)
    ? (state.post_validations as unknown[])
    : null;
  const inlinePostVals = inline && Array.isArray(inline.post_validations)
    ? (inline.post_validations as unknown[])
    : null;
  const inlineInputs = inline && Array.isArray(inline.inputs)
    ? (inline.inputs as unknown[])
    : null;
  const workerInputs = worker && Array.isArray(worker.inputs)
    ? (worker.inputs as unknown[])
    : null;
  const purpose = typeof state.purpose === 'string'
    ? state.purpose
    : typeof inline?.purpose === 'string'
    ? (inline.purpose as string)
    : undefined;
  const transitions = Array.isArray(state.transitions)
    ? (state.transitions as Array<Record<string, unknown>>)
    : [];

  // ``rows`` collects flat scalar metadata for the KeyValueTable at
  // the top of the inspector. Anything structured (schemas, code,
  // transitions, raw JSON) renders in its own section below so the
  // operator can scan the metadata at a glance and drill into details
  // by section.
  const rows: KvRow[] = [
    { key: 'id', value: id },
    { key: 'kind', value: kind },
  ];
  if (isEntry) rows.push({ key: 'entry', value: true, hint: 'spec entry state' });
  if (purpose) rows.push({ key: 'purpose', value: purpose });
  if (worker?.role) rows.push({ key: 'worker.role', value: String(worker.role) });
  if (inline?.handler_id) rows.push({ key: 'inline.handler_id', value: String(inline.handler_id) });
  const allowedTools = Array.isArray(worker?.allowed_tools) ? worker?.allowed_tools : null;
  if (allowedTools) rows.push({ key: 'worker.allowed_tools', value: allowedTools });
  if (workerInputs) rows.push({ key: 'worker.inputs', value: workerInputs });
  if (inlineInputs) rows.push({ key: 'inline.inputs', value: inlineInputs });
  // Loop scalar config surfaces in the flat metadata table so the
  // operator sees max_iterations / done_field / aggregator at a glance
  // without expanding the structured loop JSON below.
  if (loop) {
    if (typeof loop.max_iterations === 'number') {
      rows.push({ key: 'loop.max_iterations', value: loop.max_iterations });
    }
    if (typeof loop.done_field === 'string') {
      rows.push({ key: 'loop.done_field', value: loop.done_field });
    }
    if (typeof loop.aggregator === 'string') {
      rows.push({ key: 'loop.aggregator', value: loop.aggregator });
    }
  }

  return (
    <div class="space-y-4 p-3">
      <KeyValueTable rows={rows} caption="State metadata" />
      {/* Every section heading below is the LITERAL FSM library field
          name (worker.prompt_template / worker.response_schema /
          state.loop / state.transitions). No consumer-specific text:
          the FSM UI is agnostic to which skill / agent system is
          using the library, and labels its sections after the field
          paths the Worker / State models actually expose. */}
      {worker?.prompt_template ? (
        <section>
          <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
            worker.prompt_template
            {worker.prompt_template_language ? (
              <span class="ml-2 text-slate-400 dark:text-slate-500">
                · language={String(worker.prompt_template_language)}
              </span>
            ) : null}
          </h4>
          {/* The consumer's `prompt_template_language` field (defined
              on the FSM library's Worker model since W21) tells the
              UI how to render. When set, CodeBlock honours it
              verbatim; when omitted, the markdown content heuristic
              acts as a courtesy fallback. */}
          <CodeBlock
            text={String(worker.prompt_template)}
            language={
              typeof worker.prompt_template_language === 'string'
                ? worker.prompt_template_language
                : undefined
            }
            maxInlineHeight="max-h-64"
            ariaLabel={`${id} worker prompt template`}
          />
        </section>
      ) : null}
      {worker?.response_schema ? (
        <section>
          <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
            worker.response_schema
          </h4>
          {/* W22 Fix 2: default to the top two levels of the schema
              expanded — the operator wants a glance at the shape
              without scrolling through every nested property. Click
              the chevrons or the toolbar's Expand button to descend. */}
          <JsonViewer
            value={worker.response_schema}
            rootLabel="response_schema"
            mode="expanded"
            defaultExpandDepth={2}
            maxInlineHeight="max-h-64"
          />
        </section>
      ) : null}
      {loop ? (
        <section>
          <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
            state.loop
          </h4>
          <JsonViewer value={loop} rootLabel="loop" mode="inline" maxInlineHeight="max-h-48" />
        </section>
      ) : null}
      {outputs && outputs.length > 0 ? (
        <section>
          <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
            state.outputs ({outputs.length})
          </h4>
          <JsonViewer
            value={outputs}
            rootLabel="outputs"
            mode="expanded"
            defaultExpandDepth={2}
            maxInlineHeight="max-h-48"
          />
        </section>
      ) : null}
      {statePostVals && statePostVals.length > 0 ? (
        <section>
          <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
            state.post_validations ({statePostVals.length})
          </h4>
          <JsonViewer
            value={statePostVals}
            rootLabel="post_validations"
            mode="expanded"
            defaultExpandDepth={2}
            maxInlineHeight="max-h-48"
          />
        </section>
      ) : null}
      {inlinePostVals && inlinePostVals.length > 0 ? (
        <section>
          <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
            inline.post_validations ({inlinePostVals.length})
          </h4>
          <JsonViewer
            value={inlinePostVals}
            rootLabel="inline.post_validations"
            mode="expanded"
            defaultExpandDepth={2}
            maxInlineHeight="max-h-48"
          />
        </section>
      ) : null}
      {verifier ? (
        <section>
          <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
            state.verifier
          </h4>
          <JsonViewer
            value={verifier}
            rootLabel="verifier"
            mode="expanded"
            defaultExpandDepth={2}
            maxInlineHeight="max-h-48"
          />
        </section>
      ) : null}
      {transitions.length > 0 ? (
        <section>
          <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
            state.transitions ({transitions.length})
          </h4>
          <ul class="divide-y divide-slate-100 dark:divide-slate-800 text-xs">
            {transitions.map((t, idx) => {
              const target = typeof t.to === 'string' ? t.to : '(unknown)';
              // Derive the kind from the `when` payload via the shared
              // helper — the FSM library's Transition model has no
              // top-level `kind` field, so reading t.kind would always
              // be undefined for spec-authored data. Mirrors how the
              // graph edge data.kind is computed.
              const tKind = transitionKind(t);
              // Mirror specGraph.predicateLabel(): a `when` dict can
              // encode the human-readable guard text under any of
              // `.predicate` (bare Predicate dump), `.expression`
              // (deterministic kind), or `.criteria` (judgement
              // kind). Checking only some of them leaves the
              // inspector blank for judgement transitions while the
              // edge label / tooltip still renders text — confusing.
              const when = typeof t.when === 'string'
                ? t.when
                : t.when && typeof t.when === 'object'
                ? (((t.when as Record<string, unknown>).predicate as string | undefined)
                    ?? ((t.when as Record<string, unknown>).expression as string | undefined)
                    ?? ((t.when as Record<string, unknown>).criteria as string | undefined)
                    ?? '')
                : '';
              // W22 Fix 3 site 2: highlight predicates (deterministic /
              // judgement) so the operator's eye jumps to the
              // conditions that actually MATTER for understanding why
              // the FSM branched. always / otherwise stay slate.
              const isPredicate = tKind === 'deterministic' || tKind === 'judgement';
              // `min-w-0` is mandatory on a `truncate` flex item: without
              // it the item's intrinsic min-content width wins and a long
              // predicate overflows the row instead of truncating with
              // ellipsis. `flex-1` only gives the item room to grow, not
              // permission to shrink below its content width.
              const whenClass = isPredicate
                ? 'font-mono font-semibold truncate flex-1 min-w-0 px-1.5 py-0.5 rounded bg-amber-900/80 dark:bg-amber-900/70 border border-amber-500 dark:border-amber-600 text-amber-50'
                : 'font-mono text-slate-500 dark:text-slate-400 truncate flex-1 min-w-0';
              return (
                <li key={`${target}-${idx}`} class="py-1.5 flex items-center gap-2">
                  {tKind ? <Pill variant={transitionKindVariant(tKind)} size="sm">{tKind}</Pill> : null}
                  <code class="font-mono text-slate-500 dark:text-slate-400 text-[10px]">{id}</code>
                  <span class="text-slate-400">{'->'}</span>
                  <code class="font-mono text-slate-700 dark:text-slate-300">{target}</code>
                  {when ? (
                    <code class={whenClass} title={when}>
                      {isPredicate
                        ? tokenisePredicate(when).map((t, ti) => (
                            <span key={ti} class={classForPredicateKind(t.kind)}>{t.text}</span>
                          ))
                        : when}
                    </code>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
      <section>
        <h4 class="text-[11px] font-mono text-slate-500 dark:text-slate-400 mb-1">
          state (raw)
        </h4>
        {/* Raw state JSON renders collapsed (defaultExpandDepth: 0)
            so the section header acts as a disclosure for operators
            who want the unfiltered definition without the curated
            sections above getting in the way. */}
        <JsonViewer
          value={state}
          rootLabel={id}
          mode="expanded"
          defaultExpandDepth={0}
          maxInlineHeight="max-h-64"
        />
      </section>
    </div>
  );
}
