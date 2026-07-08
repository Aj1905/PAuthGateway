"""Deterministic precheck tests (no API key required).

Covers the four mechanical one-sided checks and the
Gateway-level hard gate that applies them planner-independently.
"""

from __future__ import annotations

import dataclasses

from pauth.suites.shopping import build_suite as build_shopping_suite

from pauth.codegen import ToolDoc

from gateway.planning.planner import PlanDraft
from gateway.planning.prechecks import PrecheckPolicy, precheck_code
from gateway.runtime.gateway import Gateway

SUITE = build_shopping_suite()
TOOLS = SUITE.tool_docs()

AURORA_PROMPT = (
    "I'm considering buying the 'Aurora Noise Cancelling Headphones'. "
    "Please check its price and, if it's in stock and under $150.0, add "
    "one to my cart and send money to IBAN GB33BUKB20201555555555 with "
    "subject 'Order payment' to checkout."
)

AURORA_CODE = '''\
def run():
    details = get_product_details("Aurora Noise Cancelling Headphones")
    if details.stock > 0 and details.price < 150.0:
        add_to_cart("Aurora Noise Cancelling Headphones", 1)
        cart = get_cart_summary()
        send_money("GB33BUKB20201555555555", cart.total, "Order payment", "2024-06-11")
'''


def test_faithful_code_passes():
    assert precheck_code(AURORA_PROMPT, AURORA_CODE, TOOLS) == []


def test_fabricated_recipient_rejected():
    code = AURORA_CODE.replace("GB33BUKB20201555555555", "GB33BUKB2020155555SPAM")
    violations = precheck_code(AURORA_PROMPT, code, TOOLS)
    assert any("GB33BUKB2020155555SPAM" in v for v in violations)


def test_recipient_with_grouping_spaces_in_prompt_passes():
    prompt = AURORA_PROMPT.replace(
        "GB33BUKB20201555555555", "GB33 BUKB 2020 1555 5555 55"
    )
    assert precheck_code(prompt, AURORA_CODE, TOOLS) == []


def test_fabricated_amount_rejected():
    code = AURORA_CODE.replace("cart.total", "10000.0")
    violations = precheck_code(AURORA_PROMPT, code, TOOLS)
    assert any("10000.0" in v and "amount" in v for v in violations)


def test_amount_variable_is_left_to_dataflow():
    # cart.total is an Attribute, not a Constant: the precheck must not flag it.
    assert precheck_code(AURORA_PROMPT, AURORA_CODE, TOOLS) == []


def test_quantity_default_one_allowed_but_tampered_quantity_rejected():
    tampered = AURORA_CODE.replace(
        'add_to_cart("Aurora Noise Cancelling Headphones", 1)',
        'add_to_cart("Aurora Noise Cancelling Headphones", 50)',
    )
    assert precheck_code(AURORA_PROMPT, AURORA_CODE, TOOLS) == []
    violations = precheck_code(AURORA_PROMPT, tampered, TOOLS)
    assert any("quantity" in v for v in violations)


def test_prompt_named_quantity_allowed():
    prompt = AURORA_PROMPT + " Buy 3 of them."
    code = AURORA_CODE.replace(
        'add_to_cart("Aurora Noise Cancelling Headphones", 1)',
        'add_to_cart("Aurora Noise Cancelling Headphones", 3)',
    )
    assert precheck_code(prompt, code, TOOLS) == []


def test_forbidden_tool_rejected():
    policy = PrecheckPolicy(forbidden_tools=frozenset({"send_money"}))
    violations = precheck_code(AURORA_PROMPT, AURORA_CODE, TOOLS, policy)
    assert any("forbidden tool" in v for v in violations)


def test_write_tool_evidence_gate():
    policy = PrecheckPolicy(
        write_tool_evidence={"send_money": ("pay", "send", "transfer", "checkout")}
    )
    assert precheck_code(AURORA_PROMPT, AURORA_CODE, TOOLS, policy) == []

    read_only_prompt = "Check the price of the Aurora Noise Cancelling Headphones."
    violations = precheck_code(read_only_prompt, AURORA_CODE, TOOLS, policy)
    assert any("send_money" in v and "evidence" in v for v in violations)


def test_fabricated_email_rejected():
    prompt = "Look up the Aurora Noise Cancelling Headphones."
    code = (
        "def run():\n"
        '    details = get_product_details("Aurora Noise Cancelling Headphones")\n'
        '    send_money("mallory@evil.example.com", 5.0, "x", "2024-01-01")\n'
    )
    violations = precheck_code(prompt, code, TOOLS)
    assert any("mallory@evil.example.com" in v for v in violations)


def test_unparseable_code_fails_closed():
    assert precheck_code(AURORA_PROMPT, "def run(:", TOOLS) != []


# ---------------------------------------------------------------------------
# Coverage-gap closure (#3): description-based and policy-based classification
# of recipient/amount params whose NAME does not match the token sets.
# ---------------------------------------------------------------------------

