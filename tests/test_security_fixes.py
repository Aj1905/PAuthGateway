"""Regression tests for the security audit fixes (OSS core).

Each test would FAIL against the pre-fix code and documents the exploit it closes.
"""

from __future__ import annotations

import math

import pytest

from pauth.grammar_validator import (
    DSLRejectionError,
    parse_and_validate,
    strip_dead_code,
    validate_semantics,
)
from pauth.evaluator import values_match
from gateway.providers.openapi_suite import (
    OpenAPIError,
    _is_link_local_host,
    _resolve_ref,
)
from gateway.runtime.gateway import _confirm_key
from gateway.planning.prechecks import _string_entailed
from gateway.runtime.audit import AuditLog
from gateway.ingress.agent_channel import message_from_dict
from gateway.ingress.agent_channel import AgentChannel, ToolCallMessage
from gateway.runtime.gateway import Gateway
from gateway.runtime.policy import PolicySpec
from gateway.planning.planner import PlanDraft
from pauth.suites.shopping import build_suite as build_shopping_suite
from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec
from gateway.providers.registry import merge_suites
from gateway.providers.suite_filter import SuiteFilter
from gateway.serving.config import LoadedConfig, prompt_suite_loader_for

TOOLS = {"get_val", "get_product", "add_to_cart", "send", "send_money"}


def _prepare(code: str) -> None:
    func = parse_and_validate(code)
    func = strip_dead_code(func, TOOLS)
    validate_semantics(func, TOOLS)


# --- sandbox escape (RCE) --------------------------------------------------

def test_dunder_attribute_access_is_rejected():
    # x.__getattr__.__globals__['__builtins__'] reaches real builtins under exec.
    code = (
        "def run():\n"
        "    x = get_val()\n"
        "    f = x.__getattr__\n"
        "    send(f)\n"
    )
    with pytest.raises(DSLRejectionError):
        _prepare(code)


def test_class_dunder_access_is_rejected():
    code = "def run():\n    x = get_val()\n    c = x.__class__\n    send(c)\n"
    with pytest.raises(DSLRejectionError):
        _prepare(code)


def test_shadowing_a_tool_name_is_rejected():
    # send = <callable> then send(...) would call an arbitrary callable.
    code = "def run():\n    x = get_val()\n    send = x\n    send(x)\n"
    with pytest.raises(DSLRejectionError):
        _prepare(code)


def test_keyword_args_on_tool_call_are_rejected():
    # kwargs escape the positional-only slicer + taint gate.
    code = "def run():\n    send_money(recipient=\"x\", amount=1)\n"
    with pytest.raises(DSLRejectionError):
        _prepare(code)


def test_benign_field_access_still_accepted():
    code = "def run():\n    p = get_product()\n    add_to_cart(p.name, 1)\n"
    _prepare(code)  # must not raise


@pytest.mark.parametrize(
    "code",
    [
        "@get_val()\ndef run():\n    pass\n",
        "def run(x=1):\n    send(x)\n",
        "def run(*args):\n    pass\n",
        "def run() -> int:\n    pass\n",
        "def run():\n    send(x)\n    x = get_val()\n",
        "def run():\n    xs = get_val()\n    for send in xs:\n        send(send)\n",
    ],
)
def test_implicit_or_noncausal_run_behavior_is_rejected(code):
    with pytest.raises(DSLRejectionError):
        _prepare(code)


# --- exact operand equality ------------------------------------------------

def test_values_match_does_not_use_python_bool_numeric_equality():
    assert not values_match(True, 1)
    assert not values_match(False, 0)
    assert not values_match({"ok": True}, {"ok": 1})
    assert not values_match([False], [0])


def test_values_match_float_tolerance_is_representation_scale_only():
    assert values_match(1.0, 1.0)
    assert values_match(1.0, 1.0 + 2 * math.ulp(1.0))
    assert not values_match(1_000_000_000.0, 1_000_000_000.5)


# --- confirmation-gate laundering -----------------------------------------

def test_confirm_key_distinguishes_int_float_bool():
    keys = {_confirm_key(1), _confirm_key(1.0), _confirm_key(True)}
    assert len(keys) == 3  # approving one must not bless the others
    assert _confirm_key(1) == _confirm_key(1)  # stable for the same value


def test_destination_entailment_rejects_substrings():
    assert not _string_entailed("alice@example.com", "send to malice@example.com")
    assert _string_entailed("alice@example.com", "send to alice@example.com.")
    assert not _string_entailed(
        "GB33BUKB20201555555555", "GB33BUKB2020155555555599"
    )
    assert _string_entailed(
        "GB33BUKB20201555555555", "IBAN GB33 BUKB 2020 1555 5555 55."
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "tool_call", "tool": "send", "args": None},
        {"kind": "tool_call", "tool": "send", "kwargs": []},
        {"kind": "tool_call", "tool": "", "args": []},
        {"kind": "prompt", "prompt": None},
        {"kind": "prompt", "prompt": "x", "strategy": 7},
        {"kind": "prompt", "prompt": "x", "max_retries": "1000000"},
        {"kind": "prompt", "prompt": "x", "enable_judge": 1},
    ],
)
def test_malformed_wire_messages_are_rejected_without_coercion(payload):
    assert message_from_dict(payload) is None


