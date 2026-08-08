"""The PAuth task-submission pipeline: the Planner -> the Slicer -> the RuleCompiler.

Ties together imperative-code validation, slice derivation and rule
compilation.  the Planner (LLM code generation) lives in :mod:`pauth.codegen`; this
module starts from a code string and produces the deterministic artefacts.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib

from .grammar_validator import (
    DSL_PROFILE_EXTENDED,
    parse_and_validate,
    strip_dead_code,
    validate_semantics,
)
from .normalize import normalize_run
from .rule_compiler import Rule, compile_rules
from .slicer import Slice, derive_slices
from .symbolic import HELPERS, call_name


@dataclasses.dataclass(frozen=True)
class ExecutionStep:
    """One statically declared tool-call occurrence in the audited plan."""

    key: str
    tool: str
    source_line: int
    depends_on: tuple[str, ...] = ()
    depends_on_steps: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ExecutionPlan:
    """Compiler-derived contract tying a PAuth program to its tool-call plan.

    The LLM still proposes restricted ``run`` code.  The deterministic compiler
    turns that proposal into both authorization rules and this compact plan
    contract, so runtime components never need to trust a second LLM-authored
    representation of the same task.
    """

    source_sha256: str
    steps: tuple[ExecutionStep, ...]

    @property
    def allowed_tools(self) -> frozenset[str]:
        return frozenset(step.tool for step in self.steps)

    def to_dict(self) -> dict:
        return {
            "source_sha256": self.source_sha256,
            "allowed_tools": sorted(self.allowed_tools),
            "steps": [dataclasses.asdict(step) for step in self.steps],
        }


@dataclasses.dataclass
class PreparedTask:
    """All artefacts derived from a task's generated code."""

    source: str               # cleaned, executable code
    func: ast.FunctionDef
    slices: list[Slice]
    rules: list[Rule]
    execution_plan: ExecutionPlan

    def render_slices(self) -> str:
        return "\n\n".join(s.render() for s in self.slices)


def _slice_dependencies(
    slice_: Slice,
    tool_names: set[str],
    call_keys: dict[int, str],
) -> tuple[set[str], set[str]]:
    """Return referenced tool names and exact prior action IDs."""
    dependency_tools: set[str] = set()
    dependency_steps: set[str] = set()
    roots = (
        list(slice_.arg_exprs)
        + list(slice_.guards)
        + list(slice_.lets.values())
        + [iter_expr for _var, iter_expr in slice_.loops]
        + [frame.iterable for frame in slice_.helper_frames]
        + [frame.body for frame in slice_.helper_frames]
    )
    for root in roots:
        for node in ast.walk(root):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)
            if name in tool_names and name not in HELPERS:
                step_key = call_keys.get(id(node))
                if step_key is not None and step_key != slice_.key:
                    dependency_steps.add(step_key)
                    dependency_tools.add(name)
    return dependency_tools, dependency_steps


def _build_execution_plan(
    source: str,
    slices: list[Slice],
    tool_names: set[str],
) -> ExecutionPlan:
    """Deduplicate branch-forked slices into a stable source-order contract."""
    call_keys = {id(slice_.call_node): slice_.key for slice_ in slices}
    order: list[str] = []
    merged: dict[str, dict] = {}
    for slice_ in slices:
        key = slice_.key
        if key not in merged:
            order.append(key)
            merged[key] = {
                "tool": slice_.tool,
                "source_line": getattr(slice_.call_node, "lineno", 0),
                "depends_on": set(),
                "depends_on_steps": set(),
            }
        dependency_tools, dependency_steps = _slice_dependencies(
            slice_, tool_names, call_keys
        )
        merged[key]["depends_on"].update(dependency_tools)
        merged[key]["depends_on_steps"].update(dependency_steps)
    steps = tuple(
        ExecutionStep(
            key=key,
            tool=merged[key]["tool"],
            source_line=merged[key]["source_line"],
            depends_on=tuple(sorted(merged[key]["depends_on"])),
            depends_on_steps=tuple(sorted(merged[key]["depends_on_steps"])),
        )
        for key in order
    )
    return ExecutionPlan(
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        steps=steps,
    )


def prepare(
    code: str,
    tool_names: set[str],
    tool_service: dict[str, str] | None = None,
    *,
    dsl_profile: str = DSL_PROFILE_EXTENDED,
) -> PreparedTask:
    """Run the Planner's output through validation (grammar), the Slicer (slices) and the RuleCompiler (rules).

    Raises :class:`pauth.grammar_validator.DSLRejectionError` if the code violates
    the DSL.

    ``dsl_profile`` selects the DSL version (experiment axis G):
    ``"g2"`` (default, this repo's extended DSL) or ``"g1"`` (the Appendix A
    reconstruction defined in ``docs/SYSTEM_MODEL.md``). Under ``"g1"``
    the Tier-1 normalization is also skipped because it widens the acceptance
    surface beyond that baseline.
    """
    func = parse_and_validate(code, profile=dsl_profile)
    func = strip_dead_code(func, tool_names)
    if dsl_profile == DSL_PROFILE_EXTENDED:
        # Tier-1 semantics-preserving normalization: rewrite reject-but-safe
        # forms (call-as-argument, straight-line reassignment) into the
        # slicer's canonical form. Does not change behavior, so the
        # deterministic core is untouched.
        func = normalize_run(func)
    validate_semantics(func, tool_names, profile=dsl_profile)
    slices = derive_slices(func, tool_names)
    rules = compile_rules(slices, tool_service)
    cleaned = ast.unparse(ast.Module(body=[func], type_ignores=[]))
    # Reparse the canonical source before recording source locations. The
    # normalized program and its digest are intentionally insensitive to the
    # model's blank lines/indent trivia; retaining pre-normalization ``lineno``
    # values here would make two identical contracts hash/display differently.
    contract_func = parse_and_validate(cleaned, profile=dsl_profile)
    contract_slices = derive_slices(contract_func, tool_names)
    execution_plan = _build_execution_plan(cleaned, contract_slices, tool_names)
    return PreparedTask(
        source=cleaned,
        func=func,
        slices=slices,
        rules=rules,
        execution_plan=execution_plan,
    )
