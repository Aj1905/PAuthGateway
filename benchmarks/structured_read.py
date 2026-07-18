"""Augment any suite with a gateway-provided ``structure_text`` tool.

The point (the whole thread): a prose value (a bill amount buried in a text
file) is inexpressible because the string-op-free grammar cannot pull it out, so
the Planner either hardcodes it (rejected by precheck: not prompt-entailed) or fails --
it never reaches the confirmation gate. ``structure_text`` turns the prose into
typed FIELDS, so the value flows as a DATAFLOW field (not a constant): precheck
defers dataflow, the taint gate marks it untrusted (structure_text is an
untrusted source), and the confirmation gate finally handles it. Same treatment
as SaaS data, plus a taint -- exactly the design.

This wraps a SuiteSpec without touching the AgentDojo adapter: it adds the tool
and intercepts it in the runner, delegating everything else.
"""

from __future__ import annotations

import dataclasses

from pauth.codegen import ToolDoc
from pauth.structuring import structure
from pauth.suites.base import SuiteSpec, ToolSpec

_VIEW_SCHEMA = (
    "object {amounts: list of number, ibans: list of string, dates: list of "
    "string, emails: list of string, lines: list of string, taint: boolean}"
)

STRUCTURE_TOOL = ToolSpec(
    name="structure_text",
    params=["text"],
    signer="web",  # an UNTRUSTED source signer -- its fields carry taint
    doc=ToolDoc(
        name="structure_text",
        description=(
            "Deterministically structure an untrusted text blob into typed "
            "fields (amounts, ibans, dates, emails). The source is untrusted."
        ),
        parameters=[{"name": "text", "type": "string", "desc": "untrusted text"}],
        returns=_VIEW_SCHEMA,
    ),
)


def augment_with_structuring(spec: SuiteSpec) -> SuiteSpec:
    """Return a copy of ``spec`` with ``structure_text`` available to plans."""
    tools = dict(spec.tools)
    tools["structure_text"] = STRUCTURE_TOOL
    base_factory = spec.runner_factory

    def runner_factory(env):
        base = base_factory(env)

        def run(tool, kwargs):
            if tool == "structure_text":
                return structure(str(kwargs["text"]))
            return base(tool, kwargs)

        return run

    return dataclasses.replace(spec, tools=tools, runner_factory=runner_factory)
