"""Regression tests for the security audit fixes (OSS core).

Each test would FAIL against the pre-fix code and documents the exploit it closes.
"""

from __future__ import annotations

import pytest

from pauth.grammar import (
    RestrictedGrammarError,
    parse_and_validate,
    strip_dead_code,
    validate_semantics,
)
from gateway.providers.openapi_suite import (
    OpenAPIError,
    _is_link_local_host,
    _resolve_ref,
)
from gateway.runtime.gateway import _confirm_key

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
    with pytest.raises(RestrictedGrammarError):
        _prepare(code)


def test_class_dunder_access_is_rejected():
    code = "def run():\n    x = get_val()\n    c = x.__class__\n    send(c)\n"
    with pytest.raises(RestrictedGrammarError):
        _prepare(code)


def test_shadowing_a_tool_name_is_rejected():
    # send = <callable> then send(...) would call an arbitrary callable.
    code = "def run():\n    x = get_val()\n    send = x\n    send(x)\n"
    with pytest.raises(RestrictedGrammarError):
        _prepare(code)


def test_keyword_args_on_tool_call_are_rejected():
    # kwargs escape the positional-only slicer + taint gate.
    code = "def run():\n    send_money(recipient=\"x\", amount=1)\n"
    with pytest.raises(RestrictedGrammarError):
        _prepare(code)


def test_benign_field_access_still_accepted():
    code = "def run():\n    p = get_product()\n    add_to_cart(p.name, 1)\n"
    _prepare(code)  # must not raise


# --- confirmation-gate laundering -----------------------------------------

def test_confirm_key_distinguishes_int_float_bool():
    keys = {_confirm_key(1), _confirm_key(1.0), _confirm_key(True)}
    assert len(keys) == 3  # approving one must not bless the others
    assert _confirm_key(1) == _confirm_key(1)  # stable for the same value


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
