"""List comprehensions: a pure map/filter over a signed collection is a
deterministic function the enforcer re-derives. Grammar accepts it; a tampered
result list is off-slice and denied (FN=0). Offline, no API key.
"""

from __future__ import annotations

import dataclasses

from pauth import prepare
from pauth.codegen import ToolDoc
from pauth.enforcer import Enforcer, check_injection, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.suites.base import SuiteSpec, ToolSpec


@dataclasses.dataclass
class User:
    email: str
    vip: bool


class Env:
    def __init__(self):
        self.users = [User("a@x.com", True), User("b@x.com", False), User("c@x.com", True)]
        self.sent: list = []


def _tool_executor(env):
    impl = {
        "get_users": lambda: env.users,
        "notify": lambda recipients: env.sent.append(recipients) or {"ok": True},
    }
    return lambda tool, kwargs: impl[tool](**kwargs)


def _tool(name, params, ret):
    return ToolSpec(name=name, params=params, signer="s",
                    doc=ToolDoc(name=name, description=name,
                               parameters=[{"name": p, "type": "string", "desc": p} for p in params],
                               returns=ret))


_TOOLS = {
    "get_users": _tool("get_users", [], "list of object {email: string, vip: boolean}"),
    "notify": _tool("notify", ["recipients"], "object {ok: boolean}"),
}

MAP_PLAN = '''\
def run():
    users = get_users()
    emails = [u.email for u in users]
    notify(emails)
'''
FILTER_PLAN = '''\
def run():
    users = get_users()
    vips = [u.email for u in users if u.vip]
    notify(vips)
'''


def _armed(code):
    suite = SuiteSpec(name="c", tools=_TOOLS, make_env=Env, tool_executor_factory=_tool_executor, tasks=[])
    prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    execute_generated_code(prepared.source, enf, suite.tool_params(), suite.tool_executor_factory(suite.make_env()))
    return enf


def test_map_comp_accepted_and_authorises_the_exact_list():
    enf = _armed(MAP_PLAN)
    ok = lambda lst: check_injection(enf, "notify", [lst]).permit
    assert ok(["a@x.com", "b@x.com", "c@x.com"])           # the mapped list
    assert not ok(["a@x.com", "b@x.com"])                  # missing one -> off-slice
    assert not ok(["a@x.com", "b@x.com", "attacker@x.com"])  # injected recipient


def test_filter_comp_authorises_only_the_kept_subset():
    enf = _armed(FILTER_PLAN)
    ok = lambda lst: check_injection(enf, "notify", [lst]).permit
    assert ok(["a@x.com", "c@x.com"])                      # only the vips
    assert not ok(["a@x.com", "b@x.com", "c@x.com"])       # b is not a vip -> denied
    assert not ok(["a@x.com"])                             # missing a vip -> denied
