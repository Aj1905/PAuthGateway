"""Adapt AgentDojo task suites to the framework-neutral :class:`SuiteSpec`.

PAuth is "prototyped in the agent-security evaluation framework AgentDojo"
(paper abstract).  This module exposes AgentDojo's banking / slack / travel /
workspace suites -- their tools, environments and user tasks -- in the form
the PAuth experiment runner consumes, including the *output schema* extension
to each tool (paper sec. 4.1.1).
"""

from __future__ import annotations

import dataclasses
import types
import typing
from typing import Any, Callable

from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suites

from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, TaskSpec, ToolSpec

from .forced_injection import generate_for_task

AGENTDOJO_SUITES = ("banking", "slack", "travel", "workspace")
_BENCHMARK_VERSION = "v1"


def _structure_names(prose: str) -> list[str]:
    """Turn a prose listing into a structured ``list[str]`` of names.

    AgentDojo's travel ``get_all_*_in_city`` tools return a single string like
    ``"Hotel Names: A\\nB\\nC\\n"`` -- a human-readable blob. The very next tools
    (``get_hotels_prices``, ``get_rating_reviews_for_*``) expect a ``list[str]``
    of names, but the restricted grammar has no string methods, so generated code
    cannot split the blob and instead subscripts or forwards it whole -> KeyError
    at runtime. This normalises the blob into the list those tools actually want.
    The first line carries a ``"<prefix>: <first name>"`` header; every other
    non-empty line is a bare name.
    """
    if not isinstance(prose, str):
        return prose  # already structured; leave untouched
    names: list[str] = []
    for i, raw in enumerate(prose.split("\n")):
        line = raw.strip()
        if i == 0 and ": " in line:
            line = line.split(": ", 1)[1]
        if line:
            names.append(line)
    return names


# Per-suite adapter post-processors: tool name -> function applied to its return
# so the value the enforcer sees matches the schema downstream tools consume.
# This is the "adapter layer" -- faithful to PAuth's premise of structured tool
# I/O, not a change to task semantics.
_STRUCTURED_RETURNS: dict[str, dict[str, Callable[[Any], Any]]] = {
    "travel": {
        "get_all_hotels_in_city": _structure_names,
        "get_all_restaurants_in_city": _structure_names,
        "get_all_car_rental_companies_in_city": _structure_names,
    },
}


def _type_str(annotation: Any) -> str:
    """Render a type annotation as a compact, LLM-readable schema string."""
    if annotation is None or annotation is type(None):
        return "none"
    origin = typing.get_origin(annotation)
    if origin in (list, tuple):
        args = typing.get_args(annotation)
        return f"list of {_type_str(args[0])}" if args else "list"
    if origin is dict:
        args = typing.get_args(annotation)
        if len(args) == 2:
            return f"object<{_type_str(args[0])} -> {_type_str(args[1])}>"
        return "object"
    if origin in (types.UnionType, typing.Union):
        args = typing.get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        rendered = "|".join(_type_str(a) for a in non_none)
        return f"{rendered}|None" if type(None) in args else rendered
    if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
        fields = ", ".join(
            f"{name}: {_type_str(field.annotation)}"
            for name, field in annotation.model_fields.items()
        )
        return f"object {{{fields}}}"
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def _tool_doc(tool: Any) -> ToolDoc:
    """Build a codegen schema (with output schema) for an AgentDojo tool."""
    parameters: list[dict[str, str]] = []
    for name, field in tool.parameters.model_fields.items():
        parameters.append(
            {
                "name": name,
                "type": _type_str(field.annotation),
                "desc": (field.description or "").strip(),
            }
        )
    return ToolDoc(
        name=tool.name,
        description=(getattr(tool, "description", "") or "").strip(),
        parameters=parameters,
        returns=_type_str(tool.return_type),
    )


def _build_tools(agentdojo_suite: Any) -> dict[str, ToolSpec]:
    structured = _STRUCTURED_RETURNS.get(agentdojo_suite.name, {})
    tools: dict[str, ToolSpec] = {}
    for tool in agentdojo_suite.tools:
        doc = _tool_doc(tool)
        if tool.name in structured:
            # The adapter restructures this tool's prose return into a list, so
            # advertise the schema the LLM should code against.
            doc = dataclasses.replace(doc, returns="list of str")
        tools[tool.name] = ToolSpec(
            name=tool.name,
            params=list(tool.parameters.model_fields.keys()),
            doc=doc,
            signer=agentdojo_suite.name,
        )
    return tools


def _make_env_factory(agentdojo_suite: Any) -> Callable[[], Any]:
    defaults = agentdojo_suite.get_injection_vector_defaults()

    def make_env() -> Any:
        return agentdojo_suite.load_and_inject_default_environment(dict(defaults))

    return make_env


def _make_runner_factory(agentdojo_suite: Any) -> Callable[[Any], Callable[..., Any]]:
    runtime = FunctionsRuntime(agentdojo_suite.tools)
    structured = _STRUCTURED_RETURNS.get(agentdojo_suite.name, {})

    def runner_factory(env: Any) -> Callable[[str, dict[str, Any]], Any]:
        def run(tool: str, kwargs: dict[str, Any]) -> Any:
            result, error = runtime.run_function(env, tool, kwargs, raise_on_error=False)
            if error:
                raise RuntimeError(error)
            post = structured.get(tool)
            if post is not None:
                result = post(result)
            return result

        return run

    return runner_factory


def load_suite(name: str) -> SuiteSpec:
    """Load one AgentDojo suite as a :class:`SuiteSpec`."""
    agentdojo_suite = get_suites(_BENCHMARK_VERSION)[name]
    tools = _build_tools(agentdojo_suite)
    tool_params = {n: s.params for n, s in tools.items()}
    make_env = _make_env_factory(agentdojo_suite)

    tasks: list[TaskSpec] = []
    for task_id in sorted(agentdojo_suite.user_tasks):
        user_task = agentdojo_suite.user_tasks[task_id]
        tasks.append(
            TaskSpec(
                id=f"{name}.{task_id}",
                prompt=user_task.PROMPT,
                reference_code=None,  # generated by A1 at run time
                forced_injections=generate_for_task(
                    agentdojo_suite, user_task, tool_params, make_env
                ),
            )
        )

    return SuiteSpec(
        name=name,
        tools=tools,
        make_env=make_env,
        runner_factory=_make_runner_factory(agentdojo_suite),
        tasks=tasks,
    )


def load_all() -> list[SuiteSpec]:
    """Load every AgentDojo suite used in the evaluation."""
    return [load_suite(name) for name in AGENTDOJO_SUITES]
