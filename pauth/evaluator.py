"""Deterministic evaluator for symbolic slice expressions.

The enforcer (paper sec. 4.1.3) checks a concrete call by *re-computing* the
operand value implied by the slice and comparing it against what the agent
actually passed.  This module evaluates a slice expression (an ``ast`` node)
over the envelope store, the slice's ``let`` bindings, and any lambda-bound
names from helper predicates.

Crucially, the evaluator never trusts the agent: a tool sub-expression is
resolved by looking up a *signed* envelope, not by re-running the tool.
"""

from __future__ import annotations

import ast
import math
from typing import Any

from .envelope import EnvelopeStore, _to_jsonable
from .symbolic import HELPERS, canon


class NotConcretizable(Exception):
    """Raised when a symbolic expression cannot be resolved.

    This happens when an operand depends on a tool result that is absent from
    the envelope store -- i.e. the call does not satisfy the implicit data
    dependency constraints (paper sec. 4.1.3) -- and means the call must be
    denied.
    """


class AttrDict(dict):
    """A dict that also supports attribute access (``d.field``)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # noqa: BLE001
            raise AttributeError(name) from exc


def wrap(value: Any) -> Any:
    """Wrap structured outputs so field access is uniform.

    Tools may return plain dicts; generated code accesses fields with dot
    notation (Appendix A rule 8).  We expose dicts as :class:`AttrDict` and
    recurse through lists, leaving pydantic models / scalars untouched.
    """
    if isinstance(value, AttrDict):
        return value
    if isinstance(value, dict):
        return AttrDict({k: wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [wrap(v) for v in value]
    if isinstance(value, tuple):
        return tuple(wrap(v) for v in value)
    return value


def field_get(obj: Any, name: str) -> Any:
    """Access ``obj.name`` for pydantic models, dicts and plain objects."""
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        raise NotConcretizable(f"field '{name}' absent")
    try:
        return getattr(obj, name)
    except AttributeError as exc:  # noqa: BLE001
        raise NotConcretizable(f"field '{name}' absent on {type(obj).__name__}") from exc


# --------------------------------------------------------------------------
# Helper functions (paper sec. 4.1.1: len, min, max, first, last)
# --------------------------------------------------------------------------

def helper_len(iterable: Any) -> int:
    return len(iterable)


def helper_min(iterable: Any, key: Any) -> Any:
    items = list(iterable)
    if not items:
        raise NotConcretizable("min() of empty sequence")
    return min(items, key=key)


def helper_max(iterable: Any, key: Any) -> Any:
    items = list(iterable)
    if not items:
        raise NotConcretizable("max() of empty sequence")
    return max(items, key=key)


def helper_first(iterable: Any, predicate: Any) -> Any:
    for item in iterable:
        if predicate(item):
            return item
    return None


def helper_last(iterable: Any, predicate: Any) -> Any:
    match = None
    for item in iterable:
        if predicate(item):
            match = item
    return match


# Execution-time helper table (used by the sandboxed runner in enforcer.py).
EXEC_HELPERS: dict[str, Any] = {
    "len": helper_len,
    "min": lambda it, key: helper_min(it, key),
    "max": lambda it, key: helper_max(it, key),
    "first": lambda it, predicate: helper_first(it, predicate),
    "last": lambda it, predicate: helper_last(it, predicate),
}


_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}


class Evaluator:
    """Evaluates a symbolic slice expression to a concrete value."""

    def __init__(
        self,
        store: EnvelopeStore,
        lets: dict[str, ast.expr],
        names: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.lets = lets
        self.names = names or {}
        self._let_cache: dict[str, Any] = {}

    # -- public ----------------------------------------------------------
    def eval(self, node: ast.expr) -> Any:
        method = getattr(self, f"_e_{type(node).__name__}", None)
        if method is None:
            raise NotConcretizable(f"cannot evaluate {type(node).__name__}")
        return method(node)

    # -- leaves ----------------------------------------------------------
    def _e_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def _e_Name(self, node: ast.Name) -> Any:
        if node.id in self.names:
            return self.names[node.id]
        if node.id in self.lets:
            if node.id not in self._let_cache:
                self._let_cache[node.id] = self.eval(self.lets[node.id])
            return self._let_cache[node.id]
        raise NotConcretizable(f"unbound name '{node.id}'")

    def _e_List(self, node: ast.List) -> Any:
        return [self.eval(e) for e in node.elts]

    # -- compound --------------------------------------------------------
    def _e_BinOp(self, node: ast.BinOp) -> Any:
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise NotConcretizable(f"unsupported operator {type(node.op).__name__}")
        try:
            return op(self.eval(node.left), self.eval(node.right))
        except ZeroDivisionError as exc:  # noqa: BLE001
            raise NotConcretizable("division by zero") from exc
        except TypeError as exc:  # incomparable / incompatible operand types
            raise NotConcretizable(f"type error in arithmetic: {exc}") from exc

    def _e_UnaryOp(self, node: ast.UnaryOp) -> Any:
        val = self.eval(node.operand)
        try:
            if isinstance(node.op, ast.USub):
                return -val
            if isinstance(node.op, ast.UAdd):
                return +val
        except TypeError as exc:
            raise NotConcretizable(f"type error in unary op: {exc}") from exc
        raise NotConcretizable("unsupported unary operator")

    def _e_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = self.eval(v)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = self.eval(v)
            if result:
                return result
        return result

    def _e_Compare(self, node: ast.Compare) -> bool:
        left = self.eval(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.eval(comparator)
            try:
                ok = _compare(op, left, right)
            except TypeError as exc:  # e.g. str vs int ordering comparison
                raise NotConcretizable(f"incomparable types: {exc}") from exc
            if not ok:
                return False
            left = right
        return True

    def _e_Attribute(self, node: ast.Attribute) -> Any:
        return field_get(self.eval(node.value), node.attr)

    def _e_Subscript(self, node: ast.Subscript) -> Any:
        container = self.eval(node.value)
        index = self.eval(node.slice)
        try:
            return container[index]
        except (IndexError, KeyError, TypeError) as exc:  # noqa: BLE001
            raise NotConcretizable(f"bad subscript: {exc}") from exc

    def _e_Call(self, node: ast.Call) -> Any:
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name in HELPERS:
            return self._eval_helper(name, node)
        # A tool call: resolve from a signed envelope -- never re-run the tool.
        key = canon(node)
        if not self.store.has(key):
            raise NotConcretizable(f"no envelope for '{key}'")
        return wrap(self.store.get(key))

    # -- helpers ---------------------------------------------------------
    def _eval_helper(self, name: str, node: ast.Call) -> Any:
        if not node.args:
            raise NotConcretizable(f"{name}() needs an iterable argument")
        iterable = self.eval(node.args[0])
        kw = {k.arg: k.value for k in node.keywords}
        try:
            if name == "len":
                return helper_len(iterable)
            if name in ("min", "max"):
                lam = kw.get("key")
                if not isinstance(lam, ast.Lambda):
                    raise NotConcretizable(f"{name}() requires a key= lambda")
                keyfn = self._make_lambda(lam)
                return (helper_min if name == "min" else helper_max)(iterable, keyfn)
            # first / last
            lam = kw.get("predicate")
            if not isinstance(lam, ast.Lambda):
                raise NotConcretizable(f"{name}() requires a predicate= lambda")
            pred = self._make_lambda(lam)
            return (helper_first if name == "first" else helper_last)(iterable, pred)
        except TypeError as exc:  # incomparable keys, non-iterable argument, ...
            raise NotConcretizable(f"type error in {name}(): {exc}") from exc

    def _make_lambda(self, lam: ast.Lambda):
        params = [a.arg for a in lam.args.args]

        def _fn(item: Any) -> Any:
            child_names = dict(self.names)
            if params:
                child_names[params[0]] = item
            child = Evaluator(self.store, self.lets, child_names)
            child._let_cache = self._let_cache  # share resolved lets
            return child.eval(lam.body)

        return _fn


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.Eq):
        return values_match(left, right)
    if isinstance(op, ast.NotEq):
        return not values_match(left, right)
    if isinstance(op, ast.Is):
        if right is None:
            return left is None
        return values_match(left, right)
    if isinstance(op, ast.IsNot):
        if right is None:
            return left is not None
        return not values_match(left, right)
    if isinstance(op, ast.In):
        return left in right
    if isinstance(op, ast.NotIn):
        return left not in right
    raise NotConcretizable(f"unsupported comparison {type(op).__name__}")


def values_match(expected: Any, actual: Any) -> bool:
    """Equality used by the enforcer to compare an operand to its rule.

    Numbers are compared with a small tolerance so that floating-point
    re-computation of, e.g., ``balance / 4`` does not cause a false positive.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual)
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-6)
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, str) and isinstance(actual, str):
        return expected == actual
    return _to_jsonable(expected) == _to_jsonable(actual)
