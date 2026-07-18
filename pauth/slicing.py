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
    # Bounded for(s): the call runs once per element of an enclosing loop. For
    # NESTED loops, ``loops`` lists them outer->inner as ``(var, iter_expr)``; the
    # rule is quantified over the NESTED enumeration (each inner ``iter`` evaluated
    # with the outer vars bound), so the authorized set is exactly the tuples the
    # loops can produce -- a value from no reachable tuple is off-slice (FN=0).
    loops: list = dataclasses.field(default_factory=list)

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
) -> list[tuple[ast.stmt, list[ast.expr], tuple[str | None, ast.expr] | None]]:
    """Flatten the body into (leaf-statement, path-conditions, loop) triples.

    Nested if/else is supported: a statement's path condition is the CONJUNCTION
    of every enclosing ``if`` test, each else-branch contributing ``not C``. The
    enforcer already requires ALL guards to hold (``all(...)``), so a leaf under
    ``if C1: if C2:`` compiles to a rule requiring ``C1 and C2`` -- authorized
    only on its exact path, off-path (injected) values matching no rule (FN=0).
    ``for`` stays top-level (grammar-enforced) and never nests with an ``if``.
    """
    out: list[tuple[ast.stmt, list[ast.expr], list]] = []

    def walk(stmts: list[ast.stmt], guards: list[ast.expr], loops: list) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                walk(stmt.body, guards + [stmt.test], loops)
                if stmt.orelse:
                    neg = ast.copy_location(
                        ast.UnaryOp(op=ast.Not(), operand=stmt.test), stmt.test
                    )
                    walk(stmt.orelse, guards + [neg], loops)
            elif isinstance(stmt, ast.For):
                loop_var = stmt.target.id if isinstance(stmt.target, ast.Name) else None
                walk(stmt.body, guards, loops + [(loop_var, stmt.iter)])
            else:
                out.append((stmt, list(guards), list(loops)))

    walk(func.body, [], [])
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
    for order, (stmt, guard, _loops) in enumerate(stmts):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    assigns.setdefault(target.id, []).append((stmt.value, guard, order))

    slices: list[Slice] = []
    counts: dict[str, int] = {}
    for stmt, guard, loops in stmts:
        call = _tool_call_of(stmt, tool_names)
        if call is None:
            continue
        tool = call_name(call)
        idx = counts.get(tool, 0)
        counts[tool] = idx + 1
        slices.extend(_build_slices(call, tool, idx, guard, assigns, loops))
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
    loops: list | None = None,
) -> list[Slice]:
    """Dependency-closure the call, forking on any disjunctive variable.

    A partial resolution is ``(lets, guard_nodes, frontier)``. Resolving a name
    with N definitions forks the partial into N (one per branch). With single
    definitions this collapses to exactly the old single-slice behaviour.
    """
    arg_exprs = list(call.args)
    loops = loops or []
    loop_vars = {v for v, _ in loops if v}  # bound per-element at enforcement time

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
        for _v, it in loops:  # resolve each collection var into the closure
            frontier.extend(names_in(it))
        # loop vars are bound per-element at enforcement time, not resolved here.
        return ({}, gnodes, [n for n in frontier if n not in loop_vars])

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
            loops=list(loops),
        ))
    return out
