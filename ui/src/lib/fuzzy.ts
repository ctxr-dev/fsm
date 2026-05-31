/**
 * Tiny fzf-style fuzzy scorer for the command palette.
 *
 * ~80 LOC, zero deps. Scoring rule (descending priority):
 *
 *   1. All query chars must appear in order (case-insensitive). If not,
 *      the candidate is excluded.
 *   2. Score = match bonus - gap penalty.
 *      - Match bonus:  +10 per matched char.
 *      - Prefix bonus: +20 if the first matched char is at index 0 of
 *                       the candidate (the query starts the candidate).
 *      - Word-boundary bonus: +5 for each match whose preceding char is
 *                              ` `, `_`, `-`, `/`, `.` (i.e. starts a word).
 *      - Camelhump bonus: +3 for each match where the matched char is
 *                          uppercase in the original candidate.
 *      - Gap penalty:  -1 per character between consecutive matches.
 *   3. Ties: shorter candidates rank higher (-0.01 × candidate.length
 *      as a tiebreaker).
 *
 * Returns a `{score, matches}` for each kept candidate. `matches` is
 * the array of indices into the original candidate string where the
 * query chars landed; callers use it to draw <mark> highlights.
 */

export interface FuzzyHit<T> {
  item: T;
  score: number;
  /** Indices into `text(item)` of the matched query characters. */
  matches: number[];
}

export interface FuzzyOptions<T> {
  /** Extract the searchable text from an item. */
  text: (item: T) => string;
  /** Optional weight to multiply the score by (priority bucket). */
  weight?: (item: T) => number;
}

const GAP_PENALTY = 1;
const MATCH_BONUS = 10;
const PREFIX_BONUS = 20;
const WORD_BOUNDARY_BONUS = 5;
const CAMELHUMP_BONUS = 3;
const LENGTH_TIEBREAKER = 0.01;

const WORD_BOUNDARY_CHARS = new Set([' ', '_', '-', '/', '.', ':']);

/**
 * Score a single candidate against the query. Returns null if the
 * query characters cannot all be found in order.
 */
export function scoreOne(candidate: string, query: string): { score: number; matches: number[] } | null {
  if (query.length === 0) return { score: 0, matches: [] };
  const c = candidate.toLowerCase();
  const q = query.toLowerCase();
  const matches: number[] = [];
  let ci = 0;
  let qi = 0;
  let score = 0;
  let lastMatch = -1;
  while (qi < q.length && ci < c.length) {
    if (c[ci] === q[qi]) {
      matches.push(ci);
      score += MATCH_BONUS;
      if (ci === 0 && qi === 0) score += PREFIX_BONUS;
      if (ci > 0) {
        const prev = candidate[ci - 1];
        if (WORD_BOUNDARY_CHARS.has(prev)) score += WORD_BOUNDARY_BONUS;
      }
      if (candidate[ci] >= 'A' && candidate[ci] <= 'Z') score += CAMELHUMP_BONUS;
      if (lastMatch >= 0) score -= GAP_PENALTY * (ci - lastMatch - 1);
      lastMatch = ci;
      qi += 1;
    }
    ci += 1;
  }
  if (qi < q.length) return null;
  score -= LENGTH_TIEBREAKER * candidate.length;
  return { score, matches };
}

/**
 * Score and rank a list of items against the query. Items that don't
 * contain the query (in order) are excluded. Returns hits sorted
 * descending by score.
 */
export function rankFuzzy<T>(
  items: readonly T[],
  query: string,
  opts: FuzzyOptions<T>,
): FuzzyHit<T>[] {
  if (query.trim().length === 0) {
    return items.map((item) => ({ item, score: 0, matches: [] }));
  }
  const hits: FuzzyHit<T>[] = [];
  for (const item of items) {
    const text = opts.text(item);
    const r = scoreOne(text, query);
    if (r == null) continue;
    const weight = opts.weight?.(item) ?? 1;
    hits.push({ item, score: r.score * weight, matches: r.matches });
  }
  hits.sort((a, b) => b.score - a.score);
  return hits;
}
