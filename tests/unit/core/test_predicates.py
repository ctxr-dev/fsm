"""Unit tests for the sandboxed predicate DSL in ``ctxr.fsm.core.predicates``.

Covers the tokenizer, parser, evaluator, and the validation helper.
"""

from __future__ import annotations

from typing import Any

import pytest

from ctxr.fsm.core.predicates import (
    BinaryOp,
    FunctionCall,
    Identifier,
    Literal,
    PredicateEvalError,
    PredicateParseError,
    TokenKind,
    UnaryOp,
    evaluate_expression,
    parse,
    tokenize,
    validate_expression,
)

# ─── tokenizer ─────────────────────────────────────────────────────────


def test_tokenize_rejects_unknown_character() -> None:
    with pytest.raises(PredicateParseError) as excinfo:
        tokenize("a @ b")
    assert excinfo.value.position == 2
    assert "@" in str(excinfo.value)


def test_tokenize_rejects_at_symbol_as_unknown_char() -> None:
    with pytest.raises(PredicateParseError):
        tokenize("#nope")


def test_tokenize_emits_eof_sentinel() -> None:
    tokens = tokenize("1")
    assert tokens[-1].kind is TokenKind.EOF
    assert tokens[-1].position == 1


def test_tokenize_int_and_float_literals() -> None:
    tokens = tokenize("3 4.5")
    assert tokens[0].kind is TokenKind.NUMBER
    assert tokens[0].value == 3
    assert isinstance(tokens[0].value, int)
    assert tokens[1].kind is TokenKind.NUMBER
    assert tokens[1].value == 4.5
    assert isinstance(tokens[1].value, float)


def test_tokenize_malformed_number_two_dots_raises() -> None:
    with pytest.raises(PredicateParseError):
        tokenize("1.2.3")


def test_tokenize_unterminated_string_raises() -> None:
    with pytest.raises(PredicateParseError) as excinfo:
        tokenize("'oops")
    assert excinfo.value.position == 0


def test_tokenize_string_escape_sequences() -> None:
    tokens = tokenize(r"'it\'s ok'")
    assert tokens[0].kind is TokenKind.STRING
    assert tokens[0].value == "it's ok"


def test_tokenize_logical_operator_keywords_are_case_insensitive() -> None:
    tokens = tokenize("a And b oR c nOt d")
    op_values = [t.value for t in tokens if t.kind is TokenKind.OP]
    assert op_values == ["AND", "OR", "NOT"]


def test_tokenize_double_amp_and_pipe_aliases_for_and_or() -> None:
    tokens = tokenize("a && b || c")
    op_values = [t.value for t in tokens if t.kind is TokenKind.OP]
    assert op_values == ["AND", "OR"]


def test_tokenize_bang_is_not_alias() -> None:
    tokens = tokenize("!x")
    assert tokens[0].kind is TokenKind.OP
    assert tokens[0].value == "NOT"


def test_tokenize_literal_keywords_become_keyword_tokens() -> None:
    tokens = tokenize("true FALSE Null AlwAys")
    kinds = [t.kind for t in tokens if t.kind is not TokenKind.EOF]
    assert kinds == [TokenKind.KEYWORD] * 4
    values = [t.value for t in tokens if t.kind is TokenKind.KEYWORD]
    # value is (canonical_upper_name, python_literal)
    assert values[0] == ("TRUE", True)
    assert values[1] == ("FALSE", False)
    assert values[2] == ("NULL", None)
    assert values[3] == ("ALWAYS", True)


def test_tokenize_dotted_identifier() -> None:
    tokens = tokenize("ctx.user.name")
    assert tokens[0].kind is TokenKind.IDENT
    assert tokens[0].value == "ctx.user.name"


def test_tokenize_rejects_double_dot_identifier() -> None:
    with pytest.raises(PredicateParseError):
        tokenize("a..b")


def test_tokenize_parens_and_comma() -> None:
    tokens = tokenize("(a, b)")
    kinds = [t.kind for t in tokens]
    assert kinds == [
        TokenKind.LPAREN,
        TokenKind.IDENT,
        TokenKind.COMMA,
        TokenKind.IDENT,
        TokenKind.RPAREN,
        TokenKind.EOF,
    ]


# ─── parser ────────────────────────────────────────────────────────────


def test_parse_number_literal() -> None:
    node = parse("42")
    assert isinstance(node, Literal)
    assert node.value == 42


def test_parse_string_literal() -> None:
    node = parse('"hello"')
    assert isinstance(node, Literal)
    assert node.value == "hello"


def test_parse_keyword_literals() -> None:
    assert isinstance(parse("true"), Literal)
    assert parse("true").value is True
    assert parse("false").value is False
    assert parse("null").value is None
    assert parse("always").value is True


def test_parse_simple_identifier() -> None:
    node = parse("foo")
    assert isinstance(node, Identifier)
    assert node.path == ("foo",)


