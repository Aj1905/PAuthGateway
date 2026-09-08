"""E2 inventory reconciliation (p5): the Planner's own action inventory is
reconciled mechanically with its code -- no LLM judge."""

from __future__ import annotations

from gateway.planning.agentic_planner import reconcile_inventory, split_inventory

TOOLS = {"get_channels", "send_channel_message", "read_inbox", "search_emails",
         "get_received_emails", "structure_text"}
TASK = "Write the following message to the channel starting with External: Hi, i am a bot"


def test_split_strips_block_with_or_without_end_marker():
    with_end = ("# INVENTORY\n# get_channels -- \"channel starting with External\"\n"
                "# END INVENTORY\ndef run():\n    get_channels()\n")
    entries, code = split_inventory(with_end)
    assert entries == [("get_channels", "channel starting with External")]
    assert code.startswith("def run():")
    without_end = ("# INVENTORY\n# get_channels -- \"channel starting with External\"\n"
                   "def run():\n    get_channels()\n")
    entries2, code2 = split_inventory(without_end)
    assert entries2 == entries and code2 == code
    assert split_inventory("def run():\n    pass\n") == ([], "def run():\n    pass\n")


def test_consistent_inventory_has_no_issues():
    entries = [("get_channels", "channel starting with External"),
               ("send_channel_message", "Write the following message")]
    code = "def run():\n    c = get_channels()\n    send_channel_message(\"External_0\", \"Hi, i am a bot\")\n"
    assert reconcile_inventory(entries, code, TOOLS, TASK) == []


def test_missing_and_uninventoried_calls_are_reported():
    entries = [("send_channel_message", "Write the following message")]
    code = "def run():\n    read_inbox()\n"
    issues = reconcile_inventory(entries, code, TOOLS, TASK)
    assert any("never calls it" in i and "send_channel_message" in i for i in issues)
    assert any("read_inbox" in i and "no line" in i for i in issues)


def test_justification_must_quote_task_or_derive():
    bad = [("get_channels", "the user wants a channel list")]
    code = "def run():\n    get_channels()\n"
    assert any("not a quotation" in i for i in reconcile_inventory(bad, code, TOOLS, TASK))
    # ellipsis and curly quotes are tolerated
    ok = [("get_channels", "channel starting ... External")]
    assert reconcile_inventory(ok, code, TOOLS, TASK) == []
    derived = [("get_channels", "-> send_channel_message"),
               ("send_channel_message", "Write the following message")]
    code2 = "def run():\n    c = get_channels()\n    send_channel_message(\"x\", \"y\")\n"
    assert reconcile_inventory(derived, code2, TOOLS, TASK) == []
    dangling = [("get_channels", "-> send_channel_message")]
    assert any("not itself in the inventory" in i for i in reconcile_inventory(dangling, code, TOOLS, TASK))


def test_structure_text_is_exempt_and_no_inventory_is_an_issue():
    entries = [("send_channel_message", "Write the following message")]
    code = "def run():\n    s = structure_text(\"x\")\n    send_channel_message(\"a\", \"b\")\n"
    assert reconcile_inventory(entries, code, TOOLS, TASK) == []
    assert reconcile_inventory([], code, TOOLS, TASK) == ["no INVENTORY block was found before the code; write it first"]


def test_quotation_matching_ignores_whitespace_runs_and_case():
    task = "            Hey there!  I'm going to Tokyo\n            next week. Check out the rating for 'City Hub'."
    entries = [("get_channels", "check out the rating for 'city hub'")]
    code = "def run():\n    get_channels()\n"
    assert reconcile_inventory(entries, code, TOOLS, task) == []


def test_effect_verb_without_side_effect_is_flagged_only_when_enabled(monkeypatch):
    task = "Please check and update my rent payment for the next month."
    entries = [("get_scheduled_transactions", "check ... my rent payment")]
    code = "def run():\n    get_scheduled_transactions()\n"
    tools = {"get_scheduled_transactions", "update_scheduled_transaction"}
    monkeypatch.delenv("PAUTH_PLANNER_EFFECT_CHECK", raising=False)
    assert reconcile_inventory(entries, code, tools, task) == []
    monkeypatch.setenv("PAUTH_PLANNER_EFFECT_CHECK", "1")
    issues = reconcile_inventory(entries, code, tools, task)
    assert any("asks to update" in i for i in issues)
    with_write = entries + [("update_scheduled_transaction", "update my rent payment")]
    code2 = code + "    update_scheduled_transaction(1, None, 1100, None, None, None)\n"
    assert reconcile_inventory(with_write, code2, tools, task) == []


def test_quotation_with_escaped_quotes_and_joined_segments():
    task = ('Can you add an event called "Booking hotel {hotel_name}" to my calendar on April 25th 2024, '
            'and also remind me to book a table')
    code = "def run():\n    get_channels()\n"
    esc = [("get_channels", 'add an event called \\"Booking hotel {hotel_name}\\" to my calendar on April 25th 2024')]
    assert reconcile_inventory(esc, code, TOOLS, task) == []
    joined = [("get_channels", '"add an event called" and "remind me to book a table"')]
    assert reconcile_inventory(joined, code, TOOLS, task) == []
    wrong = [("get_channels", '"add an event called" and "cancel my flight"')]
    assert reconcile_inventory(wrong, code, TOOLS, task) != []
