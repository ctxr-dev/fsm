"""Sandboxed predicate DSL for FSM transition guards.

This module ports the deterministic predicate evaluator originally written
in JavaScript (the predecessor ``@ctxr/fsm`` Node package, archived at the
``legacy-js-archive`` git tag) to Python.
The DSL is intentionally tiny and side-effect-free; no ``eval``, ``exec`` or
``compile`` is used anywhere. Parsing and evaluation are implemented with a
hand-written tokenizer plus a recursive-descent parser.

Grammar
-------

::

    expr        := or_expr
    or_expr     := and_expr (OR and_expr)*
    and_expr    := not_expr (AND not_expr)*
    not_expr    := NOT not_expr | comparison
    comparison  := primary (CMP primary)?
    primary     := LPAREN or_expr RPAREN
                 | number
                 | string
                 | ident_path                       -- dotted env lookup
                 | function_call
                 | TRUE | FALSE | NULL | ALWAYS
    function_call := IDENT LPAREN (or_expr (COMMA or_expr)*)? RPAREN

Supported literals: integers, floats, single- or double-quoted strings
(with ``\\\\`` and ``\\'``/``\\"`` escapes), the keywords ``true``, ``false``,
``null`` (case-insensitive), and the sentinel ``always`` (always true).

Logical operators: ``AND``/``&&``, ``OR``/``||``, ``NOT``/``!`` —
case-insensitive.

Comparison operators: ``==`` ``!=`` ``<`` ``>`` ``<=`` ``>=``. Ordered
comparisons across incompatible types return ``False`` instead of raising.

Functions (the only callable names):

* ``len(x)``    — length of ``str``/``list``/``tuple``/``dict``; ``len(None)``
  is ``0`` (matching the JS port semantics for missing identifiers); any
  other type raises ``PredicateEvalError``.
* ``empty(x)``  — ``True`` if ``x`` is ``None`` or has length zero.
* ``in(x, h)``  — ``True`` if ``x`` is a substring of the string ``h``, an
  element of the list/tuple ``h``, or a key of the dict ``h``.

Public surface
--------------

``evaluate_expression(expr, env) -> bool`` parses ``expr``, evaluates it
against ``env`` and coerces the result to ``bool``. ``validate_expression``
returns ``True``/``False`` without raising. ``tokenize`` and ``parse`` are
exported for debugging and tests. ``PredicateParseError`` and
``PredicateEvalError`` are the only exception types raised by this module
(everything else is wrapped).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "PredicateEvalError",
    "PredicateParseError",
    "evaluate_expression",
    "parse",
    "tokenize",
    "validate_expression",
]


# ─── Errors ────────────────────────────────────────────────────────────


class PredicateParseError(Exception):
    """Raised when a predicate source string fails to tokenize or parse.

    The optional ``position`` attribute records the 0-based character
    offset within the source string that triggered the error, or ``None``
    when the position is unknown (for example, for empty input).
    """

    def __init__(self, message: str, position: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.position = position

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.position is None:
            return self.message
        return f"{self.message} (at position {self.position})"


class PredicateEvalError(Exception):
    """Raised when an otherwise well-formed predicate fails at runtime.

    Typical causes are calling ``len()`` on a non-collection value or
    invoking an unknown function. The optional ``position`` attribute
    records the source offset of the offending construct when available.
    """

    def __init__(self, message: str, position: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.position = position

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.position is None:
            return self.message
        return f"{self.message} (at position {self.position})"


# ─── Tokens ────────────────────────────────────────────────────────────


class TokenKind(StrEnum):
    """Lexical token categories emitted by :func:`tokenize`."""

    NUMBER = "number"
    STRING = "string"
    IDENT = "identifier"
    OP = "op"
    LPAREN = "lparen"
    RPAREN = "rparen"
    COMMA = "comma"
    KEYWORD = "keyword"
    EOF = "eof"


@dataclass(frozen=True)
class Token:
    """A single lexical token produced by the tokenizer.

    ``kind`` selects the category, ``value`` carries the canonical
    payload (a Python ``int``/``float`` for numbers, the decoded text for
    strings, the upper-cased keyword for keywords, the source slice for
    identifiers and operators), and ``position`` is the 0-based offset
    of the first character in the source string.
    """

    kind: TokenKind
    value: Any
    position: int


# Recognised reserved keywords. Operator keywords (AND/OR/NOT) become
# ``OP`` tokens with their canonical upper-case value; literal keywords
# (TRUE/FALSE/NULL/ALWAYS) become ``KEYWORD`` tokens carrying the
# corresponding Python literal.
_OPERATOR_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})
_LITERAL_KEYWORDS: dict[str, Any] = {
    "TRUE": True,
    "FALSE": False,
    "NULL": None,
    "ALWAYS": True,
}
_COMPARE_OPS: frozenset[str] = frozenset({"==", "!=", "<", ">", "<=", ">="})


def _is_digit(ch: str) -> bool:
    return "0" <= ch <= "9"


def _is_ident_start(ch: str) -> bool:
    return ch == "_" or ("A" <= ch <= "Z") or ("a" <= ch <= "z")


def _is_ident_part(ch: str) -> bool:
    return _is_ident_start(ch) or _is_digit(ch) or ch == "."


def tokenize(source: str) -> list[Token]:
    """Tokenize ``source`` into a flat list of :class:`Token`\\ s.

    The last token is always a ``TokenKind.EOF`` sentinel whose
    ``position`` equals ``len(source)``. Raises
    :class:`PredicateParseError` on unknown characters, malformed
    numbers, or unterminated string literals.
    """

    if not isinstance(source, str):
        raise PredicateParseError("tokenize: source must be a string")
    tokens: list[Token] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        # Skip whitespace.
        if ch in " \t\n\r":
            i += 1
            continue
        if ch == "(":
            tokens.append(Token(TokenKind.LPAREN, "(", i))
            i += 1
            continue
        if ch == ")":
            tokens.append(Token(TokenKind.RPAREN, ")", i))
            i += 1
            continue
        if ch == ",":
            tokens.append(Token(TokenKind.COMMA, ",", i))
            i += 1
            continue
        # Two-char operators first.
        if ch == "&" and i + 1 < n and source[i + 1] == "&":
            tokens.append(Token(TokenKind.OP, "AND", i))
            i += 2
            continue
        if ch == "|" and i + 1 < n and source[i + 1] == "|":
            tokens.append(Token(TokenKind.OP, "OR", i))
            i += 2
            continue
        if ch == "=" and i + 1 < n and source[i + 1] == "=":
            tokens.append(Token(TokenKind.OP, "==", i))
            i += 2
            continue
        if ch == "!" and i + 1 < n and source[i + 1] == "=":
            tokens.append(Token(TokenKind.OP, "!=", i))
            i += 2
            continue
        if ch == "<" and i + 1 < n and source[i + 1] == "=":
            tokens.append(Token(TokenKind.OP, "<=", i))
            i += 2
            continue
        if ch == ">" and i + 1 < n and source[i + 1] == "=":
            tokens.append(Token(TokenKind.OP, ">=", i))
            i += 2
            continue
        if ch == "!":
            tokens.append(Token(TokenKind.OP, "NOT", i))
            i += 1
            continue
        if ch == "<":
            tokens.append(Token(TokenKind.OP, "<", i))
            i += 1
            continue
        if ch == ">":
            tokens.append(Token(TokenKind.OP, ">", i))
            i += 1
            continue
        # String literal (single or double quoted) with backslash escapes.
        if ch == "'" or ch == '"':
            start = i
            quote = ch
            i += 1
            buf: list[str] = []
            while i < n and source[i] != quote:
                if source[i] == "\\" and i + 1 < n:
                    buf.append(source[i + 1])
                    i += 2
                else:
                    buf.append(source[i])
                    i += 1
            if i >= n:
                raise PredicateParseError(
                    f"Unterminated string literal starting at position {start}",
                    position=start,
                )
            i += 1  # consume closing quote
            tokens.append(Token(TokenKind.STRING, "".join(buf), start))
            continue
        # Numeric literal: integer or float (one optional decimal point).
        if _is_digit(ch):
            start = i
            has_dot = False
            while i < n and (_is_digit(source[i]) or source[i] == "."):
                if source[i] == ".":
                    if has_dot:
                        raise PredicateParseError(
                            f'Malformed number at position {start}',
                            position=start,
                        )
                    has_dot = True
                i += 1
            raw = source[start:i]
            try:
                value: int | float = float(raw) if has_dot else int(raw)
            except ValueError as exc:
                raise PredicateParseError(
                    f'Malformed number "{raw}" at position {start}',
                    position=start,
                ) from exc
            tokens.append(Token(TokenKind.NUMBER, value, start))
            continue
        # Identifier or keyword.
        if _is_ident_start(ch):
            start = i
            while i < n and _is_ident_part(source[i]):
                i += 1
            raw = source[start:i]
            upper = raw.upper()
            if upper in _OPERATOR_KEYWORDS:
                tokens.append(Token(TokenKind.OP, upper, start))
                continue
            if upper in _LITERAL_KEYWORDS:
                # Normalise the surface form to the canonical lower-case
                # keyword and stash the literal Python value alongside it.
                tokens.append(
                    Token(TokenKind.KEYWORD, (upper, _LITERAL_KEYWORDS[upper]), start)
                )
                continue
            # Plain identifier (possibly dotted). Reject obviously
            # malformed forms like ``a..b`` or trailing/leading dots so
            # the parser never has to grapple with empty path segments.
            if raw.startswith(".") or raw.endswith(".") or ".." in raw:
                raise PredicateParseError(
                    f'Malformed identifier "{raw}" at position {start}',
                    position=start,
                )
            tokens.append(Token(TokenKind.IDENT, raw, start))
            continue
        raise PredicateParseError(
            f'Unexpected character "{ch}" at position {i}', position=i
        )
    tokens.append(Token(TokenKind.EOF, None, n))
    return tokens


# ─── AST ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Literal:
    """Numeric, string, boolean, or null literal."""

    value: Any
    position: int


@dataclass(frozen=True)
class Identifier:
    """Dotted identifier path looked up against the evaluator env."""

    path: tuple[str, ...]
    position: int


@dataclass(frozen=True)
class BinaryOp:
    """Comparison or logical binary operator (``op`` ∈ {==, !=, <, >, <=, >=, AND, OR})."""

    op: str
    left: Node
    right: Node
    position: int


@dataclass(frozen=True)
class UnaryOp:
    """Unary operator. Currently only ``NOT`` is supported."""

    op: str
    operand: Node
    position: int


@dataclass(frozen=True)
class FunctionCall:
    """Call to one of the whitelisted built-in functions."""

    name: str
    args: tuple[Node, ...]
    position: int


Node = Literal | Identifier | BinaryOp | UnaryOp | FunctionCall


# Functions whitelisted in the DSL. Names are validated at parse time so
# unknown calls fail fast instead of surfacing at evaluation time.
_ALLOWED_FUNCTIONS: frozenset[str] = frozenset({"len", "empty", "in"})


# ─── Parser ────────────────────────────────────────────────────────────


class _Parser:
    """Hand-written recursive-descent parser for the predicate DSL.

    The parser is single-pass and produces an immutable AST of dataclass
    nodes. It only ever raises :class:`PredicateParseError`.
    """

    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    # ── helpers ───────────────────────────────────────────────────────

    def _peek(self, offset: int = 0) -> Token:
        idx = self._pos + offset
        if idx >= len(self._tokens):
            return self._tokens[-1]
        return self._tokens[idx]

    def _advance(self) -> Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, kind: TokenKind, label: str) -> Token:
        tok = self._peek()
        if tok.kind is not kind:
            raise PredicateParseError(
                f"Expected {label} at position {tok.position}, got {tok.kind.value}",
                position=tok.position,
            )
        return self._advance()

    # ── grammar entry point ───────────────────────────────────────────

    def parse(self) -> Node:
        if not self._tokens or self._tokens[0].kind is TokenKind.EOF:
            raise PredicateParseError("Empty expression", position=0)
        node = self._parse_or()
        tail = self._peek()
        if tail.kind is not TokenKind.EOF:
            raise PredicateParseError(
                f"Unexpected token at position {tail.position}: {tail.value!r}",
                position=tail.position,
            )
        return node

    # ── precedence climbing ───────────────────────────────────────────

    def _parse_or(self) -> Node:
        left = self._parse_and()
        while self._peek().kind is TokenKind.OP and self._peek().value == "OR":
            op_tok = self._advance()
            right = self._parse_and()
            left = BinaryOp("OR", left, right, op_tok.position)
        return left

    def _parse_and(self) -> Node:
        left = self._parse_not()
        while self._peek().kind is TokenKind.OP and self._peek().value == "AND":
            op_tok = self._advance()
            right = self._parse_not()
            left = BinaryOp("AND", left, right, op_tok.position)
        return left

    def _parse_not(self) -> Node:
        if self._peek().kind is TokenKind.OP and self._peek().value == "NOT":
            op_tok = self._advance()
            operand = self._parse_not()
            return UnaryOp("NOT", operand, op_tok.position)
        return self._parse_comparison()

    def _parse_comparison(self) -> Node:
        left = self._parse_primary()
        nxt = self._peek()
        if nxt.kind is TokenKind.OP and nxt.value in _COMPARE_OPS:
            op_tok = self._advance()
            right = self._parse_primary()
            return BinaryOp(op_tok.value, left, right, op_tok.position)
        return left

    def _parse_primary(self) -> Node:
        tok = self._peek()
        # Parenthesised sub-expression.
        if tok.kind is TokenKind.LPAREN:
            self._advance()
            inner = self._parse_or()
            close = self._peek()
            if close.kind is not TokenKind.RPAREN:
                raise PredicateParseError(
                    f"Expected ')' at position {close.position}",
                    position=close.position,
                )
            self._advance()
            return inner
        # Numeric / string literal.
        if tok.kind is TokenKind.NUMBER or tok.kind is TokenKind.STRING:
            self._advance()
            return Literal(tok.value, tok.position)
        # Reserved literal keyword (true/false/null/always).
        if tok.kind is TokenKind.KEYWORD:
            self._advance()
            _name, literal_value = tok.value
            return Literal(literal_value, tok.position)
        # Identifier — either a function call or a dotted path lookup.
        if tok.kind is TokenKind.IDENT:
            # Function call: identifier immediately followed by '('.
            nxt = self._peek(1)
            if nxt.kind is TokenKind.LPAREN:
                name_tok = self._advance()
                name = name_tok.value
                if name not in _ALLOWED_FUNCTIONS:
                    raise PredicateParseError(
                        f'Unknown function "{name}" at position {name_tok.position}',
                        position=name_tok.position,
                    )
                self._advance()  # consume '('
                args: list[Node] = []
                if self._peek().kind is not TokenKind.RPAREN:
                    args.append(self._parse_or())
                    while self._peek().kind is TokenKind.COMMA:
                        self._advance()
                        args.append(self._parse_or())
                close = self._peek()
                if close.kind is not TokenKind.RPAREN:
                    raise PredicateParseError(
                        f"Expected ')' or ',' at position {close.position}",
                        position=close.position,
                    )
                self._advance()
                return FunctionCall(name, tuple(args), name_tok.position)
            # Plain dotted identifier path.
            self._advance()
            path = tuple(tok.value.split("."))
            if any(segment == "" for segment in path):
                raise PredicateParseError(
                    f'Malformed identifier "{tok.value}" at position {tok.position}',
                    position=tok.position,
                )
            return Identifier(path, tok.position)
        raise PredicateParseError(
            f"Unexpected token at position {tok.position}: {tok.value!r}",
            position=tok.position,
        )


def parse(source: str) -> Node:
    """Parse ``source`` into an AST.

    Equivalent to ``_Parser(tokenize(source)).parse()`` but with an
    explicit check for empty input so callers get a stable error type.
    Raises :class:`PredicateParseError` on any malformed input.
    """

    if not isinstance(source, str):
        raise PredicateParseError("parse: source must be a string")
    trimmed = source.strip()
    if not trimmed:
        raise PredicateParseError("Empty expression", position=0)
    tokens = tokenize(trimmed)
    return _Parser(tokens).parse()


# ─── Evaluator ─────────────────────────────────────────────────────────


def _resolve_path(env: Any, path: tuple[str, ...]) -> Any:
    """Walk ``path`` through nested mappings/objects, returning ``None`` on miss.

    Mirrors the JS reference behaviour where missing identifier segments
    short-circuit to ``undefined`` (here, Python ``None``) instead of
    raising. Mapping keys are tried first; falls back to ``getattr`` so
    Pydantic models and dataclasses also work transparently.
    """

    cursor: Any = env
    for segment in path:
        if cursor is None:
            return None
        if isinstance(cursor, dict):
            if segment in cursor:
                cursor = cursor[segment]
                continue
            return None
        # Last-ditch: attribute access for objects (Pydantic models etc).
        try:
            cursor = getattr(cursor, segment)
        except AttributeError:
            return None
    return cursor


def _fn_len(value: Any, position: int) -> int:
    """Evaluate ``len(value)`` with the DSL's permissive semantics."""

    if value is None:
        return 0
    if isinstance(value, str | list | tuple | dict):
        return len(value)
    raise PredicateEvalError(
        f"len() expects a string, list, tuple, or dict; got {type(value).__name__}",
        position=position,
    )


