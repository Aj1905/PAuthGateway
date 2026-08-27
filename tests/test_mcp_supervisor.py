"""StdioTransport health-check + auto-restart supervisor tests (envelope signing)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gateway.providers.mcp_suite import (
    MCPError,
    StdioTransport,
    build_mcp_suite_from_transport,
)

MOCK = str(Path(__file__).parent / "fixtures" / "mock_mcp_stdio.py")


def _transport(max_restarts: int = 3) -> StdioTransport:
    return StdioTransport([sys.executable, MOCK], max_restarts=max_restarts)


def test_transport_serves_normally():
    t = _transport()
    try:
        result = t.rpc("tools/list")
        assert result is not None
        assert t.restarts == 0 and t.is_alive()
    finally:
        t.close()


def test_noisy_subprocess_stderr_cannot_deadlock_rpc():
    t = _transport()
    try:
        assert t.rpc("noisy") == {"ok": True}
        assert len(t._read_stderr()) <= 8192
    finally:
        t.close()


def test_transport_restarts_after_subprocess_killed():
    t = _transport()
    try:
        t.rpc("tools/list")  # warm up
        # Simulate a crash: kill the subprocess out from under the transport.
        t._proc.kill()
        t._proc.wait(timeout=5)
        assert not t.is_alive()
        # The next call must transparently respawn and succeed.
        result = t.rpc("tools/list")
        assert result is not None
        assert t.restarts == 1 and t.is_alive()
    finally:
        t.close()


def test_transport_gives_up_after_max_restarts():
    # A command that exits immediately -> every rpc finds it dead and restarts,
    # exhausting the budget and raising rather than looping forever.
    t = StdioTransport([sys.executable, "-c", "raise SystemExit(0)"], max_restarts=2)
    try:
        with pytest.raises(MCPError):
            t.rpc("tools/list")
        assert t.restarts <= 2
    finally:
        t.close()


def test_on_restart_hook_is_invoked():
    calls = []
    t = StdioTransport([sys.executable, MOCK], on_restart=lambda tr: calls.append(tr.restarts))
    try:
        t.rpc("tools/list")
        t._proc.kill()
        t._proc.wait(timeout=5)
        t.rpc("tools/list")
        assert calls == [1]  # hook fired once, after the first respawn
    finally:
        t.close()


def test_effecting_tool_call_is_never_retried_after_lost_response(tmp_path):
    marker = tmp_path / "effects.txt"
    fixture = str(Path(__file__).parent / "fixtures" / "mcp_effect_then_exit.py")
    transport = StdioTransport([sys.executable, fixture, str(marker)])
    try:
        transport.rpc("tools/list")
        with pytest.raises(MCPError, match="indeterminate.*not retried"):
            transport.rpc("tools/call", {"name": "write", "arguments": {}})
        assert marker.read_text().splitlines() == ["effect"]
    finally:
        transport.close()


def test_mcp_application_error_is_not_returned_as_a_success():
    class FakeTransport:
        def rpc(self, method, params=None):
            if method == "tools/list":
                return {
                    "tools": [
                        {
                            "name": "write",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                }
            return {"isError": True, "content": [{"type": "text", "text": "failed"}]}

        def close(self):
            return None

    suite = build_mcp_suite_from_transport("fake", FakeTransport())
    executor = suite.tool_executor_factory(suite.make_env())
    with pytest.raises(MCPError, match="application error"):
        executor("write", {})


def test_mcp_optional_none_is_omitted_from_wire_arguments():
    class CapturingTransport:
        called = None

        def rpc(self, method, params=None):
            if method == "tools/list":
                return {
                    "tools": [
                        {
                            "name": "write",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "required_value": {"type": "string"},
                                    "optional_value": {"type": "string"},
                                },
                                "required": ["required_value"],
                            },
                        }
                    ]
                }
            self.called = params
            return {"content": []}

        def close(self):
            return None

    transport = CapturingTransport()
    suite = build_mcp_suite_from_transport("fake", transport)
    executor = suite.tool_executor_factory(suite.make_env())
    executor("write", {"required_value": "x", "optional_value": None})
    assert transport.called["arguments"] == {"required_value": "x"}
    assert "optional; pass None" in suite.tools["write"].doc.parameters[1]["desc"]


@pytest.mark.parametrize(
    "tool",
    [
        {"name": "", "inputSchema": {"properties": {}}},
        {"name": "write", "inputSchema": []},
        {"name": "write", "inputSchema": {"properties": []}},
        {
            "name": "write",
            "inputSchema": {"properties": {}, "required": {}},
        },
    ],
)
def test_mcp_rejects_malformed_tool_schema(tool):
    class FakeTransport:
        def rpc(self, method, params=None):
            return {"tools": [tool]}

        def close(self):
            return None

    with pytest.raises(MCPError):
        build_mcp_suite_from_transport("fake", FakeTransport())


def test_mcp_ignores_non_string_remote_descriptions():
    class FakeTransport:
        def rpc(self, method, params=None):
            return {
                "tools": [
                    {
                        "name": "write",
                        "description": {"not": "text"},
                        "inputSchema": {
                            "properties": {
                                "value": {
                                    "type": "string",
                                    "description": ["not", "text"],
                                }
                            }
                        },
                    }
                ]
            }

        def close(self):
            return None

    suite = build_mcp_suite_from_transport("fake", FakeTransport())
    assert suite.tools["write"].doc.description == ""
    assert suite.tools["write"].doc.parameters[0]["desc"] == (
        "(optional; pass None to omit)"
    )
