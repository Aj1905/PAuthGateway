"""Suite registry: merge several :class:`SuiteSpec`s into a single one.

The gateway's algorithmic core (``pauth.prepare`` / ``Enforcer``) operates
over one ``SuiteSpec``. Real deployments need to host several tool sources
side by side: the self-contained shopping suite for demos, AgentDojo's
banking / slack / travel / workspace adapters, MCP-server-backed suites
for user-registered SaaS, OpenAPI-backed suites, etc.

Rather than changing the gateway to know about multiple suites, this
module composes a *virtual* ``SuiteSpec`` whose tool universe is the
union of the underlying suites. Tool names must be globally unique;
collisions raise at registry-construction time so the user has to pick a
namespacing scheme explicitly.

A merged suite's ``make_env`` returns a dict keyed by source-suite name;
``tool_executor_factory`` returns a dispatcher that routes each tool call to
the originating suite's tool_executor. ``tool_signer`` is the union of the
underlying signers, so envelopes are tagged with the source signer and
PAuth's cross-service deduplication (``pauth.rules.compile_rules``)
keeps working unchanged.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from pauth.suites.base import SuiteSpec, TaskSpec, ToolSpec


@dataclasses.dataclass
class _MergedEnv:
    """Container so the tool_executor can recover each suite's env by name."""

    envs: dict[str, Any]


def namespaced_tool(source: str, tool: str) -> str:
    """Identifier-safe namespaced tool name (``<suite>__<tool>``, D2).

    The paper notation is ``<suite>:<tool>``, but the restricted grammar calls
    tools as Python identifiers, so ``:`` is not usable -- ``__`` is.
    """
    return f"{source}__{tool}"


def merge_suites(
    name: str, suites: dict[str, SuiteSpec], namespace: bool = False
) -> SuiteSpec:
    """Return a ``SuiteSpec`` whose tool universe is the union of ``suites``.

    ``name`` is the label the merged suite carries (used when the gateway
    asks the suite loader for a name). ``suites`` maps a source-suite
    label to its ``SuiteSpec``. Source labels drive per-suite env construction.

    With ``namespace=False`` (default) a tool-name collision raises
    ``ValueError`` -- unchanged behaviour. With ``namespace=True`` (D2) every
    tool is renamed to ``<source>__<tool>`` so collisions are impossible; the
    generated code and rules use the namespaced name, and the tool_executor maps it
    back to the owning suite's original tool.
    """
    merged_tools: dict[str, ToolSpec] = {}
    tool_owner: dict[str, str] = {}
    original_name: dict[str, str] = {}  # merged tool name -> source's tool name

    for source_name, source in suites.items():
        for tool_name, spec in source.tools.items():
            merged_name = namespaced_tool(source_name, tool_name) if namespace else tool_name
            if merged_name in merged_tools:
                prev = tool_owner[merged_name]
                raise ValueError(
                    f"tool name collision: {merged_name!r} is owned by both "
                    f"{prev!r} and {source_name!r}; rename one or set namespace=True"
                )
            if namespace:
                # Rename the tool identifier the grammar/enforcer/the Planner see.
                spec = dataclasses.replace(
                    spec, name=merged_name,
                    doc=dataclasses.replace(spec.doc, name=merged_name),
                )
            merged_tools[merged_name] = spec
            tool_owner[merged_name] = source_name
            original_name[merged_name] = tool_name

    def make_env() -> _MergedEnv:
        return _MergedEnv(envs={n: s.make_env() for n, s in suites.items()})

    def tool_executor_factory(merged_env: _MergedEnv) -> Callable[[str, dict[str, Any]], Any]:
        tool_executors: dict[str, Callable[[str, dict[str, Any]], Any]] = {
            n: s.tool_executor_factory(merged_env.envs[n]) for n, s in suites.items()
        }

        def run(tool: str, kwargs: dict[str, Any]) -> Any:
            owner = tool_owner.get(tool)
            if owner is None:
                raise ValueError(f"no source suite owns tool {tool!r}")
            return tool_executors[owner](original_name[tool], kwargs)

        return run

    # Tasks are paper-reproduction fixtures attached to specific suites.
    # The merged suite intentionally exposes none -- a multi-suite plan is
    # derived at runtime, not from a fixture list.
    merged_tasks: list[TaskSpec] = []

    return SuiteSpec(
        name=name,
        tools=merged_tools,
        make_env=make_env,
        tool_executor_factory=tool_executor_factory,
        tasks=merged_tasks,
    )


def tool_owners(suites: dict[str, SuiteSpec]) -> dict[str, str]:
    """Return ``{tool_name: source_suite_name}`` for the merged universe.

    Useful for introspection / logging / building config-aware error
    messages; the merged ``SuiteSpec`` itself does not expose this map.
    """
    owners: dict[str, str] = {}
    for source_name, source in suites.items():
        for tool_name in source.tools:
            if tool_name in owners:
                continue  # callers can detect collisions via merge_suites
            owners[tool_name] = source_name
    return owners
