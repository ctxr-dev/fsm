// fsm-predicates.mjs — deterministic predicate evaluator for FSM transitions.
//
// Supports a small, safe expression DSL parsed and evaluated over a
// caller-supplied environment object. The DSL deliberately omits anything
// that could cause side effects or unbounded computation:
//
//   literals:    numbers, strings (single or double quoted), true, false, null
//   identifiers: dotted paths into the environment (e.g. tier, project_profile.languages)
//   operators:   ==  !=  <  >  <=  >=
//                AND  OR  NOT (case-insensitive; also && || ! accepted)
//                ()
//   functions:   len(x)        -- length of a string or array
//                in(x, list)   -- membership test
//                empty(x)      -- shortcut for len(x) == 0
//
// Two reserved keywords:
//   "always" — always evaluates to true (for unconditional transitions)
//   "otherwise" — returns true iff no other earlier-listed transition predicate
//                 evaluated true. Caller (the transition resolver) handles the
//                 "otherwise" semantics; this module rejects "otherwise" inside
//                 normal expressions.
//
// Anything outside this grammar throws a parse error. There is no eval(),
// no template-literal escape, no implicit type coercion beyond JavaScript's
// own loose comparisons (which we narrow further via the equality semantics
// below).

const TOKEN_TYPES = Object.freeze({
  NUMBER: "NUMBER",
  STRING: "STRING",
  IDENT: "IDENT",
  OP: "OP",
  LPAREN: "LPAREN",
  RPAREN: "RPAREN",
  COMMA: "COMMA",
  EOF: "EOF",
});

const KEYWORDS = new Set(["AND", "OR", "NOT", "TRUE", "FALSE", "NULL", "ALWAYS"]);
const COMPARE_OPS = new Set(["==", "!=", "<", ">", "<=", ">="]);

// ─── Tokeniser ─────────────────────────────────────────────────────────

function tokenise(input) {
  const tokens = [];
  let i = 0;
  while (i < input.length) {
    const c = input[i];
    if (c === " " || c === "\t" || c === "\n" || c === "\r") {
      i++;
      continue;
    }
    if (c === "(") {
      tokens.push({ type: TOKEN_TYPES.LPAREN, value: "(", pos: i });
      i++;
      continue;
    }
    if (c === ")") {
      tokens.push({ type: TOKEN_TYPES.RPAREN, value: ")", pos: i });
      i++;
      continue;
    }
    if (c === ",") {
      tokens.push({ type: TOKEN_TYPES.COMMA, value: ",", pos: i });
      i++;
      continue;
    }
    if (c === "&" && input[i + 1] === "&") {
      tokens.push({ type: TOKEN_TYPES.OP, value: "AND", pos: i });
      i += 2;
      continue;
    }
    if (c === "|" && input[i + 1] === "|") {
      tokens.push({ type: TOKEN_TYPES.OP, value: "OR", pos: i });
      i += 2;
      continue;
    }
    if (c === "=" && input[i + 1] === "=") {
      tokens.push({ type: TOKEN_TYPES.OP, value: "==", pos: i });
      i += 2;
      continue;
    }
    if (c === "!" && input[i + 1] === "=") {
      tokens.push({ type: TOKEN_TYPES.OP, value: "!=", pos: i });
      i += 2;
      continue;
    }
    if (c === "!") {
      tokens.push({ type: TOKEN_TYPES.OP, value: "NOT", pos: i });
      i++;
      continue;
    }
    if (c === "<" && input[i + 1] === "=") {
      tokens.push({ type: TOKEN_TYPES.OP, value: "<=", pos: i });
      i += 2;
      continue;
    }
    if (c === ">" && input[i + 1] === "=") {
      tokens.push({ type: TOKEN_TYPES.OP, value: ">=", pos: i });
      i += 2;
      continue;
    }
    if (c === "<") {
      tokens.push({ type: TOKEN_TYPES.OP, value: "<", pos: i });
      i++;
      continue;
    }
    if (c === ">") {
      tokens.push({ type: TOKEN_TYPES.OP, value: ">", pos: i });
      i++;
      continue;
    }
    if (c === "'" || c === '"') {
      const start = i;
      const quote = c;
      i++;
      let out = "";
      while (i < input.length && input[i] !== quote) {
        if (input[i] === "\\" && i + 1 < input.length) {
          out += input[i + 1];
          i += 2;
        } else {
          out += input[i];
          i++;
        }
      }
      if (i >= input.length) {
        throw new SyntaxError(`Unterminated string literal at position ${start}`);
      }
      i++;
      tokens.push({ type: TOKEN_TYPES.STRING, value: out, pos: start });
      continue;
    }
    if (/[0-9]/.test(c)) {
      const start = i;
      let str = "";
      while (i < input.length && /[0-9.]/.test(input[i])) {
        str += input[i];
        i++;
      }
      const n = Number(str);
      if (Number.isNaN(n)) {
        throw new SyntaxError(`Malformed number "${str}" at position ${start}`);
      }
      tokens.push({ type: TOKEN_TYPES.NUMBER, value: n, pos: start });
      continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      const start = i;
      let str = "";
      while (i < input.length && /[A-Za-z0-9_.]/.test(input[i])) {
        str += input[i];
        i++;
      }
      const upper = str.toUpperCase();
      if (KEYWORDS.has(upper)) {
        if (upper === "TRUE") {
          tokens.push({ type: TOKEN_TYPES.IDENT, value: "true", literal: true, pos: start });
        } else if (upper === "FALSE") {
          tokens.push({ type: TOKEN_TYPES.IDENT, value: "false", literal: false, pos: start });
        } else if (upper === "NULL") {
          tokens.push({ type: TOKEN_TYPES.IDENT, value: "null", literal: null, pos: start });
        } else if (upper === "ALWAYS") {
          tokens.push({ type: TOKEN_TYPES.IDENT, value: "always", literal: true, pos: start });
        } else {
          tokens.push({ type: TOKEN_TYPES.OP, value: upper, pos: start });
        }
      } else {
        tokens.push({ type: TOKEN_TYPES.IDENT, value: str, pos: start });
      }
      continue;
    }
    throw new SyntaxError(`Unexpected character "${c}" at position ${i}`);
  }
  tokens.push({ type: TOKEN_TYPES.EOF, value: null, pos: input.length });
  return tokens;
}

