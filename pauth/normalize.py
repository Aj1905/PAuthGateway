"""Semantics-preserving normalization applied before slicing (Tier-1).

The DSL rejects several constructs that are not *unsafe*, merely
outside the slicer's canonical form. Rejecting them lowers the Planner's acceptance rate
without buying any security. This module rewrites such code into the canonical
form the slicer already understands, **without changing what the code does** --
so the deterministic core (the Slicer slices / the Rule compiler rules / runtime enforcement enforcement) is untouched
and the FP/FN guarantee is unaffected. Two transforms, both run after
:func:`strip_dead_code` and before :func:`validate_semantics`:

* ``_hoist_call_args`` -- lift a call used as an *argument* of another call into
  a preceding temporary (``x = first(get_emails())`` ->
  ``_h0 = get_emails(); x = first(_h0)``). This satisfies the "helper's first
  argument is a bare identifier" and "no tool call nested in an expression" rules
  by construction. Lambdas are never descended into (a ``key=`` body may depend on
  the lambda parameter), and only call-argument positions are hoisted -- never a
  short-circuit position (``a() and b()``) -- so evaluation order is preserved.

* ``_ssa_rename`` -- give each reassigned variable a single static definition
  (``content = a; content = content + b`` -> ``content = a; content_1 = ... ``).
  This is a bijective use-def renaming: identical semantics, and it restores the
  "one definition per name" invariant the slicer assumes. It is applied only when
  every reassignment is straight-line; if any reassigned name is defined inside a
  conditional (which would need a merge/phi), the pass bails and leaves the code
  unchanged so behavior is exactly as before (still rejected at the Planner -- no
  regression, no unsound slice).
"""

from __future__ import annotations

import ast

from .symbolic import HELPERS, call_name


def normalize_run(func: ast.FunctionDef) -> ast.FunctionDef:
    """Apply the Tier-1 semantics-preserving normalizations in order."""
    _existing = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    func = _hoist_call_args(func, _existing)
    func = _ssa_rename(func)
    return func


# --------------------------------------------------------------------------
# 1. Hoist calls used as arguments of other calls into preceding temporaries.
# --------------------------------------------------------------------------

class _ArgCallHoister(ast.NodeTransformer):
    """Replace every ``Call`` reached (post-order) with a fresh temp, recording
    ``<temp> = <call>`` in ``sink``. Applied to the *arguments* of a root call,
    never the root call itself, and never inside a lambda."""

    def __init__(self, sink: list[ast.Assign], existing: set[str], counter: list[int]) -> None:
        self.sink, self.existing, self.counter = sink, existing, counter

    def _tmp(self) -> str:
        while True:
            name = f"_h{self.counter[0]}"
            self.counter[0] += 1
            if name not in self.existing:
                self.existing.add(name)
                return name

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:  # noqa: N802
        return node  # a key=/predicate= body may reference the lambda param; leave it

    def visit_Call(self, node: ast.Call) -> ast.AST:  # noqa: N802
        self.generic_visit(node)  # hoist inner (nested) calls first -> correct order
        return self.force(node)

    def force(self, node: ast.expr) -> ast.Name:
        """Lift ``node`` into a fresh ``<temp> = node`` and return the temp name."""
        tmp = self._tmp()
        self.sink.append(
            ast.Assign(targets=[ast.Name(id=tmp, ctx=ast.Store())], value=node)
        )
        return ast.Name(id=tmp, ctx=ast.Load())


def _root_call(stmt: ast.stmt) -> ast.Call | None:
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return stmt.value
    if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
        return stmt.value
    return None


def _hoist_body(body: list[ast.stmt], existing: set[str], counter: list[int]) -> list[ast.stmt]:
    out: list[ast.stmt] = []
    for stmt in body:
        if isinstance(stmt, ast.If):
            stmt.body = _hoist_body(stmt.body, existing, counter)
        root = _root_call(stmt)
        if root is not None:
            sink: list[ast.Assign] = []
            hoister = _ArgCallHoister(sink, existing, counter)
            root.args = [hoister.visit(a) for a in root.args]
            for kw in root.keywords:
                kw.value = hoister.visit(kw.value)
            # A helper's first argument must be a bare identifier (<HelperCall>).
            # The LLM often wraps a scalar as a list literal -- max([x], key=...) --
            # or indexes a collection. Lifting that expression to a temp yields the
            # canonical ``c = <collection>; max(c, key=...)`` form the slicer wants.
            if (call_name(root) in HELPERS and root.args
                    and not isinstance(root.args[0], ast.Name)):
                root.args[0] = hoister.force(root.args[0])
            out.extend(sink)
        out.append(stmt)
    return out


def _hoist_call_args(func: ast.FunctionDef, existing: set[str]) -> ast.FunctionDef:
    counter = [0]
    func.body = _hoist_body(func.body, existing, counter)
    ast.fix_missing_locations(func)
    return func


# --------------------------------------------------------------------------
# 2. SSA-rename straight-line reassigned variables (bail on conditional defs).
# --------------------------------------------------------------------------

def _assign_counts(func: ast.FunctionDef) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    counts[t.id] = counts.get(t.id, 0) + 1
    return counts


def _ssa_rename(func: ast.FunctionDef) -> ast.FunctionDef:
    counts = _assign_counts(func)
    reassigned = {name for name, c in counts.items() if c > 1}
    if not reassigned:
        return func

    # A reassignment inside an if-body is a conditional definition: soundly
    # renaming it needs a merge (phi) at the join. Out of Tier-1 scope -> bail,
    # leaving the code unchanged (still rejected downstream; never mis-sliced).
    cond_assigned: set[str] = set()
    for stmt in func.body:
        if isinstance(stmt, ast.If):
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Assign):
                    for t in inner.targets:
                        if isinstance(t, ast.Name):
                            cond_assigned.add(t.id)
    if reassigned & cond_assigned:
        return func

    all_names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    cur: dict[str, str] = {}   # original base name -> id currently in scope
    ver: dict[str, int] = {}   # original base name -> definitions seen so far

    def remap_loads(node: ast.AST) -> None:
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in cur:
                n.id = cur[n.id]

    def fresh(base: str) -> str:
        ver[base] += 1
        name = f"{base}_{ver[base]}"
        while name in all_names:
            ver[base] += 1
            name = f"{base}_{ver[base]}"
        all_names.add(name)
        return name

    def process(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                remap_loads(stmt.value)  # RHS binds the *current* versions first
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id in reassigned:
                        base = t.id
                        if base not in ver:
                            ver[base] = 0
                            cur[base] = base           # first def keeps the name
                        else:
                            cur[base] = t.id = fresh(base)
            elif isinstance(stmt, ast.If):
                remap_loads(stmt.test)
                process(stmt.body)  # only Load-remap inside (no reassigned defs here)
            else:
                remap_loads(stmt)

    process(func.body)
    ast.fix_missing_locations(func)
    return func
