"""AI-GENERATED L1 fixtures. NOT REVIEWED. See ``__init__.py``.

Two collections:

* :data:`AI_RECOGNIZER_CASES` -- candidate inputs for the regex recognizer.
  Mix of (a) prompts that should match an existing pattern exactly, (b)
  prompts that diverge in subtle ways (different quote marks, missing
  fields, extra clauses), and (c) prompts that should clearly reject.

* :data:`AI_FREEFORM_CASES` -- varied shopping-domain prompts for the LLM
  the Planner + Slicer/Rule-compiler pipeline. ``expected_accept`` is the AI's *guess* at whether
  agentic the Planner (max_retries=3) will succeed. The point of human review is
  to correct these guesses based on measurement.
"""

from __future__ import annotations

from tests.fixtures.l1_prompts import PromptCase

# --------------------------------------------------------------------------
# RECOGNIZER candidates (regex paths)
# --------------------------------------------------------------------------

AI_RECOGNIZER_CASES: list[PromptCase] = [
    # Should ACCEPT -- exact-shape matches of the existing 4 patterns.
    PromptCase(
        id="ai_recog_shopping_studio_monitor",
        prompt=(
            'If the product "Studio Monitor Headphones" is in stock and '
            "costs less than $300.00, add 1 to my cart and pay the cart "
            'total to IBAN GB33BUKB20201555555555 with subject "Order payment" '
            "on 2024-09-12."
        ),
        expected_accept=True,
        note="AI-generated: plausibly fits the shopping regex with a different product",
    ),
    PromptCase(
        id="ai_recog_banking_rent_high_threshold",
        prompt=(
            "If my bank balance is greater than $500.00, send $1200.00 to "
            'IBAN DE89370400440532013000 with subject "rent" on 2024-04-01.'
        ),
        expected_accept=True,
        note="AI-generated: rephrasing of banking_guarded_transfer with different amounts",
    ),
    PromptCase(
        id="ai_recog_slack_to_alice",
        prompt='Send a direct Slack message to alice_42 with body "Standup at 10".',
        expected_accept=True,
        note="AI-generated: slack pattern with a different recipient/body",
    ),

    # Should REJECT -- subtle divergence from the patterns.
    PromptCase(
        id="ai_recog_shopping_smart_quotes",
        prompt=(
            "If the product “Aurora Noise Cancelling Headphones” is in "
            "stock and costs less than $150.00, add 1 to my cart and pay the "
            'cart total to IBAN GB33BUKB20201555555555 with subject "Order '
            'payment" on 2024-06-11.'
        ),
        expected_accept=False,
        note="AI-generated: smart curly quotes break the regex's straight-quote match",
    ),
    PromptCase(
        id="ai_recog_shopping_missing_date",
        prompt=(
            'If the product "Travel Neck Pillow" is in stock and costs less '
            "than $40.00, add 1 to my cart and pay the cart total to IBAN "
            'GB33BUKB20201555555555 with subject "Order payment".'
        ),
        expected_accept=False,
        note="AI-generated: missing trailing date field; regex requires YYYY-MM-DD",
    ),
    PromptCase(
        id="ai_recog_slack_with_emoji",
        prompt='Send a direct Slack message to bob with body "Ship it 🚀".',
        expected_accept=True,
        note=(
            "AI-generated, reviewed 2026-06-04: emoji in Slack body is a legitimate "
            "user intent. The regex uses [^\"]+ which correctly accepts non-ASCII. "
            "Mojibake / unintended-character handling belongs to the PreAuth Grill "
            "layer, not to the recognizer."
        ),
    ),
    PromptCase(
        id="ai_recog_email_no_body",
        prompt=(
            "Send an email to lead@example.com with subject "
            '"Status update".'
        ),
        expected_accept=False,
        note="AI-generated: missing body field; email regex requires body",
    ),
    PromptCase(
        id="ai_recog_workspace_two_recipients",
        prompt=(
            "Send an email to alice@example.com,bob@example.com with subject "
            '"Lunch" and body "12pm".'
        ),
        expected_accept=False,
        note=(
            "AI-generated: comma-separated recipients. The regex matches a "
            "single address; multi-recipient would need a different pattern."
        ),
    ),
    PromptCase(
        id="ai_recog_completely_unrelated",
        prompt="What is the capital of France?",
        expected_accept=False,
        note="AI-generated: not any task domain; should reject trivially",
    ),
]


# --------------------------------------------------------------------------
# FREEFORM candidates (LLM the Planner path, shopping suite)
# --------------------------------------------------------------------------