def test_agent_channel_rejects_non_object_and_malformed_typed_messages():
    channel = AgentChannel(_shopping_loader)
    assert channel.receive_json([])["kind"] == "error"  # type: ignore[arg-type]
    accepted = channel.receive_json(
        {
            "kind": "prompt",
            "prompt": (
                'If the product "USB-C Charging Cable" is in stock and costs '
                "less than $20.00, add 1 to my cart and pay the cart total to "
                "IBAN GB33BUKB20201555555555 with subject \"Order payment\" on "
                "2026-08-27."
            ),
            "strategy": "deterministic",
        }
    )
    assert accepted["accepted"]
    response = channel.receive(
        ToolCallMessage(tool="add_to_cart", args=None)  # type: ignore[arg-type]
    )
    assert response.kind == "error"


def test_audit_log_snapshots_nested_args_and_serializes_sequence():
    import concurrent.futures

    log = AuditLog()
    mutable = [{"nested": [1]}]
    log.record("tool_call", "permit", args=mutable)
    mutable[0]["nested"].append(2)
    assert log.events()[0].args == [{"nested": [1]}]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: log.record("tool_call", "deny", args=[index]),
                range(100),
            )
        )
    assert [event.seq for event in log.events()] == list(range(101))


class _StaticPlanner:
    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    def generate(self, prompt, suite_loader):
        self.calls += 1
        return PlanDraft(
            suite_name="shopping",
            code=self.code,
            run_doc={"code": self.code},
            reason="static test plan",
        )


def _shopping_loader(name):
    if name != "shopping":
        raise ValueError(name)
    return build_shopping_suite()


def test_gateway_itself_enforces_plan_once_and_preserves_first_plan():
    first = _StaticPlanner(
        'def run():\n    add_to_cart("USB-C Charging Cable", 1)\n'
    )
    second = _StaticPlanner(
        'def run():\n    add_to_cart("Studio Monitor Headphones", 1)\n'
    )
    gateway = Gateway(_shopping_loader)
    assert gateway.submit_user_prompt_with_planner(
        "Add one USB-C Charging Cable to my cart.", first
    ).accepted
    rejected = gateway.submit_user_prompt_with_planner("replace plan", second)
    assert not rejected.accepted
    assert "plan-once" in rejected.reason
    assert first.calls == 1 and second.calls == 0
    assert gateway.handle_tool_call(
        "add_to_cart", ["USB-C Charging Cable", 1]
    ).permit


def test_operand_policy_uses_full_bounded_loop_enforcement_path():
    code = (
        "def run():\n"
        "    products = list_products(None, 30.0)\n"
        "    for product in products:\n"
        "        add_to_cart(product.name, 1)\n"
    )
    policy = PolicySpec.from_param_names(
        {"add_to_cart": ["quantity"]},
        build_shopping_suite().tool_params(),
    )
    gateway = Gateway(_shopping_loader, operand_policy=policy)
    assert gateway.submit_user_prompt_with_planner(
        "List products below 30 dollars and add each one.", _StaticPlanner(code)
    ).accepted
    assert gateway.handle_tool_call("list_products", [None, 30.0]).permit
    # Only the declared quantity is free; collection membership and replay
    # accounting still come from the base Enforcer implementation.
    assert gateway.handle_tool_call(
        "add_to_cart", ["Basic Wired Earbuds", 99]
    ).permit
    assert not gateway.handle_tool_call(
        "add_to_cart", ["Studio Monitor Headphones", 99]
    ).permit


def test_configured_suite_filter_narrows_the_merged_planner_universe():
    def source(name: str, tool: str) -> SuiteSpec:
        spec = ToolSpec(
            name=tool,
            params=[],
            signer=name,
            doc=ToolDoc(
                name=tool,
                description=f"Perform the {name} action.",
                parameters=[],
                returns="object",
            ),
        )
        return SuiteSpec(
            name=name,
            tools={tool: spec},
            make_env=object,
            tool_executor_factory=lambda _env: (lambda _tool, _kwargs: {}),
            tasks=[],
        )

    sources = {
        "alpha": source("alpha", "alpha_action"),
        "beta": source("beta", "beta_action"),
    }
    loaded = LoadedConfig(
        merged_name="all",
        sources=sources,
        merged=merge_suites("all", sources),
        policy=PolicySpec({}),
        suite_filter=SuiteFilter(top_k=1, min_score=1),
    )
    selected = prompt_suite_loader_for(loaded)("perform alpha", "all")
    assert selected.tool_names() == {"alpha_action"}


# --- OpenAPI SSRF / DoS ----------------------------------------------------

def test_integer_encoded_metadata_ip_is_blocked():
    assert _is_link_local_host("2852039166")   # 169.254.169.254 decimal
    assert _is_link_local_host("0xA9FEA9FE")   # hex
    assert not _is_link_local_host("127.0.0.1")       # loopback allowed by design
    assert not _is_link_local_host("2130706433")      # decimal loopback allowed


def test_cyclic_ref_is_rejected():
    doc = {"components": {"schemas": {"A": {"$ref": "#/components/schemas/A"}}}}
    with pytest.raises(OpenAPIError):
        _resolve_ref(doc, {"$ref": "#/components/schemas/A"})
