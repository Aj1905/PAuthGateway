"""Multi-suite merge + namespacing tests (D2)."""

from __future__ import annotations

from typing import Any, Callable

import pytest

from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec
from gateway.providers.registry import merge_suites, namespaced_tool


def _ping_suite(reply: str) -> SuiteSpec:
    tools = {
        "ping": ToolSpec(
            name="ping", params=["msg"], signer="s",
            doc=ToolDoc(name="ping", description="echo", parameters=[
                {"name": "msg", "type": "string", "desc": "message"}], returns="string"),
        )
    }
    impl: dict[str, Callable[..., Any]] = {"ping": lambda msg: f"{reply}:{msg}"}
    return SuiteSpec(
        name="x", tools=tools, make_env=lambda: object(),
        runner_factory=lambda env: (lambda tool, kw: impl[tool](**kw)), tasks=[],
    )


def test_collision_raises_without_namespace():
    with pytest.raises(ValueError, match="collision"):
        merge_suites("m", {"a": _ping_suite("A"), "b": _ping_suite("B")})


def test_namespace_resolves_collision():
    merged = merge_suites("m", {"a": _ping_suite("A"), "b": _ping_suite("B")}, namespace=True)
    assert set(merged.tool_names()) == {"a__ping", "b__ping"}
    # namespaced names are valid Python identifiers (grammar/A1 can call them).
    assert all(n.replace("__", "_").isidentifier() for n in merged.tool_names())
    # the tool doc carries the namespaced name too.
    docs = {d.name for d in merged.tool_docs()}
    assert docs == {"a__ping", "b__ping"}


def test_namespaced_runner_routes_to_owning_suite():
    merged = merge_suites("m", {"a": _ping_suite("A"), "b": _ping_suite("B")}, namespace=True)
    run = merged.runner_factory(merged.make_env())
    assert run("a__ping", {"msg": "hi"}) == "A:hi"
    assert run("b__ping", {"msg": "hi"}) == "B:hi"


def test_namespaced_tool_helper():
    assert namespaced_tool("banking", "send_money") == "banking__send_money"
