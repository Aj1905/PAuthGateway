"""End-to-end demo driving :class:`AgentChannel` like a real agent would.

Two demos:

1. **In-process** -- instantiates :class:`AgentChannel` directly and
   replays the L2 canonical scenarios through it. Confirms that the new
   agent-facing shape preserves the gateway's verdicts.
2. **HTTP round-trip** -- if ``--http`` is given, hits a running
   ``gateway/serving/http_server.py`` and replays the same scenarios over the
   wire. The runner prints the JSON request/response of the first
   benign attempt so the wire shape is visible.

Usage::

    .venv/bin/python gateway/agent_demo.py
    .venv/bin/python gateway/agent_demo.py --http http://127.0.0.1:8081
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pauth.suites.base import SuiteSpec
from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.ingress.agent_channel import (
    AgentChannel,
    PromptMessage,
    ToolCallMessage,
)
from tests.fixtures.l2_scenarios import SCENARIOS


def suite_loader(name: str) -> SuiteSpec:
    if name == "shopping":
        return build_shopping_suite()
    raise ValueError(f"agent_demo supports shopping only, not {name!r}")


# --------------------------------------------------------------------------
# In-process channel demo
# --------------------------------------------------------------------------

def run_inprocess() -> tuple[int, int]:
    failures = 0
    total_attempts = 0
    print("=" * 78)
    print(f"in-process AgentChannel demo :: {len(SCENARIOS)} scenarios")
    print("=" * 78)

    for scenario in SCENARIOS:
        channel = AgentChannel(suite_loader)
        prompt_response = channel.receive(PromptMessage(prompt=scenario.prompt))
        ok = getattr(prompt_response, "accepted", False) == scenario.submission_should_accept
        print(f"\n[{scenario.id}] submit {'OK' if ok else 'MISMATCH'} :: {prompt_response}")
        if not ok:
            failures += 1

        for i, attempt in enumerate(scenario.attempts):
            total_attempts += 1
            response = channel.receive(ToolCallMessage(tool=attempt.tool, args=attempt.args))
            permit = getattr(response, "permit", False)
            ok = permit == attempt.expected_permit
            verdict = "PERMIT" if permit else "REJECT"
            print(f"  [{i}] {verdict} (expected {'PERMIT' if attempt.expected_permit else 'REJECT'}) "
                  f"{attempt.tool} :: {attempt.label}")
            if not ok:
                failures += 1

    return failures, total_attempts


# --------------------------------------------------------------------------
# HTTP round-trip demo
# --------------------------------------------------------------------------

def _post_json(url: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_http(base_url: str) -> tuple[int, int]:
    failures = 0
    total_attempts = 0
    print("=" * 78)
    print(f"HTTP AgentChannel demo :: {base_url} ({len(SCENARIOS)} scenarios)")
    print("=" * 78)

    for sc_idx, scenario in enumerate(SCENARIOS):
        # Create session.
        sess = _post_json(f"{base_url}/sessions", {})
        session_id = sess["session_id"]
        messages_url = f"{base_url}/sessions/{session_id}/messages"

        # Submit prompt.
        prompt_payload = {"kind": "prompt", "prompt": scenario.prompt}
        if sc_idx == 0:
            print(f"\n  >>> {json.dumps(prompt_payload)}")
        prompt_response = _post_json(messages_url, prompt_payload)
        if sc_idx == 0:
            print(f"  <<< {json.dumps(prompt_response)}")
        ok = bool(prompt_response.get("accepted")) == scenario.submission_should_accept
        print(f"\n[{scenario.id}] submit {'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures += 1

        # Tool calls.
        for i, attempt in enumerate(scenario.attempts):
            total_attempts += 1
            call_payload = {"kind": "tool_call", "tool": attempt.tool, "args": attempt.args}
            response = _post_json(messages_url, call_payload)
            permit = bool(response.get("permit"))
            ok = permit == attempt.expected_permit
            verdict = "PERMIT" if permit else "REJECT"
            print(f"  [{i}] {verdict} (expected {'PERMIT' if attempt.expected_permit else 'REJECT'}) "
                  f"{attempt.tool}")
            if not ok:
                failures += 1

    return failures, total_attempts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", default="", help="if set, hit this base URL instead of in-process")
    args = parser.parse_args()

    if args.http:
        failures, attempts = run_http(args.http.rstrip("/"))
    else:
        failures, attempts = run_inprocess()

    print("\n" + "=" * 78)
    print(f"scenarios: {len(SCENARIOS)}, attempts: {attempts}, mismatches: {failures}")
    print("=" * 78)
    if failures:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
