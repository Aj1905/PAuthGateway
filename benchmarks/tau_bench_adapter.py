"""Adapt tau-bench (Sierra, MIT) retail domain into a PAuth SuiteSpec.

tau-bench is realistic structured tool-agent work: typed function-calling over a
DB-backed retail domain (users / orders / products), with policy rules. We use it
for the AVAILABILITY axis -- can the Planner plan on real, complex, stateful tasks? -- the
honest counterweight to FN=0. (tau-bench has no injections; we author a few
off-plan probes for the security axis.)

tau-bench tools return JSON STRINGS. This adapter (a) surfaces a STRUCTURED return
schema (from the domain entities) so the Planner sees fields, not "string" (the travel
stringified-collection problem), and (b) parses each result into an attribute-
accessible object so a generated plan's ``order.status`` works -- gateway-side
structuring of an otherwise stringified API.

Reference plans come from each task's ground-truth actions (fully-resolved
constant calls), so the suite runs OFFLINE. The real availability measure -- does
the Planner GENERATE a valid plan from the instruction -- needs a live run:
``python -m eval.fpfn --suites tau_retail --no-cache`` (API key).
"""

from __future__ import annotations

import copy
import json
from typing import Any, Callable

from pauth.codegen import ToolDoc
from pauth.suites.base import Call, SuiteSpec, TaskSpec, ToolSpec

# Structured return schema per tool, reconstructed from the retail entities (the
# tool bodies only ``json.dumps`` these). Approximate but field-typed, which is
# what the Planner needs to navigate the result.
_RETURNS = {
    "get_order_details": ("object {order_id: string, user_id: string, address: object, "
                          "items: list of object, fulfillments: list of object, "
                          "status: string, payment_history: list of object}"),
    "get_product_details": "object {name: string, product_id: string, variants: object}",
    "get_user_details": ("object {name: object, address: object, email: string, "
                         "payment_methods: object, orders: list of string}"),
    "find_user_id_by_email": "string (user id)",
    "find_user_id_by_name_zip": "string (user id)",
    "list_all_product_types": "object {name: string -> product_id: string}",
    "cancel_pending_order": "object (updated order)",
    "exchange_delivered_order_items": "object (updated order)",
    "modify_pending_order_address": "object (updated order)",
    "modify_pending_order_items": "object (updated order)",
    "modify_pending_order_payment": "object (updated order)",
    "modify_user_address": "object (updated user)",
    "return_delivered_order_items": "object (updated order)",
    "calculate": "string (number)",
    "think": "string",
    "transfer_to_human_agents": "string",
}
# Off-plan writes replayed as forced injections (default-deny must block them).
_INJECT_WRITES = ("cancel_pending_order", "modify_user_address", "return_delivered_order_items")


def _env():
    from tau_bench.envs import get_env
    return get_env("retail", user_strategy="llm", user_model="gpt-4o",
                   user_provider="openai", task_split="test")


def _wrap(v: Any) -> Any:
    """Make a parsed JSON value attribute-accessible (dict.field, list of same)."""
    if isinstance(v, dict):
        obj = type("Row", (), {})()
        for k, val in v.items():
            setattr(obj, k, _wrap(val))
        return obj
    if isinstance(v, list):
        return [_wrap(x) for x in v]
    return v


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_literal(x) for x in value) + "]"
    return json.dumps(str(value))


def _tools(env) -> dict[str, ToolSpec]:
    tools: dict[str, ToolSpec] = {}
    for info in env.tools_info:
        fn = info["function"]
        name = fn["name"]
        props = fn.get("parameters", {}).get("properties", {})
        params = list(props.keys())
        tools[name] = ToolSpec(
            name=name, params=params, signer="tau_retail",
            doc=ToolDoc(
                name=name,
                description=fn.get("description", "").strip().split("\n")[0],
                parameters=[{"name": p, "type": props[p].get("type", "any"),
                             "desc": props[p].get("description", "").strip()[:80]}
                            for p in params],
                returns=_RETURNS.get(name, "object"),
            ),
        )
    return tools


def _reference_code(actions, param_order) -> str:
    lines = ["def run():"]
    for a in actions:
        order = param_order.get(a.name, list(a.kwargs.keys()))
        args = ", ".join(_literal(a.kwargs.get(p)) for p in order)
        lines.append(f"    {a.name}({args})")
    return "\n".join(lines) + "\n"


def build_suite(reference: bool = True) -> SuiteSpec:
    """``reference=True`` ships the ground-truth plan (offline sanity). With
    ``reference=False`` tasks carry NO plan, so the Planner must GENERATE one from the
    instruction -- the real availability measure (needs an API key)."""
    env = _env()
    tools = _tools(env)
    param_order = {n: s.params for n, s in tools.items()}
    tools_map = env.tools_map
    base_data = env.data

    def make_env():
        return copy.deepcopy(base_data)

    def runner_factory(data):
        def run(tool: str, kwargs: dict[str, Any]) -> Any:
            res = tools_map[tool].invoke(data, **kwargs)
            if isinstance(res, str) and res[:1] in "{[":
                try:
                    return _wrap(json.loads(res))
                except json.JSONDecodeError:
                    return res
            return res
        return run

    tasks: list[TaskSpec] = []
    for i, t in enumerate(env.tasks):
        acts = [a for a in t.actions if a.name in tools]
        if not acts:
            continue
        plan_tools = {a.name for a in acts}
        # Off-plan write injections (a write the plan never authorized).
        injections = [Call(w, []) for w in _INJECT_WRITES if w not in plan_tools]
        tasks.append(TaskSpec(
            id=f"tau_retail_{i}",
            prompt=t.instruction,
            reference_code=_reference_code(acts, param_order) if reference else None,
            forced_injections=injections or [Call("modify_user_address", [])],
        ))

    return SuiteSpec(
        name="tau_retail", tools=tools, make_env=make_env,
        runner_factory=runner_factory, tasks=tasks,
    )


def build_suite_a1() -> SuiteSpec:
    """the Planner-generation variant: no reference plan, so the Planner plans from the instruction."""
    return build_suite(reference=False)
