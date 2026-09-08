"""Verification reads: declared read-only tools run off-plan only when every
operand is named in the user's prompt. Everything else stays default-deny."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gateway.planning.planner import PlanDraft
from gateway.runtime.gateway import Gateway
from gateway.runtime.policy import PolicySpec
from gateway.serving.config import load_config
from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec

PROMPT = "Create a directory /tmp/work/archive and move /tmp/work/notes.txt into it."
PLAN = '''\
def run():
    create_directory("/tmp/work/archive")
    move_file("/tmp/work/notes.txt", "/tmp/work/archive/notes.txt")
'''


class _Env:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []


def _tool(name: str, params: list[str]) -> ToolSpec:
    return ToolSpec(
        name=name, params=params, signer="fs",
        doc=ToolDoc(name=name, description=name,
                    parameters=[{"name": p, "type": "string", "desc": p} for p in params],
                    returns="object"),
    )


TOOLS = {
    "create_directory": _tool("create_directory", ["path"]),
    "move_file": _tool("move_file", ["source", "destination"]),
    "list_directory": _tool("list_directory", ["path"]),
    "get_file_info": _tool("get_file_info", ["path"]),
    "read_text_file": _tool("read_text_file", ["path", "head", "tail"]),
}


def _suite(env: _Env) -> SuiteSpec:
    def executor_factory(_env):
        def run(tool, kwargs):
            env.executed.append((tool, kwargs))
            return f"{tool} ok"
        return run
    return SuiteSpec(name="fs", tools=TOOLS, make_env=lambda: env,
                     tool_executor_factory=executor_factory, tasks=[])


class _StaticPlanner:
    def generate(self, prompt, suite_loader):
        return PlanDraft(suite_name="fs", code=PLAN, reason="fixture")


def _gateway(reads: list[str]) -> tuple[Gateway, _Env]:
    env = _Env()
    suite = _suite(env)
    policy = PolicySpec.from_param_names({}, suite.tool_params(), verification_reads=reads)
    gw = Gateway(lambda name: suite, operand_policy=policy)
    sub = gw.submit_user_prompt_with_planner(PROMPT, _StaticPlanner())
    assert sub.accepted, sub.reason
    return gw, env


def test_off_plan_read_of_prompt_named_path_runs_without_a_hold():
    gw, env = _gateway(["list_directory", "get_file_info"])
    result = gw.handle_tool_call("list_directory", ["/tmp/work/archive"])
    assert result.permit and result.execution_status.value == "succeeded"
    assert env.executed == [("list_directory", {"path": "/tmp/work/archive"})]
    assert gw.pending_reauthorizations() == []
    # Repeatable: verification reads are not consumed like plan rules.
    assert gw.handle_tool_call("list_directory", ["/tmp/work/archive"]).permit
    # Audit records it as a permit with the policy reason.
    assert any(e["decision"] == "permit" and "verification read" in e["reason"]
               for e in (dict(x) if isinstance(x, dict) else x.__dict__ for x in gw.audit_log()))


def test_composed_path_counts_as_named():
    gw, env = _gateway(["get_file_info"])
    assert gw.handle_tool_call("get_file_info", ["/tmp/work/archive/notes.txt"]).permit


def test_read_of_unnamed_path_is_still_held():
    gw, env = _gateway(["list_directory", "read_text_file"])
    held = gw.handle_tool_call("list_directory", ["/tmp/work/secret"])
    assert not held.permit and held.reauthorization_required
    traversal = gw.handle_tool_call("list_directory", ["/tmp/work/../etc"])
    assert not traversal.permit
    shallow = gw.handle_tool_call("list_directory", ["/tmp"])
    assert not shallow.permit
    assert env.executed == []


def test_none_operands_are_fine_but_unnamed_numbers_are_not():
    gw, env = _gateway(["read_text_file"])
    assert gw.handle_tool_call("read_text_file", ["/tmp/work/notes.txt", None, None]).permit
    assert not gw.handle_tool_call("read_text_file", ["/tmp/work/notes.txt", 5, None]).permit


def test_undeclared_tool_is_not_a_verification_read():
    gw, env = _gateway(["get_file_info"])
    held = gw.handle_tool_call("list_directory", ["/tmp/work/archive"])
    assert not held.permit and held.reauthorization_required


def test_plan_rules_and_attacks_unaffected():
    gw, env = _gateway(["list_directory"])
    assert gw.handle_tool_call("create_directory", ["/tmp/work/archive"]).permit
    assert gw.handle_tool_call("move_file", ["/tmp/work/notes.txt", "/tmp/work/archive/notes.txt"]).permit
    bad = gw.handle_tool_call("move_file", ["/tmp/work/archive/notes.txt", "/tmp/evil.txt"])
    assert not bad.permit


def test_policy_rejects_unknown_read_tool():
    with pytest.raises(ValueError, match="verification_reads"):
        PolicySpec.from_param_names({}, {"a": ["x"]}, verification_reads=["nope"])


def test_config_parses_verification_reads(tmp_path):
    cfg = tmp_path / "g.json"
    cfg.write_text(json.dumps({
        "merged_suite_name": "m",
        "suites": [{"name": "shopping", "kind": "shopping",
                    "verification_reads": ["get_product_details"]}],
    }))
    loaded = load_config(cfg)
    assert loaded.policy.verification_reads == frozenset({"get_product_details"})
    cfg.write_text(json.dumps({
        "merged_suite_name": "m",
        "suites": [{"name": "shopping", "kind": "shopping", "verification_reads": ["bogus"]}],
    }))
    with pytest.raises(ValueError):
        load_config(cfg)


def test_immediate_parent_of_named_path_is_readable_but_not_deeper_ancestors():
    gw, env = _gateway(["list_directory"])
    assert gw.handle_tool_call("list_directory", ["/tmp/work"]).permit
    assert gw.handle_tool_call("list_directory", ["/tmp/work/"]).permit
    assert not gw.handle_tool_call("list_directory", ["/tmp"]).permit
    assert not gw.handle_tool_call("list_directory", ["/"]).permit
