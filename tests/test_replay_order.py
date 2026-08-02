"""Replay and call-order enforcement (session-state extension over the paper).

* Replay: a call site (or loop tuple) authorizes at most as many live calls as
  the plan contains. Post-hoc probes (``live=False``, the default) stay blind to
  consumption, preserving the pure-authorization-relation contract that the
  FN=0 tests rely on.
* Order (opt-in ``ordered_tools``): a live side-effecting call requires every
  earlier ordered site to have executed or to be provably off-path.

Offline, no API key.
"""

from __future__ import annotations

from pauth import prepare
from pauth.codegen import ToolDoc
from pauth.enforcer import Enforcer
from pauth.tool_executor import execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.evaluator import wrap
from pauth.suites.base import SuiteSpec, ToolSpec


def _tool(name, params, ret):
    return ToolSpec(name=name, params=params, signer="s",
                    doc=ToolDoc(name=name, description=name,
                                parameters=[{"name": p, "type": "number", "desc": p} for p in params],
                                returns=ret))


_TOOLS = {
    "get_price": _tool("get_price", [], "number"),
    "get_items": _tool("get_items", [], "array of number"),
    "set_limit": _tool("set_limit", ["value"], "object {ok: boolean}"),
    "send": _tool("send", ["amount"], "object {ok: boolean}"),
}


class _Env:
    def __init__(self, price=50, items=(5, 5)):
        self.price = price
        self.items = list(items)
        self.sent: list = []


def _tool_executor(env):
    impl = {
        "get_price": lambda: env.price,
        "get_items": lambda: env.items,
        "set_limit": lambda value: {"ok": True},
        "send": lambda amount: env.sent.append(amount) or {"ok": True},
    }
    return lambda tool, kwargs: impl[tool](**kwargs)


def _suite(env_kwargs=None):
    kw = env_kwargs or {}
    return SuiteSpec(name="ro", tools=_TOOLS, make_env=lambda: _Env(**kw),
                     tool_executor_factory=_tool_executor, tasks=[])


def _run(plan, ordered=None, env_kwargs=None):
    suite = _suite(env_kwargs)
    prepared = prepare(plan, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer(),
                   ordered_tools=ordered)
    report = execute_generated_code(
        prepared.source, enf, suite.tool_params(), suite.tool_executor_factory(suite.make_env())
    )
    return enf, report


STRAIGHT = "def run():\n    set_limit(1)\n    send(5)\n"


def test_replay_of_executed_call_denied_live_but_probe_blind():
    enf, report = _run(STRAIGHT)
    assert not report.crashed and not report.denied
    # live replay of an already-executed site is refused ...
    d = enf.check("send", [5], live=True)
    assert not d.permit and "replay" in d.reason
    # ... while the default probe still sees the pure authorization relation.
    assert enf.check("send", [5]).permit


def test_loop_tuples_consumed_individually():
    plan = "def run():\n    items = get_items()\n    for i in items:\n        send(i)\n"
    enf, report = _run(plan, env_kwargs={"items": (5, 5)})
    assert not report.crashed and not report.denied
    sends = [e for e in report.events if e.tool == "send"]
    assert len(sends) == 2 and all(e.decision.permit for e in sends)
    # both duplicate tuples are consumed; a third live send(5) has none left
    d = enf.check("send", [5], live=True)
    assert not d.permit
    # the probe view still authorizes the value
    assert enf.check("send", [5]).permit


def test_in_order_run_passes_under_ordering():
    enf, report = _run(STRAIGHT, ordered={"set_limit", "send"})
    assert not report.crashed and not report.denied


def test_out_of_order_side_effect_denied():
    suite = _suite()
    prepared = prepare(STRAIGHT, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer(),
                   ordered_tools={"set_limit", "send"})
    # agent tries the later side effect first: earlier site not executed
    d = enf.check("send", [5], live=True)
    assert not d.permit and "out of order" in d.reason
    # execute the earlier site, then the send is authorized
    d1 = enf.check("set_limit", [1], live=True)
    assert d1.permit
    enf.record(d1.rule, wrap({"ok": True}), d1.token)
    assert enf.check("send", [5], live=True).permit


def test_off_path_earlier_site_is_skippable():
    plan = (
        "def run():\n"
        "    p = get_price()\n"
        "    if p > 100:\n"
        "        set_limit(1)\n"
        "    send(5)\n"
    )
    # price=50: the guarded set_limit is provably off-path, send may proceed
    enf, report = _run(plan, ordered={"set_limit", "send"}, env_kwargs={"price": 50})
    assert not report.crashed and not report.denied
    sends = [e for e in report.events if e.tool == "send"]
    assert sends and sends[0].decision.permit


def test_unresolved_guard_blocks_ordering():
    plan = (
        "def run():\n"
        "    p = get_price()\n"
        "    if p > 100:\n"
        "        set_limit(1)\n"
        "    send(5)\n"
    )
    suite = _suite({"price": 200})
    prepared = prepare(plan, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer(),
                   ordered_tools={"set_limit", "send"})
    # without the price envelope the earlier site is not provably off-path
    d = enf.check("send", [5], live=True)
    assert not d.permit and "out of order" in d.reason
