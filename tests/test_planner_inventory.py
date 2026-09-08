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
