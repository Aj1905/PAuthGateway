"""NL-slice derivation (paper sec. 3.3 and step A2 of sec. 4.1.2).

Given the imperative ``run`` function produced by A1, we derive -- for *every*
tool invocation -- a slice: the tool name, a symbolic expression for each
operand, and the path conditions required to reach the call.  Slicing is the
deterministic core of PAuth: "The derivation of slices/rules (Steps A2 and A3)
is deterministic without LLM" (paper sec. 5.2).

A slice keeps only the dependency closure of its target call: the ``let``
bindings whose values flow into the operands or guards, plus the ``assert``
conditions that guard the call and guard every ``let`` it depends on.
"""

from __future__ import annotations

import ast
import dataclasses

from .symbolic import call_name, canon, names_in


@dataclasses.dataclass
class Slice:
    """A symbolic specification of one expected tool call."""

    tool: str
    call_index: int               # 0-based occurrence index of this tool
    call_node: ast.Call           # the invocation in the generated code
    arg_exprs: list[ast.expr]     # positional operand expressions
    guards: list[ast.expr]        # path conditions (asserts), unsplit
    lets: dict[str, ast.expr]     # let-bindings referenced by the closure
    # Bounded for: the call runs once per element of ``loop_iter``, with ``loop_var``
    # bound to the element. The rule is quantified -- an operand is authorized iff
    # it matches the expression for SOME element of the (gateway-observed) collection.
    loop_var: str | None = None
    loop_iter: ast.expr | None = None

    @property
    def key(self) -> str:
        """Stable identifier ``tool#index``."""
        return f"{self.tool}#{self.call_index}"

    def render(self) -> str:
        """Render the slice in the style of paper Figure 7."""
        lines = [f"(* Slice for {self.tool} *)"]
        for name, expr in self.lets.items():
            lines.append(f"let {name} = {ast.unparse(expr)}")
        for guard in self.guards:
            lines.append(f"assert {ast.unparse(guard)}")
        lines.append(ast.unparse(self.call_node))
        return "\n".join(lines)


def _statements_with_guards(
    func: ast.FunctionDef,
) -> list[tuple[ast.stmt, list[ast.expr]]]:
    """Flatten the body into (statement, path-conditions) pairs.

    A statement's path condition is empty (top level), the enclosing ``if`` test
    (if-body), or its negation (else-body). Nesting stays forbidden, so a guard is
    at most one predicate; else-body statements carry ``not C``.
    """
    out: list[tuple[ast.stmt, list[ast.expr], tuple[str | None, ast.expr] | None]] = []
    for stmt in func.body:
        if isinstance(stmt, ast.If):
            for inner in stmt.body:
                out.append((inner, [stmt.test], None))
            if stmt.orelse:
                neg = ast.copy_location(ast.UnaryOp(op=ast.Not(), operand=stmt.test), stmt.test)
                for inner in stmt.orelse:
                    out.append((inner, [neg], None))
        elif isinstance(stmt, ast.For):
            loop_var = stmt.target.id if isinstance(stmt.target, ast.Name) else None
            for inner in stmt.body:
                out.append((inner, [], (loop_var, stmt.iter)))
        else:
            out.append((stmt, [], None))
    return out


def derive_slices(func: ast.FunctionDef, tool_names: set[str]) -> list[Slice]:
    """Derive slices per tool invocation in ``func``.

    A variable normally has one definition. The one supported disjunction is the
    "default then conditionally set" merge (``x = <const>``; ``if C: x = <expr>``):
    such a variable holds a LIST of definitions, and a call using it forks into one
    slice per branch (each with that branch's provenance + guard). The enforcer's
    match-any-rule already turns those into a sound disjunction: a concrete call is
    authorized iff it matches ONE branch exactly, so an off-branch (injected) value
    matches none -- FN=0 is preserved.
    """
    stmts = _statements_with_guards(func)

    # Record every assignment: name -> [(value expression, guard, order), ...].
    assigns: dict[str, list[tuple[ast.expr, list[ast.expr], int]]] = {}
    for order, (stmt, guard, _loop) in enumerate(stmts):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    assigns.setdefault(target.id, []).append((stmt.value, guard, order))

    slices: list[Slice] = []
    counts: dict[str, int] = {}
    for stmt, guard, loop in stmts:
        call = _tool_call_of(stmt, tool_names)
        if call is None:
            continue
        tool = call_name(call)
        idx = counts.get(tool, 0)
        counts[tool] = idx + 1
        slices.extend(_build_slices(call, tool, idx, guard, assigns, loop))
    return slices


