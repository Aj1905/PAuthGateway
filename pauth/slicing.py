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

    The grammar forbids nested ifs, so a statement's path condition is either
    empty (top level) or the single enclosing ``if`` test.
    """
    out: list[tuple[ast.stmt, list[ast.expr]]] = []
    for stmt in func.body:
        if isinstance(stmt, ast.If):
            for inner in stmt.body:
                out.append((inner, [stmt.test]))
        else:
            out.append((stmt, []))
    return out


def derive_slices(func: ast.FunctionDef, tool_names: set[str]) -> list[Slice]:
    """Derive one :class:`Slice` per tool invocation in ``func``."""
    stmts = _statements_with_guards(func)

    # Record every assignment: name -> (value expression, guard, order).
    assigns: dict[str, tuple[ast.expr, list[ast.expr], int]] = {}
    for order, (stmt, guard) in enumerate(stmts):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    assigns[target.id] = (stmt.value, guard, order)

    slices: list[Slice] = []
    counts: dict[str, int] = {}
    for stmt, guard in stmts:
        call = _tool_call_of(stmt, tool_names)
        if call is None:
            continue
        tool = call_name(call)
        idx = counts.get(tool, 0)
        counts[tool] = idx + 1
        slices.append(_build_slice(call, tool, idx, guard, assigns))
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


def _build_slice(
    call: ast.Call,
    tool: str,
    idx: int,
    guard: list[ast.expr],
    assigns: dict[str, tuple[ast.expr, list[ast.expr], int]],
) -> Slice:
    arg_exprs = list(call.args)

    # Breadth-first dependency closure over let-bindings.
    lets: dict[str, tuple[ast.expr, int]] = {}
    guard_nodes: dict[str, tuple[ast.expr, int]] = {}
    frontier: list[str] = []

    def add_guard(cond: ast.expr) -> None:
        key = canon(cond)
        if key not in guard_nodes:
            guard_nodes[key] = (cond, getattr(cond, "lineno", 0))
            frontier.extend(names_in(cond))

    for cond in guard:
        add_guard(cond)
    for expr in arg_exprs:
        frontier.extend(names_in(expr))

    while frontier:
        name = frontier.pop()
        if name in lets or name not in assigns:
            continue
        value, value_guard, order = assigns[name]
        lets[name] = (value, order)
        frontier.extend(names_in(value))
        for cond in value_guard:
            add_guard(cond)

    ordered_lets = {
        name: value
        for name, (value, _order) in sorted(lets.items(), key=lambda kv: kv[1][1])
    }
    ordered_guards = [
        cond for cond, _line in sorted(guard_nodes.values(), key=lambda cl: cl[1])
    ]
    return Slice(
        tool=tool,
        call_index=idx,
        call_node=call,
        arg_exprs=arg_exprs,
        guards=ordered_guards,
        lets=ordered_lets,
    )
