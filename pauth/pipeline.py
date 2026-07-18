"""The PAuth task-submission pipeline: the Planner -> the Slicer -> the Rule compiler.

Ties together imperative-code validation, slice derivation and rule
compilation.  the Planner (LLM code generation) lives in :mod:`pauth.codegen`; this
module starts from a code string and produces the deterministic artefacts.
"""

from __future__ import annotations

import ast
import dataclasses

from .grammar import parse_and_validate, strip_dead_code, validate_semantics
from .normalize import normalize_run
from .rules import Rule, compile_rules
from .slicing import Slice, derive_slices


@dataclasses.dataclass
class PreparedTask:
    """All artefacts derived from a task's generated code."""

    source: str               # cleaned, executable code
    func: ast.FunctionDef
    slices: list[Slice]
    rules: list[Rule]

    def render_slices(self) -> str:
        return "\n\n".join(s.render() for s in self.slices)


def prepare(
    code: str,
    tool_names: set[str],
    tool_service: dict[str, str] | None = None,
) -> PreparedTask:
    """Run the Planner's output through validation (grammar), the Slicer (slices) and the Rule compiler (rules).

    Raises :class:`pauth.grammar.RestrictedGrammarError` if the code violates
    the restricted grammar.
    """
    func = parse_and_validate(code)
    func = strip_dead_code(func, tool_names)
    # Tier-1 semantics-preserving normalization: rewrite reject-but-safe forms
    # (call-as-argument, straight-line reassignment) into the slicer's canonical
    # form. Does not change behavior, so the deterministic core is untouched.
    func = normalize_run(func)
    validate_semantics(func, tool_names)
    slices = derive_slices(func, tool_names)
    rules = compile_rules(slices, tool_service)
    cleaned = ast.unparse(ast.Module(body=[func], type_ignores=[]))
    return PreparedTask(source=cleaned, func=func, slices=slices, rules=rules)