def _tool_call_of(stmt: ast.stmt, tool_names: set[str]) -> ast.Call | None:
    """Return the tool-call expression of a statement, if it is one."""
    node: ast.expr | None = None
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        node = stmt.value
    elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
        node = stmt.value
    if isinstance(node, ast.Call) and call_name(node) in tool_names:
        return node
    return None


def _build_slices(
    call: ast.Call,
    tool: str,
    idx: int,
    guard: list[ast.expr],
    assigns: dict[str, list[tuple[ast.expr, list[ast.expr], int]]],
    loop: tuple[str | None, ast.expr] | None = None,
) -> list[Slice]:
    """Dependency-closure the call, forking on any disjunctive variable.

    A partial resolution is ``(lets, guard_nodes, frontier)``. Resolving a name
    with N definitions forks the partial into N (one per branch). With single
    definitions this collapses to exactly the old single-slice behaviour.
    """
    arg_exprs = list(call.args)
    loop_var = loop[0] if loop else None
    loop_iter = loop[1] if loop else None

    def _init():
        gnodes: dict[str, tuple[ast.expr, int]] = {}
        frontier: list[str] = []
        for cond in guard:
            k = canon(cond)
            if k not in gnodes:
                gnodes[k] = (cond, getattr(cond, "lineno", 0))
                frontier.extend(names_in(cond))
        for expr in arg_exprs:
            frontier.extend(names_in(expr))
        if loop_iter is not None:  # resolve the collection var into the closure
            frontier.extend(names_in(loop_iter))
        # loop_var is bound per-element at enforcement time, not resolved here.
        return ({}, gnodes, [n for n in frontier if n != loop_var])

    # Worklist of partial resolutions; forks on disjunctive names.
    partials: list[tuple[dict, dict, list[str]]] = [_init()]
    complete: list[tuple[dict, dict]] = []
    guard_ct = 0
    while partials:
        lets, gnodes, frontier = partials.pop()
        if not frontier:
            complete.append((lets, gnodes))
            continue
        name = frontier.pop()
        if name in lets or name not in assigns:
            partials.append((lets, gnodes, frontier))
            continue
        for value, value_guard, order in assigns[name]:
            nl = dict(lets)
            nl[name] = (value, order)
            ng = dict(gnodes)
            nf = list(frontier) + list(names_in(value))
            for cond in value_guard:
                k = canon(cond)
                if k not in ng:
                    ng[k] = (cond, getattr(cond, "lineno", 0))
                    nf.extend(names_in(cond))
            partials.append((nl, ng, nf))
        guard_ct += 1
        if guard_ct > 10_000:  # runaway guard (should never trigger on bounded code)
            break

    out: list[Slice] = []
    for lets, gnodes in complete:
        ordered_lets = {
            n: v for n, (v, _o) in sorted(lets.items(), key=lambda kv: kv[1][1])
        }
        ordered_guards = [c for c, _l in sorted(gnodes.values(), key=lambda cl: cl[1])]
        out.append(Slice(
            tool=tool, call_index=idx, call_node=call,
            arg_exprs=arg_exprs, guards=ordered_guards, lets=ordered_lets,
            loop_var=loop_var, loop_iter=loop_iter,
        ))
    return out
