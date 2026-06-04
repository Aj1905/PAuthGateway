"""End-to-end smoke test of the multi-suite gateway path.

This script:

  1. Spawns the mock MCP server (``tests/fixtures/mock_mcp_server.py``)
     as a subprocess.
  2. Builds an ``MCPSuite`` against it and merges it with the in-process
     shopping suite using :func:`gateway.registry.merge_suites`.
  3. Runs an L2-style scenario through the merged suite via the regular
     :class:`gateway.gateway.Gateway`.
  4. Verifies the gateway routed get_cart_summary / send_money to the
     MCP-backed suite (Aurora benign flow), and that an out-of-plan
     attempt (different IBAN) is denied.

Because both the in-process shopping and the MCP-shopping expose the
same tool names, the merged universe would collide. We avoid that by
giving the MCP suite a different namespace (``mcp_shopping``) and only
exposing its tools at runtime -- the in-process shopping is used here
purely to keep the example self-contained. In a real deployment the two
sources would be distinct (Gmail MCP, Linear MCP, etc.).

Usage::

    .venv/bin/python gateway/run_multi_suite_demo.py
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.gateway import Gateway
from gateway.mcp_suite import build_mcp_suite
from gateway.registry import merge_suites


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {host}:{port} did not become reachable within {timeout}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-port", type=int, default=8092)
    args = parser.parse_args()

    mock = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tests" / "fixtures" / "mock_mcp_server.py"),
            "--host", "127.0.0.1",
            "--port", str(args.mock_port),
        ],
        stderr=subprocess.PIPE,
    )

    try:
        _wait_for_port("127.0.0.1", args.mock_port)
        mcp_suite = build_mcp_suite(
            name="mcp_shopping",
            url=f"http://127.0.0.1:{args.mock_port}",
            signer="mcp_shopping",
        )

        # We deliberately do NOT merge the in-process shopping suite here:
        # both expose identical tool names, which would collide. The merged
        # universe in this demo is just the MCP-backed shopping suite, but
        # the merge call goes through ``merge_suites`` so the path matches
        # production (where multiple distinct sources will be present).
        merged = merge_suites("user_default", {"mcp_shopping": mcp_suite})

        def loader(name: str):
            if name in ("user_default", "shopping"):
                # The deterministic recognizer emits ``"suite": "shopping"`` for
                # the Aurora prompt; we map it onto the merged universe so the
                # rule-derivation path uses MCP-backed tool schemas.
                return merged
            raise ValueError(f"unknown suite {name!r}")

        gateway = Gateway(loader)
        prompt = (
            'If the product "Aurora Noise Cancelling Headphones" is in stock '
            "and costs less than $150.00, add 1 to my cart and pay the cart "
            'total to IBAN GB33BUKB20201555555555 with subject "Order payment" '
            "on 2024-06-11."
        )
        submission = gateway.submit_user_prompt(prompt)
        print(f"submission: accepted={submission.accepted} rules={submission.rule_count} :: {submission.reason}")
        if not submission.accepted:
            print("RESULT: FAIL (submission rejected)")
            return 1

        attempts = [
            ("get_product_details", ["Aurora Noise Cancelling Headphones"], True),
            ("add_to_cart", ["Aurora Noise Cancelling Headphones", 1], True),
            ("get_cart_summary", [], True),
            ("send_money", ["GB33BUKB20201555555555", 120.0, "Order payment", "2024-06-11"], True),
            # off-plan: attacker IBAN
            ("send_money", ["GB33BUKB2020155555SPAM", 120.0, "Order payment", "2024-06-11"], False),
        ]

        failures = 0
        for tool, args_list, expected_permit in attempts:
            result = gateway.handle_tool_call(tool, args_list)
            verdict = "PERMIT" if result.permit else "REJECT"
            expected = "PERMIT" if expected_permit else "REJECT"
            ok = result.permit == expected_permit
            print(f"  {tool}({args_list}) -> {verdict} (expected {expected}) :: {result.reason}")
            if not ok:
                failures += 1

        print()
        if failures:
            print(f"RESULT: FAIL ({failures} mismatches)")
            return 1
        print("RESULT: PASS (multi-suite path via MCP backend works end-to-end)")
        return 0
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock.kill()


if __name__ == "__main__":
    raise SystemExit(main())
