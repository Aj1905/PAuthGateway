"""Fail-closed checks for tool calls nested in helper lambdas.

Appendix A shows two such forms, but safely executing them requires an ordered
occurrence-provenance model.  Until that model is complete, both DSL profiles
reject the examples rather than authorize unvisited helper branches.
"""

from __future__ import annotations

import pytest

from pauth import prepare
from pauth.grammar_validator import DSLRejectionError


def _reject(code: str, profile: str, tools: set[str]) -> None:
    with pytest.raises(
        DSLRejectionError,
        match="nested tool results|nested inside an expression",
    ):
        prepare(
            code,
            tools,
            {name: "appendix-suite" for name in tools},
            dsl_profile=profile,
        )


@pytest.mark.parametrize("profile", ["g1", "g2"])
def test_paper_max_helper_tool_form_is_rejected_fail_closed(profile):
    _reject(
        '''def run():
    channels = get_channels()
    selected = max(channels, key=lambda channel: len(get_users_in_channel(channel)))
    send(selected)
''',
        profile,
        {"get_channels", "get_users_in_channel", "send"},
    )


@pytest.mark.parametrize("profile", ["g1", "g2"])
def test_paper_nested_first_tool_form_is_rejected_fail_closed(profile):
    _reject(
        '''def run():
    channels = get_channels()
    selected = first(channels, predicate=lambda channel: first(read_channel_messages(channel), predicate=lambda message: message.sender == "Alice") is not None)
    send(selected)
''',
        profile,
        {"get_channels", "read_channel_messages", "send"},
    )


@pytest.mark.parametrize("profile", ["g1", "g2"])
@pytest.mark.parametrize(
    "predicate",
    [
        "emit(item)",
        "False and emit(item)",
        "emit(item) or inspect(item)",
        "emit(transform(item))",
        "zero_arg_tool()",
    ],
)
def test_unmodelled_helper_tool_forms_are_rejected(profile, predicate):
    _reject(
        f'''def run():
    items = read_items()
    selected = last(items, predicate=lambda item: {predicate})
''',
        profile,
        {"read_items", "emit", "inspect", "transform", "zero_arg_tool"},
    )
