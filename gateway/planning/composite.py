"""Composite plans: sequential stages + bounded fan-out.

A composite plan decomposes one user task into a sequence of stages. Each
stage's code is ordinary Appendix-A restricted code, validated and compiled
by the *unmodified* PAuth core (``pauth.prepare``). What is new is the thin
composition layer:

* **Guards** -- a stage activates only when its guard (Appendix A
  ``<Condition>`` syntax, no extensions) evaluates true against variable
  bindings the *gateway* recorded from its own signed envelopes. The agent's
  claims play no part.
* **Bounded fan-out** -- a stage may be a loop *template* over a list
  observed in an earlier stage. The gateway instantiates it mechanically:
  the index placeholder is replaced by literal integers and every
  ``list[i].field`` expression is partially evaluated to a constant from the
  signed observation. ``n = min(len(observed), max_instances)``; the LLM is
  never consulted at instantiation time.

Properties the composition layer must uphold (S10; tested in
``tests/test_composite.py``):

1. **Inactivity** -- stage k's rules authorize nothing until its guard has
   evaluated true.
2. **Non-accumulation** -- once a stage is left, its rules never authorize
   again. Additionally, within a stage every rule is one-shot: the flat
   PAuth enforcer re-authorizes an exact replay of an already-executed call,
   which the composite layer refuses.
3. **Template soundness** -- instances differ from the validated template
   only in the literal index and observation-derived constants.
4. **Bounded authority** -- a fan-out stage authorizes at most
   ``max_instances`` instances; overflow is reported, never silently run.

Observation-derived constants in fan-out instances are exempt from the
Q15-e *prompt*-entailment precheck by construction -- their provenance is a
signed envelope, not LLM text. That trust decision mirrors the paper's
treatment of data-derived operands (e.g. ``cart.total``) and is the
dangerous-flow surface Stage 3 studies.
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Any

from pauth.codegen import ToolDoc
from pauth.grammar import RestrictedGrammarError, parse_and_validate

from .prechecks import PrecheckPolicy, precheck_code


class CompositePlanError(Exception):
    """Raised when a composite plan fails structural validation."""


@dataclasses.dataclass(frozen=True)
class FanoutSpec:
    """Mechanical fan-out of a body template over an observed list."""

    list_var: str
    index_var: str = "I"
    max_instances: int = 25


@dataclasses.dataclass(frozen=True)
class StageTemplate:
    """One stage: restricted code plus an optional entry guard / fan-out."""

    code: str
    guard: str | None = None
    fanout: FanoutSpec | None = None


@dataclasses.dataclass(frozen=True)
class CompositePlan:
    suite_name: str
    stages: tuple[StageTemplate, ...]
    reason: str = "composite plan"


@dataclasses.dataclass
class FanoutInstantiation:
    """Concrete code for a fan-out stage plus truncation accounting."""

    code: str
    n_instances: int
    truncated: int  # observed elements beyond max_instances (0 = none)


_ALLOWED_CMP = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


# ---------------------------------------------------------------------------
# Guard language: Appendix A <Condition>, nothing more.
# ---------------------------------------------------------------------------

def _guard_expr(source: str) -> ast.expr:
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        raise CompositePlanError(f"guard does not parse: {exc}") from exc
    return tree.body


def _validate_guard_node(node: ast.expr) -> None:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        for value in node.values:
            _validate_guard_node(value)
        return
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or not isinstance(node.ops[0], _ALLOWED_CMP):
            raise CompositePlanError("guard comparisons must use a single relational operator")
        _validate_guard_operand(node.left)
        _validate_guard_operand(node.comparators[0])
        return
    raise CompositePlanError(
        "guard must be relational comparisons combined with and/or (Appendix A <Condition>)"
    )


def _validate_guard_operand(node: ast.expr) -> None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (bool, int, float, str)) or node.value is None:
            return
        raise CompositePlanError(f"guard constant {node.value!r} is not a scalar")
    if isinstance(node, ast.Name):
        return
    if isinstance(node, ast.Attribute):
        _validate_guard_operand(node.value)
        return
    if isinstance(node, ast.Subscript):
        if not (isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int)):
            raise CompositePlanError("guard subscripts must use a literal integer index")
        _validate_guard_operand(node.value)
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        if len(node.args) == 1 and isinstance(node.args[0], ast.Name) and not node.keywords:
            return
        raise CompositePlanError("guard len() takes exactly one bare variable")
    raise CompositePlanError(
        f"guard operand {ast.dump(node)} is outside the <Condition> subset"
    )


def guard_variables(source: str) -> set[str]:
    """Root variable names a guard reads."""
    expr = _guard_expr(source)
    _validate_guard_node(expr)
    names: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            names.add(node.id)
    names.discard("len")
    return names


class GuardNotEvaluable(Exception):
    """A guard references a variable the gateway has not yet observed."""


def eval_guard(source: str, bindings: dict[str, Any]) -> bool:
    """Deterministically evaluate a guard against gateway-recorded bindings."""
    expr = _guard_expr(source)
    _validate_guard_node(expr)
    return bool(_eval_node(expr, bindings))


def _eval_node(node: ast.expr, bindings: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in bindings:
            raise GuardNotEvaluable(node.id)
        return bindings[node.id]
    if isinstance(node, ast.Attribute):
        base = _eval_node(node.value, bindings)
        return _project(base, node.attr)
    if isinstance(node, ast.Subscript):
        base = _eval_node(node.value, bindings)
        return base[node.slice.value]  # type: ignore[union-attr]
    if isinstance(node, ast.Call):  # validated: len(<Name>)
        return len(_eval_node(node.args[0], bindings))
    if isinstance(node, ast.BoolOp):
        results = [_eval_node(v, bindings) for v in node.values]
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, bindings)
        right = _eval_node(node.comparators[0], bindings)
        op = node.ops[0]
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        return left >= right
    raise CompositePlanError(f"unexpected guard node {ast.dump(node)}")


def _project(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value[field]
    return getattr(value, field)


# ---------------------------------------------------------------------------
# Stage structure analysis
# ---------------------------------------------------------------------------

def _run_function(code: str) -> ast.FunctionDef:
    try:
        module = ast.parse(code)
    except SyntaxError as exc:
        raise CompositePlanError(f"stage code does not parse: {exc}") from exc
    funcs = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    if len(funcs) != 1 or funcs[0].name != "run":
        raise CompositePlanError("stage code must define exactly one function named 'run'")
    return funcs[0]


def assignment_map(code: str, tool_names: set[str]) -> dict[str, str]:
    """Map variables to the tool whose call they are assigned from.

    Only variables assigned from a tool that is called *exactly once* in the
    stage are returned -- those are the bindings the gateway can attribute
    to a specific envelope without ambiguity.
    """
    func = _run_function(code)
    tool_counts: dict[str, int] = {}
    assigns: list[tuple[str, str]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in tool_names:
                tool_counts[node.func.id] = tool_counts.get(node.func.id, 0) + 1
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in tool_names
        ):
            assigns.append((node.targets[0].id, node.value.func.id))
    return {var: tool for var, tool in assigns if tool_counts.get(tool) == 1}


# ---------------------------------------------------------------------------
# Fan-out instantiation: mechanical, LLM-free, observation-constant folding.
# ---------------------------------------------------------------------------

class _IndexSubstituter(ast.NodeTransformer):
    def __init__(self, index_var: str, value: int) -> None:
        self.index_var = index_var
        self.value = value

    def visit_Name(self, node: ast.Name) -> ast.expr:  # noqa: N802
        if node.id == self.index_var and isinstance(node.ctx, ast.Load):
            return ast.copy_location(ast.Constant(self.value), node)
        return node


class _ListAccessFolder(ast.NodeTransformer):
    """Replace ``list_var[i].field...`` chains with observed constants."""

    def __init__(self, list_var: str, list_value: Any) -> None:
        self.list_var = list_var
        self.list_value = list_value

    def _try_fold(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Subscript):
            base = self._try_fold(node.value)
            if base is _NOT_FOLDABLE:
                return _NOT_FOLDABLE
            if not (isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int)):
                raise CompositePlanError("fan-out body list index must be a literal integer")
            return base[node.slice.value]
        if isinstance(node, ast.Attribute):
            base = self._try_fold(node.value)
            if base is _NOT_FOLDABLE:
                return _NOT_FOLDABLE
            return _project(base, node.attr)
        if isinstance(node, ast.Name) and node.id == self.list_var:
            return self.list_value
        return _NOT_FOLDABLE

    def visit_Attribute(self, node: ast.Attribute) -> ast.expr:  # noqa: N802
        folded = self._try_fold(node)
        if folded is not _NOT_FOLDABLE:
            return ast.copy_location(_constant_or_raise(folded), node)
        return self.generic_visit(node)  # type: ignore[return-value]

    def visit_Subscript(self, node: ast.Subscript) -> ast.expr:  # noqa: N802
        folded = self._try_fold(node)
        if folded is not _NOT_FOLDABLE:
            return ast.copy_location(_constant_or_raise(folded), node)
        return self.generic_visit(node)  # type: ignore[return-value]

    def visit_Name(self, node: ast.Name) -> ast.expr:  # noqa: N802
        if node.id == self.list_var:
            raise CompositePlanError(
                f"fan-out body may reference {self.list_var!r} only via indexed field access"
            )
        return node


_NOT_FOLDABLE = object()


def _constant_or_raise(value: Any) -> ast.Constant:
    if isinstance(value, (bool, int, float, str)):
        return ast.Constant(value)
    raise CompositePlanError(
        f"observed value {value!r} is not a scalar; fan-out bodies must fold to constants"
    )


class _LocalRenamer(ast.NodeTransformer):
    def __init__(self, names: set[str], suffix: str) -> None:
        self.names = names
        self.suffix = suffix

    def visit_Name(self, node: ast.Name) -> ast.expr:  # noqa: N802
        if node.id in self.names:
            return ast.copy_location(ast.Name(f"{node.id}_{self.suffix}", node.ctx), node)
        return node


def instantiate_fanout(
    stage: StageTemplate,
    list_value: Any,
) -> FanoutInstantiation:
    """Mechanically unroll a fan-out template over an observed list."""
    assert stage.fanout is not None
    spec = stage.fanout
    func = _run_function(stage.code)
    params = [a.arg for a in func.args.args]
    if params != [spec.list_var]:
        raise CompositePlanError(
            f"fan-out stage must be 'def run({spec.list_var}):', got parameters {params}"
        )
    try:
        total = len(list_value)
    except TypeError as exc:
        raise CompositePlanError(
            f"fan-out variable {spec.list_var!r} is not a list-like observation"
        ) from exc
    n = min(total, spec.max_instances)

    merged: list[ast.stmt] = []
    for i in range(n):
        body = [_copy_stmt(s) for s in func.body]
        body = [_IndexSubstituter(spec.index_var, i).visit(s) for s in body]
        folder = _ListAccessFolder(spec.list_var, list_value)
        body = [folder.visit(s) for s in body]
        local_names = {
            t.id
            for s in body
            for node in ast.walk(s)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name)
        }
        body = [_LocalRenamer(local_names, str(i)).visit(s) for s in body]
        merged.extend(body)

    if not merged:
        merged = [ast.Pass()]
    out = ast.Module(
        body=[
            ast.FunctionDef(
                name="run",
                args=ast.arguments(
                    posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
                ),
                body=merged,
                decorator_list=[],
            )
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(out)
    return FanoutInstantiation(
        code=ast.unparse(out) + "\n",
        n_instances=n,
        truncated=max(0, total - n),
    )


def _copy_stmt(stmt: ast.stmt) -> ast.stmt:
    return ast.parse(ast.unparse(stmt)).body[0]


# ---------------------------------------------------------------------------
# Plan validation (composite accept gate)
# ---------------------------------------------------------------------------

def validate_plan(
    prompt: str,
    plan: CompositePlan,
    tools: list[ToolDoc],
    policy: PrecheckPolicy | None = None,
) -> list[str]:
    """Structural + Q15-e validation of a composite plan.

    Returns a list of violations (empty = structurally acceptable). Instance
    code still passes through ``pauth.prepare`` at activation time; this gate
    covers what is checkable before any observation exists.
    """
    violations: list[str] = []
    if not plan.stages:
        return ["composite plan has no stages"]

    tool_names = {t.name for t in tools}
    known_vars: set[str] = set()
    for idx, stage in enumerate(plan.stages):
        label = f"stage {idx}"
        # Guard discipline: stage 0 runs unconditionally; later guards may
        # only read variables bound by earlier stages.
        if idx == 0 and stage.guard is not None:
            violations.append(f"{label}: the first stage must not have a guard")
        if stage.guard is not None:
            try:
                refs = guard_variables(stage.guard)
                missing = refs - known_vars
                if missing:
                    violations.append(
                        f"{label}: guard reads {sorted(missing)} not bound by any earlier stage"
                    )
            except CompositePlanError as exc:
                violations.append(f"{label}: {exc}")

        # Template code: grammar + Q15-e precheck on author-written constants.
        template_code = stage.code
        if stage.fanout is not None:
            if idx == 0:
                violations.append(f"{label}: fan-out requires an observed list from an earlier stage")
            elif stage.fanout.list_var not in known_vars:
                violations.append(
                    f"{label}: fan-out list {stage.fanout.list_var!r} is not bound by an earlier stage"
                )
            if stage.fanout.max_instances < 1:
                violations.append(f"{label}: fan-out max_instances must be >= 1")
            try:
                probe = ast.unparse(
                    _IndexSubstituter(stage.fanout.index_var, 0).visit(
                        ast.parse(stage.code)
                    )
                )
                template_code = probe
            except SyntaxError as exc:
                violations.append(f"{label}: {exc}")
                continue
        else:
            try:
                func = _run_function(stage.code)
                if func.args.args:
                    violations.append(
                        f"{label}: sequential stage code must be self-contained (no parameters)"
                    )
            except CompositePlanError as exc:
                violations.append(f"{label}: {exc}")
                continue

        try:
            parse_and_validate(template_code)
        except RestrictedGrammarError as exc:
            violations.append(f"{label}: restricted grammar: {exc}")
        violations.extend(
            f"{label}: {v}" for v in precheck_code(prompt, template_code, tools, policy)
        )

        try:
            known_vars |= set(assignment_map(stage.code, tool_names))
        except CompositePlanError:
            pass  # already reported above

    return violations