def _fn_empty(value: Any, position: int) -> bool:
    """Evaluate ``empty(value)`` as a shortcut for ``len(value) == 0``."""

    return _fn_len(value, position) == 0


def _fn_in(needle: Any, haystack: Any, position: int) -> bool:
    """Evaluate ``in(needle, haystack)`` across strings, lists, and dicts."""

    if haystack is None:
        return False
    if isinstance(haystack, str):
        if not isinstance(needle, str):
            return False
        return needle in haystack
    if isinstance(haystack, list | tuple):
        return any(item == needle for item in haystack)
    if isinstance(haystack, dict):
        try:
            return needle in haystack
        except TypeError:
            return False
    raise PredicateEvalError(
        f"in() expects a string, list, tuple, or dict as second argument; "
        f"got {type(haystack).__name__}",
        position=position,
    )


def _truthy(value: Any) -> bool:
    """Coerce a DSL value to a Python boolean.

    Mirrors the JS port's reliance on ``Boolean(x)``: ``None``, zero,
    empty containers, and empty strings are falsy; everything else is
    truthy.
    """

    return bool(value)


def _compare(op: str, left: Any, right: Any) -> bool:
    """Apply a comparison operator with cross-type safety.

    Equality and inequality return Python's natural answer (so
    ``3 == 'a'`` is ``False`` without raising). Ordered comparisons
    return ``False`` whenever Python would raise ``TypeError`` for
    mismatched operand types — this matches the documented "cross-type
    comparisons return False, never raise" contract.
    """

    # bool(...) narrows Python's `Any`-typed comparison results to a
    # concrete bool so mypy doesn't complain about returning `Any` from
    # a `-> bool` function (the comparison operators return Any when
    # called on Any operands).
    if op == "==":
        return bool(left == right)
    if op == "!=":
        return bool(left != right)
    try:
        if op == "<":
            return bool(left < right)
        if op == ">":
            return bool(left > right)
        if op == "<=":
            return bool(left <= right)
        if op == ">=":
            return bool(left >= right)
    except TypeError:
        return False
    # Unreachable: the parser only emits operators from _COMPARE_OPS.
    raise PredicateEvalError(f"Unknown comparison operator {op!r}")


