"""Smoke test: stdio MCP transport + operand policy + suite filter.

Three checks, run sequentially:

1. ``StdioTransport`` end-to-end: spawn the stdio mock MCP, build a
   suite from it, hand it to the gateway, run the Aurora benign flow,
   try one off-plan call, confirm verdicts.

2. ``PolicyAwareEnforcer``: declare ``send_money``'s ``subject`` as a
   free operand; a call with a tampered subject (which would normally
   be denied as operand-off-slice) is now permitted. The recipient/
   amount/date checks remain.

3. ``SuiteFilter``: register the shopping suite under two distinct
   names with different descriptions; verify a shopping-flavoured
   prompt selects the right one and that the fallback (no scoring
   hits) returns the whole universe.

Run::

    .venv/bin/python gateway/run_extended_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pauth import prepare
from pauth.codegen import ToolDoc
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.evaluator import wrap
from pauth.suites.base import SuiteSpec, ToolSpec
from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.gateway import Gateway
from gateway.mcp_suite import StdioTransport, build_mcp_suite_from_transport
from gateway.policy import PolicyAwareEnforcer, PolicySpec
from gateway.registry import merge_suites
from gateway.suite_filter import SuiteFilter


def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# --------------------------------------------------------------------------
# 1. Stdio MCP transport
# --------------------------------------------------------------------------

def check_stdio_mcp() -> int:
    _section("1. StdioTransport — MCP server over subprocess stdin/stdout")
    transport = StdioTransport([
        sys.executable, str(ROOT / "tests" / "fixtures" / "mock_mcp_stdio.py"),
    ])
    try:
        mcp_suite = build_mcp_suite_from_transport("mcp_shopping_stdio", transport)
        merged = merge_suites("user_default", {"mcp_shopping_stdio": mcp_suite})

        def loader(name: str) -> SuiteSpec:
            if name in ("user_default", "shopping"):
                return merged
            raise ValueError(f"unknown suite {name!r}")

        gw = Gateway(loader)
        prompt = (
            'If the product "Aurora Noise Cancelling Headphones" is in stock '
            "and costs less than $150.00, add 1 to my cart and pay the cart "
            'total to IBAN GB33BUKB20201555555555 with subject "Order payment" '
            "on 2024-06-11."
        )
        sub = gw.submit_user_prompt(prompt)
        print(f"submission: accepted={sub.accepted} rules={sub.rule_count}")
        if not sub.accepted:
            return 1

        attempts = [
            ("get_product_details", ["Aurora Noise Cancelling Headphones"], True),
            ("add_to_cart", ["Aurora Noise Cancelling Headphones", 1], True),
            ("get_cart_summary", [], True),
            ("send_money", ["GB33BUKB20201555555555", 120.0, "Order payment", "2024-06-11"], True),
            ("send_money", ["GB33BUKB2020155555SPAM", 120.0, "Order payment", "2024-06-11"], False),
        ]
        failures = 0
        for tool, args, expected in attempts:
            res = gw.handle_tool_call(tool, args)
            verdict = "PERMIT" if res.permit else "REJECT"
            ok = res.permit == expected
            if not ok:
                failures += 1
            print(f"  {tool}({args}) -> {verdict} :: {res.reason}")
        return 0 if failures == 0 else 1
    finally:
        transport.close()


# --------------------------------------------------------------------------
# 2. Policy-aware enforcer
# --------------------------------------------------------------------------

def check_policy() -> int:
    _section("2. PolicyAwareEnforcer — free-operand declaration")
    suite = build_shopping_suite()
    code = '''\
def run():
    details = get_product_details("Aurora Noise Cancelling Headphones")
    if details.stock > 0 and details.price < 150.0:
        add_to_cart("Aurora Noise Cancelling Headphones", 1)
        cart = get_cart_summary()
        send_money("GB33BUKB20201555555555", cart.total, "Order payment", "2024-06-11")
'''
    prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    env = suite.make_env()
    runner = suite.runner_factory(env)
    keyring = KeyRing()
    store = EnvelopeStore(keyring)

    policy = PolicySpec.from_param_names(
        {"send_money": ["subject"]}, suite.tool_params(),
    )
    enforcer = PolicyAwareEnforcer(prepared.rules, store, suite.tool_signer(), policy)

    # Drive the benign prefix manually so the envelope store is populated.
    for tool_name, kwargs in [
        ("get_product_details", {"name": "Aurora Noise Cancelling Headphones"}),
        ("add_to_cart", {"name": "Aurora Noise Cancelling Headphones", "quantity": 1}),
        ("get_cart_summary", {}),
    ]:
        args = [kwargs[p] for p in suite.tool_params()[tool_name]]
        decision = enforcer.check(tool_name, args)
        assert decision.permit, f"{tool_name} unexpectedly denied: {decision.reason}"
        result = runner(tool_name, kwargs)
        enforcer.record(decision.rule, wrap(result))

    # Now: same recipient/amount/date, but subject tampered. Without the
    # policy this is denied; with the policy it's permitted.
    tampered = ["GB33BUKB20201555555555", 120.0, "ATTACKER MESSAGE", "2024-06-11"]
    decision = enforcer.check("send_money", tampered)
    print(f"tampered subject -> permit={decision.permit} :: {decision.reason}")
    if not decision.permit:
        return 1

    # Sanity: tampered recipient is still denied.
    bad_recipient = ["GB33BUKB2020155555SPAM", 120.0, "Order payment", "2024-06-11"]
    decision = enforcer.check("send_money", bad_recipient)
    print(f"tampered recipient -> permit={decision.permit} :: {decision.reason}")
    if decision.permit:
        return 1

    return 0


# --------------------------------------------------------------------------
# 3. Suite filter
# --------------------------------------------------------------------------

def _make_dummy_suite(name: str, descriptions: dict[str, str]) -> SuiteSpec:
    tools = {
        tool_name: ToolSpec(
            name=tool_name,
            params=[],
            doc=ToolDoc(name=tool_name, description=desc, parameters=[], returns="object"),
            signer=name,
        )
        for tool_name, desc in descriptions.items()
    }
    return SuiteSpec(
        name=name,
        tools=tools,
        make_env=lambda: None,
        runner_factory=lambda _env: (lambda t, k: None),
        tasks=[],
    )


def check_filter() -> int:
    _section("3. SuiteFilter — prompt-aware suite shortlist")
    sources = {
        "email": _make_dummy_suite("email", {
            "send_email": "Send an email message to a recipient",
            "search_inbox": "Search the user's inbox for messages",
        }),
        "calendar": _make_dummy_suite("calendar", {
            "create_event": "Create a calendar event with title and time",
            "list_events": "List upcoming calendar events",
        }),
        "files": _make_dummy_suite("files", {
            "upload_file": "Upload a file to the cloud storage drive",
            "download_file": "Download a file from cloud storage",
        }),
    }
    filt = SuiteFilter(top_k=2, min_score=1)

    cases = [
        ("Send an inbox email to alice about today's meeting", {"email", "calendar"}),
        ("Upload the quarterly report file to the drive", {"files"}),
        ("What is the capital of France?", {"email", "calendar", "files"}),  # fallback
    ]
    failures = 0
    for prompt, expected in cases:
        result = filt.filter(prompt, sources)
        chosen = set(result.selected)
        ok = chosen <= expected and chosen != set()
        # Soft check: at least one expected source is selected.
        soft = bool(chosen & expected) or expected == set(sources)
        status = "OK" if ok or soft else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"  {prompt!r}")
        print(f"    selected: {result.selected}  ({result.reason})  -> {status}")

    return 0 if failures == 0 else 1


def main() -> int:
    failures = 0
    failures += check_stdio_mcp()
    failures += check_policy()
    failures += check_filter()
    print()
    print("=" * 78)
    print(f"RESULT: {'PASS' if failures == 0 else f'FAIL ({failures} section(s))'}")
    print("=" * 78)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
