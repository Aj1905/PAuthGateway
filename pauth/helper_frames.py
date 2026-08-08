"""Recognise the two Appendix-A helper forms with tool calls in lambdas.

The paper's examples put a tool call inside a helper lambda even though its
general rules reject nested tool calls.  Treating every such lambda as an
ordinary quantified loop is unsafe: Python may short-circuit the expression,
and ``first`` stops before visiting the rest of its input.  This module keeps
the exception deliberately narrow and gives the slicer explicit traversal
metadata for the forms whose execution order can be reproduced.
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Literal

from .symbolic import call_name


@dataclasses.dataclass(frozen=True)
class HelperFrame:
    """One ordered helper traversal surrounding a nested tool occurrence."""

    helper: Literal["min", "max", "first"]
    variable: str
    iterable: ast.expr
    body: ast.expr


@dataclasses.dataclass(frozen=True)
class HelperToolOccurrence:
    """A nested tool call and the helper traversal that reaches it."""

    call: ast.Call
    frame: HelperFrame


def _one_lambda_keyword(call: ast.Call, name: str) -> ast.Lambda | None:
    if len(call.keywords) != 1 or call.keywords[0].arg != name:
        return None
    value = call.keywords[0].value
    return value if isinstance(value, ast.Lambda) else None


def _one_parameter(lam: ast.Lambda) -> str | None:
    args = lam.args
    if (
        len(args.args) != 1
        or args.posonlyargs
        or args.vararg is not None
        or args.kwonlyargs
        or args.kwarg is not None
        or args.defaults
        or args.kw_defaults
    ):
        return None
    return args.args[0].arg


def _direct_tool_of_variable(
    node: ast.AST,
    variable: str,
    tool_names: set[str],
) -> ast.Call | None:
    if not isinstance(node, ast.Call):
        return None
    if call_name(node) not in tool_names or node.keywords or len(node.args) != 1:
        return None
    operand = node.args[0]
    if not isinstance(operand, ast.Name) or operand.id != variable:
        return None
    return node


def _tool_calls(node: ast.AST, tool_names: set[str]) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and call_name(child) in tool_names
    ]


def _match_min_max(
    call: ast.Call,
    tool_names: set[str],
) -> HelperToolOccurrence | None:
    helper = call_name(call)
    if helper not in {"min", "max"}:
        return None
    if len(call.args) != 1 or not isinstance(call.args[0], ast.Name):
        return None
    lam = _one_lambda_keyword(call, "key")
    if lam is None:
        return None
    variable = _one_parameter(lam)
    if variable is None:
        return None

    # Appendix A's concrete form is:
    #   max(xs, key=lambda x: len(tool(x)))
    body = lam.body
    if (
        not isinstance(body, ast.Call)
        or call_name(body) != "len"
        or body.keywords
        or len(body.args) != 1
    ):
        return None
    nested = _direct_tool_of_variable(body.args[0], variable, tool_names)
    if nested is None or _tool_calls(lam.body, tool_names) != [nested]:
        return None
    return HelperToolOccurrence(
        call=nested,
        frame=HelperFrame(helper, variable, call.args[0], lam.body),
    )


def _match_first(
    call: ast.Call,
    tool_names: set[str],
) -> HelperToolOccurrence | None:
    if call_name(call) != "first":
        return None
    if len(call.args) != 1 or not isinstance(call.args[0], ast.Name):
        return None
    outer_lambda = _one_lambda_keyword(call, "predicate")
    if outer_lambda is None:
        return None
    outer_variable = _one_parameter(outer_lambda)
    if outer_variable is None:
        return None

    # Appendix A's concrete form is:
    #   first(xs, predicate=lambda x:
    #       first(tool(x), predicate=lambda y: PURE(y)) is not None)
    body = outer_lambda.body
    if (
        not isinstance(body, ast.Compare)
        or len(body.ops) != 1
        or not isinstance(body.ops[0], ast.IsNot)
        or len(body.comparators) != 1
        or not isinstance(body.comparators[0], ast.Constant)
        or body.comparators[0].value is not None
        or not isinstance(body.left, ast.Call)
        or call_name(body.left) != "first"
    ):
        return None
    inner = body.left
    if len(inner.args) != 1:
        return None
    nested = _direct_tool_of_variable(
        inner.args[0], outer_variable, tool_names
    )
    if nested is None:
        return None
    inner_lambda = _one_lambda_keyword(inner, "predicate")
    if inner_lambda is None or _one_parameter(inner_lambda) is None:
        return None
    # The inner predicate must be pure.  Helpers remain allowed there because
    # they are deterministic; another external tool call would create a second
    # traversal whose ordering this frame does not model.
    if _tool_calls(inner_lambda.body, tool_names):
        return None
    if _tool_calls(outer_lambda.body, tool_names) != [nested]:
        return None
    return HelperToolOccurrence(
        call=nested,
        frame=HelperFrame("first", outer_variable, call.args[0], body),
    )


def helper_tool_occurrences(
    root: ast.AST,
    tool_names: set[str],
) -> list[HelperToolOccurrence]:
    """Return only the supported helper-lambda tool occurrences in ``root``."""

    found: list[HelperToolOccurrence] = []
    seen: set[int] = set()
    for node in ast.walk(root):
        if not isinstance(node, ast.Call):
            continue
        occurrence = _match_min_max(node, tool_names) or _match_first(
            node, tool_names
        )
        if occurrence is None or id(occurrence.call) in seen:
            continue
        seen.add(id(occurrence.call))
        found.append(occurrence)
    found.sort(
        key=lambda occurrence: (
            getattr(occurrence.call, "lineno", 0),
            getattr(occurrence.call, "col_offset", 0),
        )
    )
    return found