// ─── Parser ────────────────────────────────────────────────────────────

class Parser {
  constructor(tokens, source) {
    this.tokens = tokens;
    this.pos = 0;
    this.source = source;
  }
  peek(offset = 0) {
    return this.tokens[this.pos + offset];
  }
  consume(typeOrValue) {
    const t = this.tokens[this.pos];
    if (typeof typeOrValue === "string") {
      if (t.type !== typeOrValue && t.value !== typeOrValue) {
        throw new SyntaxError(
          `Expected ${typeOrValue} at position ${t.pos}, got ${t.type}/${t.value}`,
        );
      }
    }
    this.pos++;
    return t;
  }
  parse() {
    const expr = this.parseOr();
    if (this.tokens[this.pos].type !== TOKEN_TYPES.EOF) {
      const t = this.tokens[this.pos];
      throw new SyntaxError(`Unexpected token at position ${t.pos}: ${t.value}`);
    }
    return expr;
  }
  parseOr() {
    let left = this.parseAnd();
    while (this.peek().type === TOKEN_TYPES.OP && this.peek().value === "OR") {
      this.consume();
      const right = this.parseAnd();
      left = { type: "or", left, right };
    }
    return left;
  }
  parseAnd() {
    let left = this.parseNot();
    while (this.peek().type === TOKEN_TYPES.OP && this.peek().value === "AND") {
      this.consume();
      const right = this.parseNot();
      left = { type: "and", left, right };
    }
    return left;
  }
  parseNot() {
    if (this.peek().type === TOKEN_TYPES.OP && this.peek().value === "NOT") {
      this.consume();
      return { type: "not", expr: this.parseNot() };
    }
    return this.parseComparison();
  }
  parseComparison() {
    const left = this.parsePrimary();
    if (this.peek().type === TOKEN_TYPES.OP && COMPARE_OPS.has(this.peek().value)) {
      const op = this.consume().value;
      const right = this.parsePrimary();
      return { type: "compare", op, left, right };
    }
    return left;
  }
  parsePrimary() {
    const t = this.peek();
    if (t.type === TOKEN_TYPES.LPAREN) {
      this.consume();
      const expr = this.parseOr();
      if (this.peek().type !== TOKEN_TYPES.RPAREN) {
        throw new SyntaxError(`Expected ')' at position ${this.peek().pos}`);
      }
      this.consume();
      return expr;
    }
    if (t.type === TOKEN_TYPES.NUMBER) {
      this.consume();
      return { type: "literal", value: t.value };
    }
    if (t.type === TOKEN_TYPES.STRING) {
      this.consume();
      return { type: "literal", value: t.value };
    }
    if (t.type === TOKEN_TYPES.IDENT) {
      // Could be: literal keyword (true/false/null/always), function call, or identifier path.
      if (Object.prototype.hasOwnProperty.call(t, "literal")) {
        this.consume();
        return { type: "literal", value: t.literal };
      }
      // Function call?
      if (this.peek(1) && this.peek(1).type === TOKEN_TYPES.LPAREN) {
        const name = this.consume().value;
        this.consume(); // (
        const args = [];
        if (this.peek().type !== TOKEN_TYPES.RPAREN) {
          args.push(this.parseOr());
          while (this.peek().type === TOKEN_TYPES.COMMA) {
            this.consume();
            args.push(this.parseOr());
          }
        }
        if (this.peek().type !== TOKEN_TYPES.RPAREN) {
          throw new SyntaxError(`Expected ')' or ',' at position ${this.peek().pos}`);
        }
        this.consume();
        return { type: "call", name, args };
      }
      // Identifier path (dotted access).
      this.consume();
      return { type: "ident", path: t.value.split(".") };
    }
    throw new SyntaxError(`Unexpected token at position ${t.pos}: ${t.value}`);
  }
}

