"""Adapter layer: travel's prose ``get_all_*_in_city`` returns are normalised
into a structured ``list[str]`` so downstream tools (which expect a list) and
the restricted grammar (which has no string methods to split a blob) can consume
them without a runtime KeyError."""

from __future__ import annotations

from benchmarks.agentdojo_adapter import _structure_names, load_suite
from pauth import prepare
from pauth.enforcer import Enforcer, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing


def test_structure_names_strips_header_and_splits():
    blob = "Hotel Names: Le Marais Boutique\nGood Night\nLuxury Palace\n"
    assert _structure_names(blob) == ["Le Marais Boutique", "Good Night", "Luxury Palace"]
    # header with an "in City" phrasing
    assert _structure_names("Restaurant in Paris: A\nB\n") == ["A", "B"]
    # already structured / non-str passes through untouched
    assert _structure_names(["A", "B"]) == ["A", "B"]


def test_travel_read_tools_return_lists_and_advertise_schema():
    s = load_suite("travel")
    run = s.runner_factory(s.make_env())
    for tool in ("get_all_hotels_in_city", "get_all_restaurants_in_city",
                 "get_all_car_rental_companies_in_city"):
        out = run(tool, {"city": "Paris"})
        assert isinstance(out, list) and all(isinstance(x, str) for x in out), tool
        assert s.tools[tool].doc.returns == "list of str"


def test_cheapest_hotel_pattern_no_longer_crashes():
    s = load_suite("travel")
    code = ('def run():\n'
            '    hotels = get_all_hotels_in_city("Paris")\n'
            '    prices = get_hotels_prices(hotels)\n'
            '    cheapest = min(hotels, key=lambda h: prices[h])\n'
            '    addr = get_hotels_address(cheapest)\n')
    prepared = prepare(code, s.tool_names(), s.tool_signer())
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), s.tool_signer())
    rep = execute_generated_code(
        prepared.source, enf, s.tool_params(), s.runner_factory(s.make_env())
    )
    assert rep.crashed is None, rep.crashed
    assert not rep.denied
