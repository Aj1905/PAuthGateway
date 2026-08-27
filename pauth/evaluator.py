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
import dataclasses
import math
from typing import Any

from .envelope import EnvelopeStore, _to_jsonable, occurrence_symbolic
from .symbolic import HELPERS, canon, source_site


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


def helper_sum(iterable: Any, key: Any = None) -> Any:
    """Deterministic reduction: sum ``key(item)`` (or ``item``) over the
    iterable, starting from 0. A pure function of the (signed) collection, so
    the enforcer re-derives it exactly -- a fabricated total is off-slice.

    ``key`` is optional (unlike min/max): a list of already-scalar values
    (e.g. a structured view's ``amounts``) sums directly with no projection.
    """
    total: Any = 0
    for item in iterable:
        total = total + (key(item) if key is not None else item)
    return total


# Execution-time helper table (used by the sandboxed plan executor in enforcer.py).
EXEC_HELPERS: dict[str, Any] = {
    "len": helper_len,
    "min": lambda it, key: helper_min(it, key),
    "max": lambda it, key: helper_max(it, key),
    "first": lambda it, predicate: helper_first(it, predicate),
    "last": lambda it, predicate: helper_last(it, predicate),
    "sum": lambda it, key=None: helper_sum(it, key),
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
        occurrence_sites: dict[tuple[int, int, int, int], str] | None = None,
        occurrence_path: tuple[int, ...] = (),
    ) -> None:
        self.store = store
        self.lets = lets
        self.names = names or {}
        self.occurrence_sites = occurrence_sites or {}
        self.occurrence_path = occurrence_path
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

    def _e_ListComp(self, node: ast.ListComp) -> Any:
        # pure map/filter over a signed collection, re-derived deterministically.
        if len(node.generators) != 1:
            raise NotConcretizable("only single-generator comprehensions")
        gen = node.generators[0]
        if not isinstance(gen.target, ast.Name):
            raise NotConcretizable("comprehension target must be a name")
        var = gen.target.id
        collection = self.eval(gen.iter)
        if not isinstance(collection, (list, tuple)):
            raise NotConcretizable("comprehension iterable is not a collection")
        out = []
        for element in collection:
            child = Evaluator(
                self.store,
                self.lets,
                {**self.names, var: element},
                self.occurrence_sites,
                self.occurrence_path + (len(out),),
            )
            child._let_cache = self._let_cache
            if all(bool(child.eval(cond)) for cond in gen.ifs):
                out.append(child.eval(node.elt))
        return out

    def _e_Dict(self, node: ast.Dict) -> Any:
        # deterministic construction from traced keys/values; ** unpack (key None)
        # is not re-derivable operand-by-operand -> reject (default-deny).
        if any(k is None for k in node.keys):
            raise NotConcretizable("dict ** unpacking is not supported")
        return {self.eval(k): self.eval(v) for k, v in zip(node.keys, node.values)}

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
        # ``not`` is not in the Planner DSL, but the slicer SYNTHESISES it for the
        # else-branch guard of an if/else merge (guard = not C). Support it here.
        if isinstance(node.op, ast.Not):
            return not self.eval(node.operand)
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
        # A supported helper-lambda occurrence has no source-level fallback.
        # The exact rule and traversal path are part of its provenance; using
        # concrete operands would collide for duplicates, zero-operand tools,
        # and separate source sites.
        rule_key = self.occurrence_sites.get(source_site(node))
        if rule_key is not None:
            key = occurrence_symbolic(rule_key, self.occurrence_path)
            if not self.store.has(key):
                raise NotConcretizable(f"no envelope for occurrence '{key}'")
            return wrap(self.store.get(key))
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
                items = list(iterable)
                if not items:
                    raise NotConcretizable(f"{name}() of empty sequence")
                keyed = [
                    (self._eval_lambda(lam, item, index), item)
                    for index, item in enumerate(items)
                ]
                chooser = min if name == "min" else max
                return chooser(keyed, key=lambda pair: pair[0])[1]
            if name == "sum":
                lam = kw.get("key")  # optional projection
                if not isinstance(lam, ast.Lambda):
                    return helper_sum(iterable)
                total: Any = 0
                for index, item in enumerate(iterable):
                    total += self._eval_lambda(lam, item, index)
                return total
            # first / last
            lam = kw.get("predicate")
            if not isinstance(lam, ast.Lambda):
                raise NotConcretizable(f"{name}() requires a predicate= lambda")
            match = None
            for index, item in enumerate(iterable):
                if self._eval_lambda(lam, item, index):
                    if name == "first":
                        return item
                    match = item
            return match
        except TypeError as exc:  # incomparable keys, non-iterable argument, ...
            raise NotConcretizable(f"type error in {name}(): {exc}") from exc

    def _eval_lambda(self, lam: ast.Lambda, item: Any, index: int) -> Any:
        params = [a.arg for a in lam.args.args]
        child_names = dict(self.names)
        if params:
            child_names[params[0]] = item
        child = Evaluator(
            self.store,
            self.lets,
            child_names,
            self.occurrence_sites,
            self.occurrence_path + (index,),
        )
        child._let_cache = self._let_cache
        return child.eval(lam.body)

    def _make_lambda(self, lam: ast.Lambda):
        """Return the legacy callable used by confirmation-breakdown views.

        Executable DSL code cannot place tool calls inside helper lambdas, so
        callers of this compatibility helper only need pure expression
        evaluation.  Keep a traversal index nevertheless so occurrence-aware
        evaluation remains deterministic if the executable profile is widened
        later.
        """

        index = 0

        def _fn(item: Any) -> Any:
            nonlocal index
            value = self._eval_lambda(lam, item, index)
            index += 1
            return value

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
        if isinstance(right, dict):
            return any(values_match(left, key) for key in right)
        if isinstance(right, (list, tuple)):
            return any(values_match(left, item) for item in right)
        return left in right
    if isinstance(op, ast.NotIn):
        if isinstance(right, dict):
            return not any(values_match(left, key) for key in right)
        if isinstance(right, (list, tuple)):
            return not any(values_match(left, item) for item in right)
        return left not in right
    raise NotConcretizable(f"unsupported comparison {type(op).__name__}")


