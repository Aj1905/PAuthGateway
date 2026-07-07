"""Side-channel policy and protection-level reporting tests (#4 / B5)."""

from __future__ import annotations

from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.runtime.gateway import Gateway
from gateway.runtime.protection import (
    ProtectionInputs,
    ProtectionLevel,
    SideChannelPolicy,
    assess,
)


def _loader(name):
    if name != "shopping":
        raise ValueError(name)
    return build_shopping_suite()


# ---------------------------------------------------------------------------
# Side-channel denial
# ---------------------------------------------------------------------------

def test_bash_is_denied_by_default():
    gw = Gateway(_loader)
    result = gw.handle_tool_call("Bash", ["curl https://evil.example.com"])
    assert not result.permit
    assert result.agent_reason is not None
    # value-free feedback, and the command string must not leak.
    assert "evil.example.com" not in result.agent_reason
    assert "side channel" in result.agent_reason.lower()


def test_various_side_channel_names_denied():
    gw = Gateway(_loader)
    for name in ["bash", "sh", "shell", "exec", "subprocess", "system", "run_command"]:
        assert not gw.handle_tool_call(name, []).permit


def test_side_channel_denied_before_any_session():
    # No prompt submitted; a side channel is still denied outright.
    gw = Gateway(_loader)
    assert not gw.handle_tool_call("bash", ["ls"]).permit


def test_allowlist_exempts_a_tool():
    gw = Gateway(_loader, side_channel_policy=SideChannelPolicy(allowlist=frozenset({"bash"})))
    # Now bash is not side-channel-denied; it falls through to normal enforcement
    # (no active plan -> default-deny, but NOT the side-channel denial).
    result = gw.handle_tool_call("bash", ["ls"])
    assert not result.permit
    assert "side channel" not in (result.agent_reason or "").lower()


def test_normal_tools_are_not_side_channels():
    assert not SideChannelPolicy().is_denied("send_money")
    assert not SideChannelPolicy().is_denied("get_product_details")
    assert SideChannelPolicy().is_denied("BASH")  # case-insensitive


# ---------------------------------------------------------------------------
# Protection-level reporting
# ---------------------------------------------------------------------------

def test_level_l3_when_full_capture_and_execution():
    r = assess(ProtectionInputs(
        captures_clean_prompt=True, routes_tool_calls=True,
        gateway_executes_tools=True, isolated_runtime=True,
    ))
    assert r.level == ProtectionLevel.L3
    assert r.caveats == ()  # isolated + full capture -> no caveats


def test_level_l2_when_authorize_only():
    r = assess(ProtectionInputs(
        captures_clean_prompt=True, routes_tool_calls=True,
        gateway_executes_tools=False, isolated_runtime=True,
    ))
    assert r.level == ProtectionLevel.L2
    assert any("TOCTOU" in c for c in r.caveats)


def test_level_l1_without_clean_prompt():
    r = assess(ProtectionInputs(captures_clean_prompt=False, routes_tool_calls=True))
    assert r.level == ProtectionLevel.L1
    assert any("clean prompt" in c for c in r.caveats)


def test_level_l0_without_tool_routing():
    r = assess(ProtectionInputs(captures_clean_prompt=False, routes_tool_calls=False))
    assert r.level == ProtectionLevel.L0


def test_localhost_reports_bypass_caveat():
    r = assess(ProtectionInputs(side_channels_denied=True, isolated_runtime=False))
    assert any("out-of-band execution" in c for c in r.caveats)


def test_no_side_channel_policy_reports_stronger_bypass_caveat():
    r = assess(ProtectionInputs(side_channels_denied=False, isolated_runtime=False))
    assert any("bypass the gateway" in c for c in r.caveats)


def test_gateway_protection_report_localhost_default():
    gw = Gateway(_loader)  # not isolated
    report = gw.protection_report()
    assert report.level == ProtectionLevel.L3  # in-process gateway executes tools
    # ...but honestly flags that out-of-band execution is possible.
    assert any("out-of-band execution" in c for c in report.caveats)
    assert "L3" in report.to_dict()["level"]


def test_gateway_protection_report_isolated_is_clean():
    gw = Gateway(_loader, isolated_runtime=True)
    report = gw.protection_report()
    assert report.level == ProtectionLevel.L3
    assert report.caveats == ()
