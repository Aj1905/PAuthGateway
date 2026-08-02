"""Nested bounded-for: the DSL now allows a for inside a for (a bounded
join / sub-iteration). The slicer records the loop stack; the enforcer enumerates
the signed collections' NESTED product (each inner iter evaluated with the outer
vars bound). FN=0 must hold: only the tuples the loops can actually produce are
authorised, and -- crucially -- a DEPENDENT nesting must not authorise a
cross-parent pair. Offline, no API key.
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
class Item:
    v: object


@dataclasses.dataclass
class Order:
    id: str
    items: list


class Env:
    def __init__(self):
        self.letters = [Item("a"), Item("b")]
        self.numbers = [Item(1), Item(2)]
        self.orders = [Order("o1", [Item("x"), Item("y")]), Order("o2", [Item("z")])]
        self.sink: list = []


def _tool_executor(env):
    impl = {
        "get_letters": lambda: env.letters,
        "get_numbers": lambda: env.numbers,
        "get_orders": lambda: env.orders,
        "pair": lambda a, b: env.sink.append((a, b)) or {"ok": True},
        "ship": lambda order_id, item: env.sink.append((order_id, item)) or {"ok": True},
    }
    return lambda tool, kwargs: impl[tool](**kwargs)


def _tool(name, params, ret):
    return ToolSpec(name=name, params=params, signer="s",
                    doc=ToolDoc(name=name, description=name,
                               parameters=[{"name": p, "type": "string", "desc": p} for p in params],
                               returns=ret))


_TOOLS = {
    "get_letters": _tool("get_letters", [], "list of object {v: string}"),
    "get_numbers": _tool("get_numbers", [], "list of object {v: number}"),
    "get_orders": _tool("get_orders", [], "list of object {id: string, items: list}"),
    "pair": _tool("pair", ["a", "b"], "object {ok: boolean}"),
    "ship": _tool("ship", ["order_id", "item"], "object {ok: boolean}"),
}


def _suite():
    return SuiteSpec(name="nl", tools=_TOOLS, make_env=Env,
                     tool_executor_factory=_tool_executor, tasks=[])


PRODUCT = '''\
def run():
    L = get_letters()
    N = get_numbers()
    for x in L:
        for y in N:
            pair(x.v, y.v)
'''

DEPENDENT = '''\
def run():
    O = get_orders()
    for o in O:
        for i in o.items:
            ship(o.id, i.v)
'''


def _armed(code, tool):
    suite = _suite()
    prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    execute_generated_code(prepared.source, enf, suite.tool_params(), suite.tool_executor_factory(suite.make_env()))
    return enf, prepared


def test_nested_for_accepted_and_records_two_loops():
    _enf, prepared = _armed(PRODUCT, "pair")
    r = next(r for r in prepared.rules if r.tool == "pair")
    assert [v for v, _ in r.loops] == ["x", "y"]  # outer, inner


def test_independent_product_authorises_the_grid_and_denies_off_grid():
    enf, _ = _armed(PRODUCT, "pair")
    ok = lambda a, b: check_injection(enf, "pair", [a, b]).permit
    for a in ("a", "b"):
        for b in (1, 2):
            assert ok(a, b)                 # every A×B tuple authorised
    assert not ok("a", 3)                   # 3 not in numbers -> off-slice
    assert not ok("c", 1)                   # c not in letters -> off-slice
    assert not ok(1, "a")                   # swapped operands -> off-slice


def test_dependent_nesting_denies_a_cross_parent_pair():
    # the KEY FN=0 check: 'z' is order o2's item, not o1's. Both 'o1' and 'z' exist,
    # but the pair (o1, z) is NOT on the nested enumeration -> must be denied. A flat
    # product would wrongly allow it; the context-evaluated inner iter forbids it.
    enf, _ = _armed(DEPENDENT, "ship")
    ok = lambda oid, it: check_injection(enf, "ship", [oid, it]).permit
    assert ok("o1", "x") and ok("o1", "y") and ok("o2", "z")   # reachable pairs
    assert not ok("o1", "z")                # z belongs to o2, not o1 -> DENIED
    assert not ok("o2", "x")                # x belongs to o1, not o2 -> DENIED
    assert not ok("o3", "x")                # o3 does not exist -> DENIED
