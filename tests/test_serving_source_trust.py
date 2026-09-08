"""The serving path must gate tainted control operands (K4).

Before the lab wiring, ``AgentChannel`` built its ``Gateway`` without a
``SourceTrust``, whose library default is fail-OPEN: no tool was ever
untrusted, so the confirmation gate never fired in a real deployment. A
config-loaded deployment is now fail-closed unless it says otherwise.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gateway.ingress.agent_channel import AgentChannel
from gateway.planning.planner import PlanDraft
from gateway.runtime.confirmation import SourceTrust
from gateway.serving.config import load_config
from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec

PROMPT = "Read the destination stored in /tmp/w/dest.txt and move /tmp/w/notes.txt there."
PLAN = '''\
def run():
    dest = read_text_file("/tmp/w/dest.txt")
    move_file("/tmp/w/notes.txt", dest)
'''


def _tool(name: str, params: list[str]) -> ToolSpec:
    return ToolSpec(
        name=name, params=params, signer="fs",
        doc=ToolDoc(name=name, description=name,
                    parameters=[{"name": p, "type": "string", "desc": p} for p in params],
                    returns="string"),
    )


def _suite(executed: list) -> SuiteSpec:
    tools = {
        "read_text_file": _tool("read_text_file", ["path"]),
        "move_file": _tool("move_file", ["source", "destination"]),
    }

    def factory(_env):
        def run(tool, kwargs):
            executed.append((tool, kwargs))
            return "/tmp/w/moved.txt" if tool == "read_text_file" else "ok"
        return run
    return SuiteSpec(name="fs", tools=tools, make_env=lambda: None,
                     tool_executor_factory=factory, tasks=[])


class _StaticPlanner:
    def generate(self, prompt, suite_loader):
        return PlanDraft(suite_name="fs", code=PLAN, reason="fixture")


def _channel(source_trust):
    executed: list[Any] = []
    suite = _suite(executed)
    channel = AgentChannel(lambda name: suite, source_trust=source_trust)
    # Drive the plan through the gateway directly (the planner is a fixture).
    sub = channel._gateway.submit_user_prompt_with_planner(PROMPT, _StaticPlanner())  # noqa: SLF001
    assert sub.accepted, sub.reason
    channel._prompt_received = True  # noqa: SLF001
    return channel, executed


def test_fail_closed_trust_holds_tainted_destination_until_the_human_approves():
    channel, executed = _channel(SourceTrust.fail_closed())
    read = channel.receive_json({"kind": "tool_call", "tool": "read_text_file",
                                 "kwargs": {"path": "/tmp/w/dest.txt"}})
    assert read["permit"] is True
    move = channel.receive_json({"kind": "tool_call", "tool": "move_file",
                                 "kwargs": {"source": "/tmp/w/notes.txt",
                                            "destination": "/tmp/w/moved.txt"}})
    assert move["permit"] is False
    assert move["execution_status"] == "not_dispatched"
    assert "/tmp/w/moved.txt" not in move["reason"]  # value-free toward the agent
    assert [t for t, _ in executed] == ["read_text_file"]

    pending = channel.operator_pending()
    [hold] = pending["confirmations"]
    assert hold["tool"] == "move_file" and hold["param_name"] == "destination"
    assert hold["value"] == "/tmp/w/moved.txt"
    assert "read_text_file" in hold["source"]
    assert "【どこから取得した】" in hold["display"]

    assert channel.operator_decide("confirmation", hold["id"], True) is True
    move = channel.receive_json({"kind": "tool_call", "tool": "move_file",
                                 "kwargs": {"source": "/tmp/w/notes.txt",
                                            "destination": "/tmp/w/moved.txt"}})
    assert move["permit"] is True and move["execution_status"] == "succeeded"
    # A different tainted value is not covered by that approval.
    other = channel.receive_json({"kind": "tool_call", "tool": "move_file",
                                  "kwargs": {"source": "/tmp/w/notes.txt",
                                             "destination": "/tmp/w/other.txt"}})
    assert other["permit"] is False


def test_open_trust_would_not_gate():
    channel, executed = _channel(SourceTrust())
    channel.receive_json({"kind": "tool_call", "tool": "read_text_file",
                          "kwargs": {"path": "/tmp/w/dest.txt"}})
    move = channel.receive_json({"kind": "tool_call", "tool": "move_file",
                                 "kwargs": {"source": "/tmp/w/notes.txt",
                                            "destination": "/tmp/w/moved.txt"}})
    assert move["permit"] is True  # the fail-open behaviour we no longer default to


def test_config_defaults_to_fail_closed_and_validates_block(tmp_path):
    cfg = tmp_path / "g.json"
    cfg.write_text(json.dumps({"merged_suite_name": "m",
                               "suites": [{"name": "shopping", "kind": "shopping"}]}))
    loaded = load_config(cfg)
    assert loaded.source_trust.default_untrusted is True
    assert loaded.source_trust.is_untrusted("get_product_details")

    cfg.write_text(json.dumps({"merged_suite_name": "m",
                               "suites": [{"name": "shopping", "kind": "shopping"}],
                               "source_trust": {"mode": "fail_closed",
                                                "trusted_tools": ["get_cart_summary"]}}))
    loaded = load_config(cfg)
    assert not loaded.source_trust.is_untrusted("get_cart_summary")
    assert loaded.source_trust.is_untrusted("get_product_details")

    cfg.write_text(json.dumps({"merged_suite_name": "m",
                               "suites": [{"name": "shopping", "kind": "shopping"}],
                               "source_trust": {"trusted_tools": ["nope"]}}))
    with pytest.raises(ValueError, match="unknown tools"):
        load_config(cfg)
