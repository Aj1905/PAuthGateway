"""Labelled corpus for the suite-filter recall eval (D1).

The suite_filter keeps a subset of registered suites so A1's prompt does not
grow linearly with the tool universe. Its blind spot: it may DROP a suite that
holds a tool the task actually needs. This corpus labels, per prompt, which
suite is required, so the eval can measure the drop rate over a multi-suite
universe (shopping + the four AgentDojo suites).
"""

from __future__ import annotations

import dataclasses

from pauth.suites.base import SuiteSpec
from pauth.suites.shopping import build_suite as build_shopping

ALL_SUITES = ["shopping", "banking", "slack", "travel", "workspace"]


@dataclasses.dataclass
class FilterCase:
    id: str
    prompt: str
    needed_suite: str


CASES: list[FilterCase] = [
    FilterCase(
        "shopping_buy",
        "Add the Aurora Noise Cancelling Headphones product to my cart and check out, "
        "paying the cart total.",
        "shopping",
    ),
    FilterCase(
        "banking_transfer",
        "Schedule a bank transaction transferring money to IBAN DE89370400440532013000 "
        "with subject rent, and show my recent transactions.",
        "banking",
    ),
    FilterCase(
        "slack_message",
        "Read the messages in the general Slack channel and send a direct message to a user.",
        "slack",
    ),
    FilterCase(
        "travel_hotel",
        "Find hotels in the city, get their prices and rating reviews, then reserve a restaurant.",
        "travel",
    ),
    FilterCase(
        "workspace_email",
        "Search my calendar for the day's appointments and send an email to the participants.",
        "workspace",
    ),
]


def build_universe(names: list[str] | None = None) -> dict[str, SuiteSpec]:
    """Build the multi-suite universe (offline; no API key needed)."""
    names = names or ALL_SUITES
    suites: dict[str, SuiteSpec] = {}
    for n in names:
        if n == "shopping":
            suites[n] = build_shopping()
        else:
            from benchmarks.agentdojo_adapter import load_suite
            suites[n] = load_suite(n)
    return suites