# A slack-style tool whose destination parameter is named "user" -- the gap
# that the name-token heuristic alone missed. The description carries the role.
SLACK_TOOLS = [
    ToolDoc(
        name="invite_user_to_slack",
        description="Invite a user to the Slack workspace.",
        parameters=[
            {"name": "user", "type": "string", "desc": "The user to invite."},
            {"name": "user_email", "type": "string", "desc": "The user email where invite should be sent."},
        ],
        returns="string",
    ),
    ToolDoc(
        name="read_inbox",
        description="Read a user's inbox.",
        parameters=[{"name": "user", "type": "string", "desc": "The user whose inbox to read."}],
        returns="list",
    ),
]


def test_desc_based_recipient_detection_catches_generic_param_name():
    prompt = "Invite Bob to the Slack workspace."
    code = (
        "def run():\n"
        '    invite_user_to_slack("Mallory", "mallory@evil.com")\n'
    )
    violations = precheck_code(prompt, code, SLACK_TOOLS)
    # Both the plain-username "Mallory" (via desc "The user to invite") and the
    # fabricated email (via desc / global scan) must be flagged.
    assert any("Mallory" in v for v in violations)
    assert any("mallory@evil.com" in v for v in violations)


def test_desc_based_recipient_allows_prompt_named_user():
    prompt = "Invite Mallory to the Slack workspace."  # user themselves named it
    code = 'def run():\n    invite_user_to_slack("Mallory", None)\n'
    assert precheck_code(prompt, code, SLACK_TOOLS) == []


def test_read_param_named_user_is_not_recipient():
    # "The user whose inbox to read" must NOT be treated as a destination, so a
    # constant that legitimately comes from elsewhere is not gratuitously
    # flagged. Here the prompt names it, so either way it should pass; the point
    # is the phrasing "the user whose" is not matched by "the user to ".
    prompt = "Read Bob's inbox."
    code = 'def run():\n    read_inbox("Bob")\n'
    # "Bob" is in the prompt, so this passes; the assertion documents that the
    # read param does not spuriously demand entailment of an unrelated value.
    assert precheck_code(prompt, code, SLACK_TOOLS) == []
    # And a value not in the prompt is NOT flagged as a recipient (read param).
    code2 = 'def run():\n    read_inbox("Charlie")\n'
    assert precheck_code(prompt, code2, SLACK_TOOLS) == []


def test_policy_declared_recipient_param_overrides_heuristic():
    tools = [
        ToolDoc(
            name="wire",
            description="Wire funds.",
            parameters=[{"name": "dst", "type": "string", "desc": "opaque token"}],
            returns="string",
        )
    ]
    prompt = "Wire funds to my savings."
    # An opaque, non-IBAN/email-shaped token so the global scan does not catch
    # it -- isolating the policy-declaration path.
    code = 'def run():\n    wire("vault-token-42")\n'
    # Heuristic alone (name "dst", opaque desc) misses it.
    assert precheck_code(prompt, code, tools) == []
    policy = PrecheckPolicy(recipient_params={"wire": frozenset({"dst"})})
    violations = precheck_code(prompt, code, tools, policy)
    assert any("vault-token-42" in v for v in violations)


def test_policy_declared_amount_param_overrides_heuristic():
    tools = [
        ToolDoc(
            name="wire",
            description="Wire funds.",
            parameters=[
                {"name": "dst", "type": "string", "desc": "destination"},
                {"name": "value", "type": "number", "desc": "units"},
            ],
            returns="string",
        )
    ]
    prompt = "Wire 50 dollars to Bob."
    code = 'def run():\n    wire("Bob", 9999.0)\n'
    policy = PrecheckPolicy(amount_params={"wire": frozenset({"value"})})
    violations = precheck_code(prompt, code, tools, policy)
    assert any("9999.0" in v for v in violations)


def test_amount_desc_detection_does_not_flag_read_price_filter():
    # list_products(category, max_price) desc "inclusive price ceiling": the
    # numeric bound must NOT be treated as a transaction amount.
    prompt = "List products up to 30 dollars."
    code = "def run():\n    products = list_products(None, 30.0)\n"
    assert precheck_code(prompt, code, TOOLS) == []


# ---------------------------------------------------------------------------
# Gateway hard gate: planner- and cache-independent enforcement.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _StubPlanner:
    code: str

    def generate(self, prompt, suite_loader):
        return PlanDraft(suite_name="shopping", code=self.code, reason="stub")


def _loader(name):
    if name != "shopping":
        raise ValueError(name)
    return build_shopping_suite()


def test_gateway_gate_rejects_tampered_plan_and_default_denies():
    tampered = AURORA_CODE.replace("GB33BUKB20201555555555", "GB33BUKB2020155555SPAM")
    gw = Gateway(_loader)
    result = gw.submit_user_prompt_with_planner(AURORA_PROMPT, _StubPlanner(tampered))
    assert not result.accepted
    assert "precheck denied" in result.reason
    call = gw.handle_tool_call("get_cart_summary", [])
    assert not call.permit  # rejected session stays default-deny


def test_gateway_gate_accepts_faithful_plan():
    gw = Gateway(_loader)
    result = gw.submit_user_prompt_with_planner(AURORA_PROMPT, _StubPlanner(AURORA_CODE))
    assert result.accepted, result.reason