def values_match(expected: Any, actual: Any) -> bool:
    """Equality used by the enforcer to compare an operand to its rule.

    Numbers compare exactly except for a few adjacent IEEE-754 representation
    steps between two floats. Booleans and nested containers remain type-exact.
    """
    # ``bool`` is a subclass of ``int`` in Python.  Authorization must not
    # inherit Python's ``True == 1`` or truthiness coercion: approving a boolean
    # operand must authorize that exact boolean, not any non-zero value.
    if isinstance(expected, bool) or isinstance(actual, bool):
        return (
            isinstance(expected, bool)
            and isinstance(actual, bool)
            and expected is actual
        )
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if expected == actual:
            return True
        # A relative tolerance of 1e-9 authorizes a whole dollar of drift at
        # 1e9, which is not an exact operand check.  Permit only a few floating
        # point representation steps.  Integer/float pairs that are not exactly
        # equal remain different.
        if not isinstance(expected, float) or not isinstance(actual, float):
            return False
        if not math.isfinite(expected) or not math.isfinite(actual):
            return False
        tolerance = 4 * max(math.ulp(expected), math.ulp(actual))
        return abs(expected - actual) <= tolerance
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, str) and isinstance(actual, str):
        return expected == actual

    # Project model/dataclass values before recursively comparing structured
    # results.  Recursion is necessary because Python container equality would
    # otherwise re-introduce ``True == 1`` inside lists and dictionaries.
    if hasattr(expected, "model_dump"):
        try:
            expected = expected.model_dump()
        except Exception:  # noqa: BLE001 -- fall through to opaque comparison
            pass
    if hasattr(actual, "model_dump"):
        try:
            actual = actual.model_dump()
        except Exception:  # noqa: BLE001 -- fall through to opaque comparison
            pass
    if dataclasses.is_dataclass(expected) and not isinstance(expected, type):
        try:
            expected = dataclasses.asdict(expected)
        except Exception:  # noqa: BLE001 -- fall through to opaque comparison
            pass
    if dataclasses.is_dataclass(actual) and not isinstance(actual, type):
        try:
            actual = dataclasses.asdict(actual)
        except Exception:  # noqa: BLE001 -- fall through to opaque comparison
            pass

    if isinstance(expected, (list, tuple)) or isinstance(actual, (list, tuple)):
        if not isinstance(expected, (list, tuple)) or not isinstance(actual, (list, tuple)):
            return False
        return len(expected) == len(actual) and all(
            values_match(e, a) for e, a in zip(expected, actual)
        )
    if isinstance(expected, dict) or isinstance(actual, dict):
        if not isinstance(expected, dict) or not isinstance(actual, dict):
            return False
        if len(expected) != len(actual):
            return False
        actual_items = list(actual.items())
        used: set[int] = set()
        for expected_key, expected_value in expected.items():
            match = next(
                (
                    index
                    for index, (actual_key, _actual_value) in enumerate(actual_items)
                    if index not in used
                    and type(expected_key) is type(actual_key)
                    and values_match(expected_key, actual_key)
                ),
                None,
            )
            if match is None or not values_match(expected_value, actual_items[match][1]):
                return False
            used.add(match)
        return True

    # Opaque SDK values are comparable only within the exact same concrete
    # type.  The JSONable projection retains the existing stable fallback while
    # the type check prevents equal reprs from unrelated classes from matching.
    return type(expected) is type(actual) and _to_jsonable(expected) == _to_jsonable(actual)