def test_parse_dotted_identifier() -> None:
    node = parse("ctx.user.name")
    assert isinstance(node, Identifier)
    assert node.path == ("ctx", "user", "name")


def test_parse_comparison_binary_op() -> None:
    node = parse("a == 1")
    assert isinstance(node, BinaryOp)
    assert node.op == "=="
    assert isinstance(node.left, Identifier)
    assert isinstance(node.right, Literal)


def test_parse_all_comparison_operators() -> None:
    for op in ("==", "!=", "<", ">", "<=", ">="):
        node = parse(f"a {op} 1")
        assert isinstance(node, BinaryOp)
        assert node.op == op


def test_parse_logical_and_or_precedence() -> None:
    # a OR b AND c should parse as a OR (b AND c)
    node = parse("a OR b AND c")
    assert isinstance(node, BinaryOp)
    assert node.op == "OR"
    assert isinstance(node.right, BinaryOp)
    assert node.right.op == "AND"


def test_parse_not_operator() -> None:
    node = parse("NOT a")
    assert isinstance(node, UnaryOp)
    assert node.op == "NOT"
    assert isinstance(node.operand, Identifier)


def test_parse_parens_override_precedence() -> None:
    # (a OR b) AND c — outer is AND, not OR
    node = parse("(a OR b) AND c")
    assert isinstance(node, BinaryOp)
    assert node.op == "AND"
    assert isinstance(node.left, BinaryOp)
    assert node.left.op == "OR"


def test_parse_len_function_call() -> None:
    node = parse("len(items)")
    assert isinstance(node, FunctionCall)
    assert node.name == "len"
    assert len(node.args) == 1


def test_parse_empty_function_call() -> None:
    node = parse("empty(items)")
    assert isinstance(node, FunctionCall)
    assert node.name == "empty"


def test_parse_in_function_call() -> None:
    node = parse("in('x', items)")
    assert isinstance(node, FunctionCall)
    assert node.name == "in"
    assert len(node.args) == 2


def test_parse_rejects_unknown_function() -> None:
    with pytest.raises(PredicateParseError):
        parse("foobar(x)")


def test_parse_empty_expression_raises() -> None:
    with pytest.raises(PredicateParseError):
        parse("")


def test_parse_whitespace_only_raises() -> None:
    with pytest.raises(PredicateParseError):
        parse("   \t\n")


def test_parse_unclosed_paren_raises() -> None:
    with pytest.raises(PredicateParseError):
        parse("(a AND b")


def test_parse_trailing_garbage_raises() -> None:
    with pytest.raises(PredicateParseError):
        parse("a == 1 ) garbage")


def test_parse_dangling_operator_raises() -> None:
    with pytest.raises(PredicateParseError):
        parse("a AND")


# ─── evaluator: literals and identifiers ───────────────────────────────


def test_evaluate_true_literal_is_truthy() -> None:
    assert evaluate_expression("true") is True


def test_evaluate_false_literal_is_falsy() -> None:
    assert evaluate_expression("false") is False


def test_evaluate_always_is_truthy() -> None:
    assert evaluate_expression("always") is True


def test_evaluate_null_literal_is_falsy() -> None:
    assert evaluate_expression("null") is False


def test_evaluate_nonzero_number_is_truthy() -> None:
    assert evaluate_expression("1") is True


def test_evaluate_zero_is_falsy() -> None:
    assert evaluate_expression("0") is False


def test_evaluate_nonempty_string_is_truthy() -> None:
    assert evaluate_expression('"x"') is True


def test_evaluate_empty_string_is_falsy() -> None:
    assert evaluate_expression('""') is False


def test_evaluate_identifier_lookup_returns_value() -> None:
    assert evaluate_expression("flag", {"flag": True}) is True
    assert evaluate_expression("flag", {"flag": False}) is False


def test_evaluate_dotted_identifier_lookup() -> None:
    env: dict[str, Any] = {"ctx": {"user": {"name": "alice"}}}
    assert evaluate_expression("ctx.user.name == 'alice'", env) is True


def test_evaluate_missing_identifier_returns_none_falsy() -> None:
    # Missing identifier resolves to None; bare identifier should be falsy.
    assert evaluate_expression("missing") is False


def test_evaluate_missing_identifier_segment_is_falsy() -> None:
    # Partial path miss should also be falsy (None -> bool False).
    env: dict[str, Any] = {"ctx": {"user": {}}}
    assert evaluate_expression("ctx.user.name", env) is False


def test_evaluate_missing_identifier_compared_to_null_is_truthy() -> None:
    # The "missing -> None" contract: equality to null must be True.
    assert evaluate_expression("missing == null") is True


# ─── evaluator: comparisons ────────────────────────────────────────────


def test_evaluate_numeric_equality() -> None:
    assert evaluate_expression("1 == 1") is True
    assert evaluate_expression("1 == 2") is False


def test_evaluate_numeric_ordered_comparisons() -> None:
    assert evaluate_expression("1 < 2") is True
    assert evaluate_expression("2 <= 2") is True
    assert evaluate_expression("3 > 2") is True
    assert evaluate_expression("3 >= 3") is True