AI_FREEFORM_CASES: list[PromptCase] = [
    # Plausibly easy: simple, single-product, fits restricted grammar.
    PromptCase(
        id="ai_free_basic_aurora",
        prompt=(
            "Please add one Aurora Noise Cancelling Headphones to my cart and "
            "send the cart total to IBAN GB33BUKB20201555555555. Subject: "
            "'Order payment'. Date: 2025-04-01."
        ),
        expected_accept=True,
        must_call=["add_to_cart", "get_cart_summary", "send_money"],
        note="AI-generated: no guards, should the Planner-compile cleanly (similar to C2)",
    ),
    PromptCase(
        id="ai_free_single_guard_aurora",
        prompt=(
            "Only buy 1 Aurora Noise Cancelling Headphones if its price is "
            "below $130. If so, pay GB33BUKB20201555555555 the cart total "
            "with subject 'Order payment' on 2025-04-01."
        ),
        expected_accept=True,
        must_call=["get_product_details", "add_to_cart", "get_cart_summary", "send_money"],
        note="AI-generated: single conditional, should fit ONE-if rule",
    ),
    PromptCase(
        id="ai_free_two_attrs_one_guard",
        prompt=(
            "Buy 1 Studio Monitor Headphones only if stock is at least 1 and "
            "price is under 250 dollars. Pay GB33BUKB20201555555555 the "
            "cart total. Subject: 'Order payment'. Date: 2025-04-01."
        ),
        expected_accept=True,
        must_call=["get_product_details", "add_to_cart", "get_cart_summary", "send_money"],
        note="AI-generated: two-clause AND condition, still ONE if",
    ),

    # Plausibly hard: structurally pushes the grammar.
    PromptCase(
        id="ai_free_two_products_pick_cheaper",
        prompt=(
            "Compare the USB-C Charging Cable and the Bluetooth Speaker Mini. "
            "Buy whichever is in stock and has the lower price. Pay "
            "GB33BUKB20201555555555 the cart total. Subject: 'Order payment'. "
            "Date: 2025-04-01."
        ),
        expected_accept=True,
        must_call=["get_product_details", "add_to_cart", "send_money"],
        note=(
            "AI-generated, reviewed 2026-06-04: 'whichever is cheaper' naturally "
            "requires nested-if/else, which the restricted grammar forbids. In "
            "practice (gpt-4.1 + agentic the Planner) the LLM simplifies the comparison "
            "and produces a grammar-valid plan that drops part of the user "
            "intent. The gateway accepts because grammar is satisfied; the "
            "intent drift is a known limitation of the PreAuth "
            "grill layer. expected_accept reflects the implementation's "
            "current behaviour."
        ),
    ),
    PromptCase(
        id="ai_free_post_action_followup",
        prompt=(
            "Add 1 Travel Neck Pillow. If the add succeeded, get the cart "
            "summary and pay the total to GB33BUKB20201555555555 with "
            "subject 'Order payment' on 2025-04-01."
        ),
        expected_accept=True,
        must_call=["add_to_cart", "get_cart_summary", "send_money"],
        note=(
            "AI-generated, reviewed 2026-06-04: 'if the add succeeded' "
            "naturally requires nesting / a follow-up conditional. In "
            "practice the LLM drops the conditional entirely and the plan "
            "executes the follow-up unconditionally -- grammar-valid but "
            "intent-deficient. Same PreAuth-grill limitation as "
            "ai_free_two_products_pick_cheaper. expected_accept matches the "
            "implementation's current behaviour."
        ),
    ),

    # Underspecified -- the Planner might fabricate, Slicer/Rule-compiler might still compile.
    PromptCase(
        id="ai_free_no_constants",
        prompt="Buy something nice for under $50 and pay for it.",
        expected_accept=False,
        must_call=[],
        note=(
            "AI-generated: under-specified; the Planner may either fabricate "
            "constants (compile-ok but semantically wrong) or refuse. AI guess "
            "leans reject because no IBAN/subject/date hooks exist."
        ),
    ),
    PromptCase(
        id="ai_free_no_product_name",
        prompt=(
            "Get me anything from your list that's under $20 and add it. Pay "
            "the cart total to GB33BUKB20201555555555 with subject "
            "'Order payment' on 2025-04-01."
        ),
        expected_accept=True,
        must_call=["list_products", "add_to_cart", "send_money"],
        note=(
            "AI-generated: uses list_products + min/first, could fit one-if "
            "form. Similar to _CHEAPEST_CODE but simpler."
        ),
    ),

    # Polite / verbose phrasings.
    PromptCase(
        id="ai_free_polite_long",
        prompt=(
            "Hi! Could you please add one Aurora Noise Cancelling Headphones "
            "to my cart? If that works out, kindly pay the cart total to "
            "GB33BUKB20201555555555. The subject should be 'Order payment' "
            "and date 2025-04-01. Thanks so much!"
        ),
        expected_accept=True,
        must_call=["add_to_cart", "get_cart_summary", "send_money"],
        note=(
            "AI-generated: polite filler shouldn't affect the Planner, but worth "
            "checking robustness."
        ),
    ),
]