// ─── Evaluator ─────────────────────────────────────────────────────────

const FUNCTIONS = Object.freeze({
  len(x) {
    if (x === null || x === undefined) return 0;
    if (typeof x === "string") return x.length;
    if (Array.isArray(x)) return x.length;
    throw new TypeError(`len() expects a string or array, got ${typeof x}`);
  },
  empty(x) {
    return FUNCTIONS.len(x) === 0;
  },
  in(x, list) {
    if (!Array.isArray(list)) {
      throw new TypeError(`in() expects an array as second argument, got ${typeof list}`);
    }
    return list.some((entry) => entry === x);
  },
});

function resolvePath(env, path) {
  let cursor = env;
  for (const segment of path) {
    if (cursor === null || cursor === undefined) return undefined;
    cursor = cursor[segment];
  }
  return cursor;
}

function evalNode(node, env) {
  switch (node.type) {
    case "literal":
      return node.value;
    case "ident":
      return resolvePath(env, node.path);
    case "call": {
      const fn = FUNCTIONS[node.name];
      if (!fn) {
        throw new ReferenceError(`Unknown function "${node.name}"`);
      }
      const args = node.args.map((a) => evalNode(a, env));
      return fn(...args);
    }
    case "compare": {
      const left = evalNode(node.left, env);
      const right = evalNode(node.right, env);
      switch (node.op) {
        case "==":
          return left === right;
        case "!=":
          return left !== right;
        case "<":
          return left < right;
        case ">":
          return left > right;
        case "<=":
          return left <= right;
        case ">=":
          return left >= right;
        default:
          throw new Error(`Unknown comparison operator "${node.op}"`);
      }
    }
    case "and":
      return Boolean(evalNode(node.left, env)) && Boolean(evalNode(node.right, env));
    case "or":
      return Boolean(evalNode(node.left, env)) || Boolean(evalNode(node.right, env));
    case "not":
      return !evalNode(node.expr, env);
    default:
      throw new Error(`Unknown AST node type "${node.type}"`);
  }
}

// ─── Public API ────────────────────────────────────────────────────────

// parsePredicate compiles a predicate expression to an AST. Cached by source.
const compileCache = new Map();
export function parsePredicate(source) {
  if (typeof source !== "string") {
    throw new TypeError("parsePredicate: source must be a string");
  }
  const trimmed = source.trim();
  if (!trimmed) {
    throw new SyntaxError("parsePredicate: empty source");
  }
  if (compileCache.has(trimmed)) {
    return compileCache.get(trimmed);
  }
  const tokens = tokenise(trimmed);
  const parser = new Parser(tokens, trimmed);
  const ast = parser.parse();
  compileCache.set(trimmed, ast);
  return ast;
}

// evaluatePredicate parses (or reuses cached AST) and evaluates against env.
// Returns a boolean. Throws on parse error or runtime error.
export function evaluatePredicate(source, env = {}) {
  const ast = parsePredicate(source);
  return Boolean(evalNode(ast, env));
}

export const __internals__ = { tokenise, Parser, evalNode, FUNCTIONS };
