"""Symbolic-expression utilities shared across the PAuth pipeline.

A *symbolic value* in PAuth (paper sec. 3.3 / 3.4) is the computation that
produces a concrete value, expressed over tool calls, constants and helper
functions.  Internally we keep symbolic expressions as Python ``ast`` nodes
and canonicalise them to a stable string when they are used as envelope-store
keys.
"""

from __future__ import annotations

import ast

# The "standard function" tools the paper adds to AgentDojo (sec. 4.1.1):
# len/min/max/first/last, plus ``sum`` -- a deterministic reduction in the same
# class (a signed collection projected to a scalar, re-derivable by the
# enforcer, so a fabricated total is off-slice and denied). ``sum`` lets a plan
# aggregate extracted values (e.g. total a list of bill amounts) inside the
# grammar instead of handing the arithmetic to an untrusted LLM.
HELPERS = {"len", "min", "max", "first", "last", "sum"}


def canon(node: ast.AST) -> str:
    """Return the canonical string form of a symbolic expression.

    This is the stable key used to index the envelope store (paper sec. 3.4,
    "The envelope store is implemented as a dictionary indexed by the symbolic
    value").  ``ast.unparse`` gives a deterministic normalisation, so the same
    sub-expression always produces the same key whether it appears in a slice
    or in a rule's recorded call.
    """
    return ast.unparse(node)


def call_name(node: ast.AST) -> str | None:
    """Name of the called function for a ``name(...)`` or ``a.b(...)`` call."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def names_in(node: ast.AST) -> set[str]:
    """All free identifier names referenced (loaded) by an expression.

    Lambda-bound parameters are excluded, since they are not data dependencies
    of the slice -- they are local to a helper predicate.
    """
    found: set[str] = set()
    bound: set[str] = set()

    class _V(ast.NodeVisitor):
        def _scoped(self, n: ast.AST, params: set[str]) -> None:
            added = params - bound
            bound.update(added)
            self.generic_visit(n)
            bound.difference_update(added)

        def visit_Lambda(self, n: ast.Lambda) -> None:
            self._scoped(n, {a.arg for a in n.args.args})

        def _comp(self, n) -> None:  # a comprehension's targets are locally bound
            targets: set[str] = set()
            for gen in n.generators:
                targets |= {t.id for t in ast.walk(gen.target) if isinstance(t, ast.Name)}
            self._scoped(n, targets)

        visit_ListComp = _comp
        visit_SetComp = _comp
        visit_DictComp = _comp
        visit_GeneratorExp = _comp

        def visit_Name(self, n: ast.Name) -> None:
            if isinstance(n.ctx, ast.Load) and n.id not in bound:
                found.add(n.id)

    _V().visit(node)
    return found
