/**
 * predicateTokens — lex an FSM-DSL predicate expression into kinded
 * tokens for in-browser syntax colouring.
 *
 * Mirrors the Python tokenizer in `ctxr/fsm/core/predicates.py` so the
 * UI surface matches the runtime grammar exactly: AND / OR / NOT (also
 * spelled `&&` / `||` / `!`), comparison operators (==, !=, <, >, <=,
 * >=), single/double-quoted string literals with backslash escapes,
 * integer + float numbers, dotted identifiers, the built-in functions
 * `len`/`empty`/`in`, and the literal keywords TRUE/FALSE/NULL/ALWAYS.
 *
 * Output is a flat list of `{ kind, text }` tokens that, when
 * concatenated, reproduce the input verbatim (whitespace preserved).
 * Callers render each token as `<span class={CLASS_FOR_KIND[kind]}>`.
 *
 * This module is intentionally pure / dependency-free so it stays
 * cheap to import inside edge labels and inspector panels.
 */

export type PredicateTokenKind =
  | 'keyword'
  | 'operator'
  | 'identifier'
  | 'string'
  | 'number'
  | 'function'
  | 'paren'
  | 'comma'
  | 'whitespace';

export interface PredicateToken {
  kind: PredicateTokenKind;
  text: string;
}

const OPERATOR_KEYWORDS = new Set(['AND', 'OR', 'NOT', 'IN']);
const LITERAL_KEYWORDS = new Set(['TRUE', 'FALSE', 'NULL', 'ALWAYS']);
const FUNCTION_NAMES = new Set(['len', 'empty']);
const TWO_CHAR_OPS = new Set(['==', '!=', '<=', '>=', '&&', '||']);

function isDigit(ch: string): boolean {
  return ch >= '0' && ch <= '9';
}
function isIdentStart(ch: string): boolean {
  return ch === '_' || (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z');
}
function isIdentPart(ch: string): boolean {
  return isIdentStart(ch) || isDigit(ch) || ch === '.';
}

/**
 * Lex `source` into a list of tokens. Never throws on malformed input;
 * unknown characters fall through as `identifier`-kinded single-char
 * tokens so highlighting degrades gracefully instead of breaking the
 * surrounding UI on weird operator pickings.
 */
export function tokenisePredicate(source: string): PredicateToken[] {
  const out: PredicateToken[] = [];
  const n = source.length;
  let i = 0;
  while (i < n) {
    const ch = source[i];
    // Whitespace run.
    if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') {
      let j = i;
      while (j < n && (source[j] === ' ' || source[j] === '\t' || source[j] === '\n' || source[j] === '\r')) j++;
      out.push({ kind: 'whitespace', text: source.slice(i, j) });
      i = j;
      continue;
    }
    // Parens / commas.
    if (ch === '(' || ch === ')') {
      out.push({ kind: 'paren', text: ch });
      i++;
      continue;
    }
    if (ch === ',') {
      out.push({ kind: 'comma', text: ch });
      i++;
      continue;
    }
    // Two-char operators (==, !=, <=, >=, &&, ||).
    const two = source.slice(i, i + 2);
    if (TWO_CHAR_OPS.has(two)) {
      out.push({ kind: 'operator', text: two });
      i += 2;
      continue;
    }
    // Single-char operators.
    if (ch === '<' || ch === '>' || ch === '!') {
      out.push({ kind: 'operator', text: ch });
      i++;
      continue;
    }
    // String literal (single or double quoted) with backslash escapes.
    if (ch === "'" || ch === '"') {
      const quote = ch;
      let j = i + 1;
      while (j < n && source[j] !== quote) {
        if (source[j] === '\\' && j + 1 < n) j += 2;
        else j++;
      }
      if (j < n) j++; // consume closing quote
      out.push({ kind: 'string', text: source.slice(i, j) });
      i = j;
      continue;
    }
    // Numeric literal (integer or float with one optional decimal point).
    if (isDigit(ch)) {
      let j = i;
      while (j < n && (isDigit(source[j]) || source[j] === '.')) j++;
      out.push({ kind: 'number', text: source.slice(i, j) });
      i = j;
      continue;
    }
    // Identifier or keyword (dotted paths allowed).
    if (isIdentStart(ch)) {
      let j = i;
      while (j < n && isIdentPart(source[j])) j++;
      const raw = source.slice(i, j);
      const upper = raw.toUpperCase();
      if (OPERATOR_KEYWORDS.has(upper)) {
        // AND / OR / NOT / in render as operators (logical glue).
        out.push({ kind: 'operator', text: raw });
      } else if (LITERAL_KEYWORDS.has(upper)) {
        out.push({ kind: 'keyword', text: raw });
      } else if (FUNCTION_NAMES.has(raw) && source[j] === '(') {
        // len( / empty( only — `in` is handled above as an operator.
        out.push({ kind: 'function', text: raw });
      } else {
        out.push({ kind: 'identifier', text: raw });
      }
      i = j;
      continue;
    }
    // Graceful fallback: unknown char becomes an identifier token so
    // we never lose source text. The visible colour will be the plain
    // identifier shade.
    out.push({ kind: 'identifier', text: ch });
    i++;
  }
  return out;
}

/**
 * Tailwind class palette for each token kind. Tuned for the amber
 * predicate pill background so the coloured tokens stay legible in
 * both light and dark themes.
 */
export const CLASS_FOR_PREDICATE_KIND: Record<PredicateTokenKind, string> = {
  keyword: 'text-amber-200 font-semibold italic',
  operator: 'text-amber-300 font-bold',
  identifier: 'text-amber-50',
  string: 'text-lime-200',
  number: 'text-cyan-200',
  function: 'text-fuchsia-200',
  paren: 'text-amber-400',
  comma: 'text-amber-400',
  whitespace: '',
};

export function classForPredicateKind(kind: PredicateTokenKind): string {
  return CLASS_FOR_PREDICATE_KIND[kind];
}
