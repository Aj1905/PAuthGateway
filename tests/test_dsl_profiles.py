"""G1 (paper Appendix A DSL as published) vs G2 (extended, default) profiles."""

from __future__ import annotations

import pytest

from pauth import prepare
from pauth.dsl_validator import (
    DSL_PROFILE_PAPER,
    DSLRejectionError,
    parse_and_validate,
)

_TOOLS = {"read_message", "send_money", "notify"}
_SIGNER = {t: "tiny" for t in _TOOLS}


_PAPER_OK = (
    "def run():\n"
    "    m = read_message()\n"
    "    if m.amount <= 100:\n"
    '        send_money(m.iban, m.amount, "Order", "2024-01-01")\n'
)

_PAPER_HELPER_OK = (
    "def run():\n"
    "    messages = read_message()\n"
    '    selected = first(messages, predicate=lambda message: message.kind == "invoice")\n'
    "    notify(selected.id)\n"
)


def test_g1_accepts_flat_paper_program_end_to_end():
    parse_and_validate(_PAPER_OK, profile=DSL_PROFILE_PAPER)
    prepared = prepare(_PAPER_OK, _TOOLS, _SIGNER, dsl_profile="g1")
    assert prepared.rules


def test_g1_accepts_appendix_helper_lambda_end_to_end():
    prepared = prepare(
        _PAPER_HELPER_OK,
        _TOOLS,
        _SIGNER,
        dsl_profile="g1",
    )
    assert prepared.rules


def test_sum_is_g2_only():
    code = (
        "def run():\n"
        "    messages = read_message()\n"
        "    total = sum(messages)\n"
        "    notify(total)\n"
    )
    with pytest.raises(DSLRejectionError, match="forbidden in G1"):
        prepare(code, _TOOLS, _SIGNER, dsl_profile="g1")
    assert prepare(code, _TOOLS, _SIGNER, dsl_profile="g2").rules


@pytest.mark.parametrize(
    "helper",
    [
        "max(messages)",
        "first(messages, key=lambda message: True)",
        "len(messages, key=lambda message: message.id)",
        "min(messages, key=lambda a, b: a.id)",
    ],
)
def test_malformed_helper_calls_are_rejected(helper):
    code = (
        "def run():\n"
        "    messages = read_message()\n"
        f"    selected = {helper}\n"
        "    notify(selected)\n"
    )
    with pytest.raises(DSLRejectionError):
        prepare(code, _TOOLS, _SIGNER, dsl_profile="g2")


@pytest.mark.parametrize(
    "code,fragment",
    [
        (
            "def run():\n    m = read_message()\n    for x in m.items:\n        notify(x)\n",
            "for-loops",
        ),
        ('def run():\n    m = read_message()\n    notify([i for i in m.items])\n', "comprehensions"),
        ('def run():\n    notify({"k": 1})\n', "dict literals"),
        (
            "def run():\n    m = read_message()\n"
            "    if m.ok == 1:\n        notify(m.a)\n    else:\n        notify(m.b)\n",
            "else / elif",
        ),
        (
            "def run():\n    m = read_message()\n"
            "    if m.ok == 1:\n        if m.big == 1:\n            notify(m.a)\n",
            "nested if",
        ),
        (
            'def run():\n    x = ""\n    m = read_message()\n'
            "    if m.ok == 1:\n        x = m.a\n    notify(x)\n",
            "re-assigned",
        ),
    ],
)
def test_g1_rejects_repo_extensions(code, fragment):
    with pytest.raises(DSLRejectionError, match=fragment):
        parse_and_validate(code, profile=DSL_PROFILE_PAPER)
    # The same code passes under the default G2 grammar.
    parse_and_validate(code)


def test_unknown_profile_is_an_error():
    with pytest.raises(ValueError, match="unknown DSL profile"):
        parse_and_validate(_PAPER_OK, profile="g9")
