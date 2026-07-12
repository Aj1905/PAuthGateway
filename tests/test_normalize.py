"""normalize_run rewrites reject-but-safe code into the slicer's canonical form
without changing behavior."""

from __future__ import annotations

import ast

from pauth.normalize import normalize_run


def _norm(src: str) -> str:
    func = ast.parse(src).body[0]
    return ast.unparse(normalize_run(func))


def test_helper_call_argument_is_hoisted_to_a_temp():
    out = _norm("def run():\n    x = first(get_emails())\n")
    # get_emails() lifted out of the helper arg; first() now takes a bare name.
    assert "get_emails()" in out
    lines = [l.strip() for l in out.splitlines()]
    hoist = next(l for l in lines if l.startswith("_h0 ="))
    assert hoist == "_h0 = get_emails()"
    assert "first(_h0)" in out


def test_nested_calls_hoist_inner_first():
    out = _norm("def run():\n    y = pay(total(get_cart()))\n")
    body = [l.strip() for l in out.splitlines() if l.strip() and not l.strip().startswith("def")]
    # inner get_cart() must be defined before total(...) that uses it
    assert body[0] == "_h0 = get_cart()"
    assert body[1] == "_h1 = total(_h0)"
    assert body[2] == "y = pay(_h1)"


def test_list_wrapped_helper_first_arg_is_hoisted():
    # The common LLM idiom max([x], key=...) violates "first arg is a bare name";
    # lift the list literal to a temp so the canonical helper form remains.
    out = _norm("def run():\n    best = max([hotels_str], key=lambda i: reviews[i])\n")
    assert "_h0 = [hotels_str]" in out
    assert "max(_h0, key=lambda i: reviews[i])" in out


def test_lambda_body_is_not_hoisted():
    out = _norm("def run():\n    x = max(get_items(), key=lambda i: score(i))\n")
    # get_items() (a plain arg) is hoisted; score(i) inside the lambda is NOT.
    assert "_h0 = get_items()" in out
    assert "lambda i: score(i)" in out
    assert "max(_h0, key=lambda i: score(i))" in out


def test_straight_line_reassignment_becomes_ssa():
    out = _norm("def run():\n    content = intro()\n    content = append(content, body())\n")
    # second definition renamed; the use inside it points at the first version.
    # (body() is also hoisted, being a call nested in append(...)'s args.)
    assert "content = intro()" in out
    assert "_h0 = body()" in out
    assert "content_1 = append(content, _h0)" in out


def test_ssa_later_reads_see_the_latest_version():
    out = _norm(
        "def run():\n"
        "    total = base()\n"
        "    total = add(total, tax())\n"
        "    pay(total)\n"
    )
    # tax() hoisted (nested call arg); the reassignment RHS reads the first
    # version (total), the write creates total_1, and the later read sees total_1.
    assert "total_1 = add(total, _h0)" in out
    assert "pay(total_1)" in out


def test_conditional_reassignment_bails_unchanged():
    src = (
        "def run():\n"
        "    content = ''\n"
        "    if flag(x):\n"
        "        content = body()\n"
        "    send(content)\n"
    )
    # A reassignment inside an if-body needs a merge -> pass leaves it untouched.
    out = _norm(src)
    assert "content_1" not in out
    assert out.count("content =") == 2  # both original definitions preserved verbatim


def test_no_op_when_already_canonical():
    src = "def run():\n    x = get_x()\n    send(x)\n"
    assert _norm(src).strip() == ast.unparse(ast.parse(src).body[0]).strip()