def _evaluate(node: Node, env: dict[str, Any]) -> Any:
    """Recursively evaluate an AST node against ``env``.

    Returns ``Any`` because intermediate values can be numbers, strings,
    booleans, or ``None``; the public entry point coerces the final
    result to ``bool``.
    """

    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Identifier):
        return _resolve_path(env, node.path)
    if isinstance(node, FunctionCall):
        args = [_evaluate(arg, env) for arg in node.args]
        if node.name == "len":
            if len(args) != 1:
                raise PredicateEvalError(
                    f"len() takes exactly 1 argument ({len(args)} given)",
                    position=node.position,
                )
            return _fn_len(args[0], node.position)
        if node.name == "empty":
            if len(args) != 1:
                raise PredicateEvalError(
                    f"empty() takes exactly 1 argument ({len(args)} given)",
                    position=node.position,
                )
            return _fn_empty(args[0], node.position)
        if node.name == "in":
            if len(args) != 2:
                raise PredicateEvalError(
                    f"in() takes exactly 2 arguments ({len(args)} given)",
                    position=node.position,
                )
            return _fn_in(args[0], args[1], node.position)
        # Parser refuses unknown names, so this is purely defensive.
        raise PredicateEvalError(
            f'Unknown function "{node.name}"', position=node.position
        )
    if isinstance(node, BinaryOp):
        if node.op == "AND":
            left = _evaluate(node.left, env)
            if not _truthy(left):
                return False
            return _truthy(_evaluate(node.right, env))
        if node.op == "OR":
            left = _evaluate(node.left, env)
            if _truthy(left):
                return True
            return _truthy(_evaluate(node.right, env))
        left_val = _evaluate(node.left, env)
        right_val = _evaluate(node.right, env)
        return _compare(node.op, left_val, right_val)
    if isinstance(node, UnaryOp):
        if node.op == "NOT":
            return not _truthy(_evaluate(node.operand, env))
        raise PredicateEvalError(
            f"Unknown unary operator {node.op!r}", position=node.position
        )
    # Defensive: parser will not emit anything else.
    raise PredicateEvalError(  # pragma: no cover
        f"Unknown AST node type {type(node).__name__}"
    )


# ─── Public entry points ───────────────────────────────────────────────


def evaluate_expression(expression: str, env: dict[str, Any] | None = None) -> bool:
    """Parse and evaluate ``expression`` against ``env``, returning ``bool``.

    Parameters
    ----------
    expression:
        DSL source. Must be a non-empty string after trimming.
    env:
        Mapping consulted for identifier lookups. Missing keys evaluate
        to ``None`` rather than raising.

    Raises
    ------
    PredicateParseError
        If the source cannot be tokenized or parsed.
    PredicateEvalError
        If evaluation hits an unknown function, a malformed call, or a
        type error in one of the built-in functions.
    """

    if env is None:
        env = {}
    ast = parse(expression)
    result = _evaluate(ast, env)
    return _truthy(result)


def validate_expression(expression: str) -> bool:
    """Return ``True`` if ``expression`` is syntactically valid, else ``False``.

    This is a parse-only check: it does not require an environment and
    never evaluates the expression. Useful for FSM-spec linting where
    we want to surface predicate errors at load time without committing
    to particular runtime data.
    """

    try:
        parse(expression)
    except PredicateParseError:
        return False
    return True
