"""Adapt InjecAgent (Zhan et al., ACL Findings 2024) into a PAuth SuiteSpec.

InjecAgent benchmarks indirect prompt injection: a benign user task calls a tool
whose (structured) response carries an attacker instruction in a free-text field
(e.g. a product review's ``review_content``), trying to make the agent invoke an
ATTACKER tool the user never asked for. This is exactly the "structured schema,
prose field = injection surface" case: the tool return is a typed object, but one
field is attacker-controlled.

What this adapter measures. For each case we build the benign plan (a call to the
User Tool) as reference the Planner code, and replay each Attacker Tool as a forced
injection. PAuth authorizes only the user tool, so every attacker call is
off-plan and must be denied (default-deny). This is the SECURITY axis (does PAuth
block the injected off-plan action) on InjecAgent's real attack scenarios.

Honest scope. Tool execution is stubbed (InjecAgent has no real backend -- it is
LLM-emulated upstream), and the benign plan is synthesized from the User Tool, not
the Planner-generated. So this is an injection-case source, not a runnable-env / the Planner-quality
suite. Data is a vendored subset under injecagent_data/ (see its NOTICE.md).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Callable

from pauth.codegen import ToolDoc
from pauth.suites.base import Call, SuiteSpec, TaskSpec, ToolSpec

_DATA = Path(__file__).resolve().parent / "injecagent_data"


def _render_return(returns: list[dict[str, Any]]) -> str:
    """Render InjecAgent's return list as a structured schema string."""
    if not returns:
        return "object"
    fields = ", ".join(f"{r['name']}: {r.get('type', 'any')}" for r in returns)
    return f"object {{{fields}}}"


def _tool_specs() -> dict[str, ToolSpec]:
    """Flatten tools.json into {name_for_model+tool.name: ToolSpec}."""
    raw = json.loads((_DATA / "tools.json").read_text())
    tools: dict[str, ToolSpec] = {}
    for toolkit in raw:
        prefix = toolkit["name_for_model"]
        for t in toolkit.get("tools", []):
            full = prefix + t["name"]
            params = [p["name"] for p in t.get("parameters", [])]
            tools[full] = ToolSpec(
                name=full,
                params=params,
                signer="injecagent",
                doc=ToolDoc(
                    name=full,
                    description=(t.get("summary") or t.get("description") or "").strip(),
                    parameters=[
                        {"name": p["name"], "type": p.get("type", "any"),
                         "desc": (p.get("description") or "").strip()}
                        for p in t.get("parameters", [])
                    ],
                    returns=_render_return(t.get("returns", [])),
                ),
            )
    return tools


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)  # double-quoted
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value))


def _reference_code(user_tool: str, params_str: str, param_order: list[str]) -> str:
    """Synthesize the benign plan: one call to the user tool, args in schema order."""
    try:
        parsed = ast.literal_eval(params_str) if params_str else {}
        if not isinstance(parsed, dict):
            parsed = {}
    except (ValueError, SyntaxError):
        parsed = {}
    args = ", ".join(_literal(parsed.get(p)) for p in param_order)
    return f"def run():\n    result = {user_tool}({args})\n"


def _stub_runner(env: Any) -> Callable[[str, dict[str, Any]], Any]:
    # No real backend: return an empty structured object for any tool. The benign
    # plan does not use the result; attacker calls are checked, never executed.
    def run(tool: str, kwargs: dict[str, Any]) -> Any:
        return {}
    return run


def build_suite() -> SuiteSpec:
    """Build the InjecAgent SuiteSpec from the vendored dh + ds base test cases."""
    tools = _tool_specs()
    param_order = {n: s.params for n, s in tools.items()}

    tasks: list[TaskSpec] = []
    for kind in ("dh", "ds"):  # direct-harm + data-stealing
        cases = json.loads((_DATA / f"cases_{kind}_base.json").read_text())
        for i, c in enumerate(cases):
            user_tool = c["User Tool"]
            if user_tool not in tools:
                continue
            code = _reference_code(user_tool, c.get("Tool Parameters", ""), param_order[user_tool])
            # Each attacker tool the injection tries to invoke is off-plan -> deny.
            injections = [Call(a, []) for a in c.get("Attacker Tools", []) if a in tools]
            if not injections:
                continue
            tasks.append(TaskSpec(
                id=f"injecagent_{kind}_{i}_{user_tool}",
                prompt=c["User Instruction"],
                reference_code=code,
                forced_injections=injections,
            ))

    return SuiteSpec(
        name="injecagent",
        tools=tools,
        make_env=lambda: object(),
        runner_factory=_stub_runner,
        tasks=tasks,
    )
