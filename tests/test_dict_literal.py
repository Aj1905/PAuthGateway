"""Dict literals: a `{k: v}` of traced values is a deterministic construction the
enforcer re-derives (like a list). Grammar accepts it; a tampered dict operand is
off-slice and denied (FN=0). Offline, no API key.
"""

from __future__ import annotations

import dataclasses

from pauth import prepare
from pauth.codegen import ToolDoc
from pauth.enforcer import Enforcer, check_injection
from pauth.tool_executor import execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.suites.base import SuiteSpec, ToolSpec


@dataclasses.dataclass
class Rec:
    amount: float
    iban: str


class Env:
    def __init__(self):
        self.rec = Rec(98.70, "GB123")
        self.saved: list = []


def _tool_executor(env):
    impl = {
        "get_record": lambda: env.rec,
        "save": lambda record: env.saved.append(record) or {"ok": True},
    }
    return lambda tool, kwargs: impl[tool](**kwargs)


def _tool(name, params, ret):
    return ToolSpec(name=name, params=params, signer="s",
                    doc=ToolDoc(name=name, description=name,
                               parameters=[{"name": p, "type": "string", "desc": p} for p in params],
                               returns=ret))


_TOOLS = {
    "get_record": _tool("get_record", [], "object {amount: number, iban: string}"),
    "save": _tool("save", ["record"], "object {ok: boolean}"),
}

PLAN = '''\
def run():
    r = get_record()
    save({"amount": r.amount, "iban": r.iban})
'''


def _armed():
    suite = SuiteSpec(name="d", tools=_TOOLS, make_env=Env, tool_executor_factory=_tool_executor, tasks=[])
    prepared = prepare(PLAN, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    execute_generated_code(prepared.source, enf, suite.tool_params(), suite.tool_executor_factory(suite.make_env()))
    return enf


def test_dict_literal_accepted_and_runs():
    suite = SuiteSpec(name="d", tools=_TOOLS, make_env=Env, tool_executor_factory=_tool_executor, tasks=[])
    prepared = prepare(PLAN, suite.tool_names(), suite.tool_signer())
    assert prepared.rules  # grammar accepts the dict literal


def test_correct_dict_authorised_tampered_denied():
    enf = _armed()
    ok = lambda rec: check_injection(enf, "save", [rec]).permit
    assert ok({"amount": 98.70, "iban": "GB123"})           # re-derived exactly
    assert not ok({"amount": 9999.0, "iban": "GB123"})      # amount tampered
    assert not ok({"amount": 98.70, "iban": "ATTACKER"})    # iban tampered
    assert not ok({"amount": 98.70})                        # missing key