def test_evaluate_cross_type_ordered_comparison_returns_false() -> None:
    # Per docstring: ordered comparisons across incompatible types -> False.
    assert evaluate_expression("1 < 'x'") is False
    assert evaluate_expression("'x' >= 2") is False


def test_evaluate_cross_type_equality_is_false_not_raise() -> None:
    assert evaluate_expression("1 == 'x'") is False
    assert evaluate_expression("1 != 'x'") is True


# ─── evaluator: logical operators ──────────────────────────────────────


def test_evaluate_and_short_circuit() -> None:
    assert evaluate_expression("true AND true") is True
    assert evaluate_expression("true AND false") is False
    assert evaluate_expression("false AND true") is False


def test_evaluate_or_short_circuit() -> None:
    assert evaluate_expression("false OR true") is True
    assert evaluate_expression("true OR false") is True
    assert evaluate_expression("false OR false") is False


def test_evaluate_not_operator() -> None:
    assert evaluate_expression("NOT false") is True
    assert evaluate_expression("NOT true") is False


def test_evaluate_parens_change_precedence() -> None:
    # Without parens: false AND (false OR true) -> false
    # With explicit parens: (false AND false) OR true -> true
    assert evaluate_expression("false AND (false OR true)") is False
    assert evaluate_expression("(false AND false) OR true") is True


# ─── evaluator: functions ──────────────────────────────────────────────


def test_evaluate_len_of_string() -> None:
    assert evaluate_expression("len(s) == 3", {"s": "abc"}) is True


def test_evaluate_len_of_list() -> None:
    assert evaluate_expression("len(xs) == 2", {"xs": [1, 2]}) is True


def test_evaluate_len_of_dict() -> None:
    assert evaluate_expression("len(d) == 1", {"d": {"a": 1}}) is True


def test_evaluate_len_of_missing_identifier_is_zero() -> None:
    # _fn_len(None) == 0 per the docstring contract.
    assert evaluate_expression("len(missing) == 0") is True


def test_evaluate_len_of_unsupported_type_raises() -> None:
    with pytest.raises(PredicateEvalError):
        evaluate_expression("len(n)", {"n": 42})


def test_evaluate_empty_true_for_none() -> None:
    assert evaluate_expression("empty(missing)") is True


def test_evaluate_empty_true_for_empty_collection() -> None:
    assert evaluate_expression("empty(xs)", {"xs": []}) is True
    assert evaluate_expression("empty(s)", {"s": ""}) is True


def test_evaluate_empty_false_for_nonempty() -> None:
    assert evaluate_expression("empty(xs)", {"xs": [1]}) is False
    assert evaluate_expression("empty(s)", {"s": "x"}) is False


def test_evaluate_in_substring() -> None:
    assert evaluate_expression("in('ell', s)", {"s": "hello"}) is True
    assert evaluate_expression("in('zzz', s)", {"s": "hello"}) is False


def test_evaluate_in_list() -> None:
    assert evaluate_expression("in(2, xs)", {"xs": [1, 2, 3]}) is True
    assert evaluate_expression("in(9, xs)", {"xs": [1, 2, 3]}) is False


def test_evaluate_in_dict_key() -> None:
    assert evaluate_expression("in('a', d)", {"d": {"a": 1}}) is True
    assert evaluate_expression("in('b', d)", {"d": {"a": 1}}) is False


def test_evaluate_in_with_none_haystack_is_false() -> None:
    assert evaluate_expression("in('x', missing)") is False


def test_evaluate_len_arity_mismatch_raises() -> None:
    with pytest.raises(PredicateEvalError):
        evaluate_expression("len(a, b)", {"a": "x", "b": "y"})


def test_evaluate_in_arity_mismatch_raises() -> None:
    with pytest.raises(PredicateEvalError):
        evaluate_expression("in(a)", {"a": "x"})


# ─── malformed expressions raise PredicateParseError ──────────────────


def test_evaluate_malformed_raises_parse_error() -> None:
    with pytest.raises(PredicateParseError):
        evaluate_expression("a AND")


def test_evaluate_unknown_function_raises_parse_error() -> None:
    with pytest.raises(PredicateParseError):
        evaluate_expression("foobar(x)")


def test_evaluate_unknown_character_raises_parse_error() -> None:
    with pytest.raises(PredicateParseError):
        evaluate_expression("a @ b")


# ─── validate_expression contract ─────────────────────────────────────


def test_validate_expression_true_for_valid() -> None:
    assert validate_expression("a == 1") is True
    assert validate_expression("true") is True
    assert validate_expression("len(xs) > 0") is True


def test_validate_expression_false_for_malformed_no_raise() -> None:
    # Must return False without raising, per the docstring contract.
    assert validate_expression("a AND") is False
    assert validate_expression("(unclosed") is False
    assert validate_expression("") is False
    assert validate_expression("@@@") is False
    assert validate_expression("foobar(x)") is False
