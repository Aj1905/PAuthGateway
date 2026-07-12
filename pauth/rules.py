"""Compile a slice into enforcer rules (paper sec. 4.1.2, Algorithm 1).

Parsing and analysing a slice on every call would be inefficient, so PAuth
compiles each slice once, at slice-generation time, into reusable rules that
the enforcer consumes directly at runtime (paper sec. 4.1, step A3).

A rule records (paper "In general, a rule records five pieces of
information"): the expected tool, a per-operand symbolic expression, a guard
predicate conjoining all asserts, the let-defined names, and the set of tools
that produce envelopes the rule depends on.
"""

from __future__ import annotations

import ast
import dataclasses

from .slicing import Slice
from .symbolic import HELPERS, call_name


@dataclasses.dataclass
class Rule:
    """A compiled, runtime-checkable form of a slice."""

    tool: str
    call_index: int
    call_node: ast.Call
    arg_exprs: list[ast.expr]          # operand i must equal arg_exprs[i]
    guard: list[ast.expr]              # every predicate must hold (conjunction)
    lets: dict[str, ast.expr]          # let-bindings used by the expressions
    cross_service_deps: list[str]      # tools producing required envelopes
    # Bounded for: quantified rule. When set, ``loop_var`` is bound to each element
    # of ``loop_iter`` (a gateway-observed collection) and the operand is authorized
    # iff it matches ``arg_exprs`` for SOME element.
    loop_var: str | None = None
    loop_iter: ast.expr | None = None

    @property
    def key(self) -> str:
        return f"{self.tool}#{self.call_index}"

    @property
    def n_args(self) -> int:
        return len(self.arg_exprs)


def _flatten_and(expr: ast.expr) -> list[ast.expr]:
    """Split a top-level ``and`` chain into individual predicates."""
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        out: list[ast.expr] = []
        for value in expr.values:
            out.extend(_flatten_and(value))
        return out
    return [expr]


def _referenced_tools(slice_: Slice) -> set[str]:
    """Tool names whose envelopes the slice's expressions depend on."""
    tools: set[str] = set()
    roots = list(slice_.arg_exprs) + list(slice_.guards) + list(slice_.lets.values())
    for root in roots:
        for node in ast.walk(root):
            if isinstance(node, ast.Call):
                name = call_name(node)
                if name is not None and name not in HELPERS:
                    tools.add(name)
    return tools


def compile_rule(slice_: Slice, tool_service: dict[str, str] | None = None) -> Rule:
    """Algorithm 1: compile a single slice into a rule."""
    guard: list[ast.expr] = []
    for cond in slice_.guards:
        guard.extend(_flatten_and(cond))

    deps = sorted(_referenced_tools(slice_) - {slice_.tool})
    if tool_service:
        own = tool_service.get(slice_.tool)
        deps = [t for t in deps if tool_service.get(t) != own]

    return Rule(
        tool=slice_.tool,
        call_index=slice_.call_index,
        call_node=slice_.call_node,
        arg_exprs=list(slice_.arg_exprs),
        guard=guard,
        lets=dict(slice_.lets),
        cross_service_deps=deps,
        loop_var=slice_.loop_var,
        loop_iter=slice_.loop_iter,
    )


def compile_rules(
    slices: list[Slice], tool_service: dict[str, str] | None = None
) -> list[Rule]:
    """Compile every slice of a task into the enforcer's rule set."""
    return [compile_rule(s, tool_service) for s in slices]
