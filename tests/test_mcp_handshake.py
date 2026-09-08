"""MCP servers built on the Python SDK refuse every request until the client
has sent ``initialize`` and ``notifications/initialized``. The suite builder
must perform that handshake first, and again after a subprocess restart."""

from __future__ import annotations

from typing import Any

import pytest

from gateway.providers.mcp_suite import (
    MCPError,
    build_mcp_suite_from_transport,
    initialize_mcp,
)


class _StrictServer:
    """Transport double that behaves like mcp-server-git: -32602 before init."""

    def __init__(self) -> None:
        self.initialized = False
        self.log: list[tuple[str, dict | None]] = []

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.log.append((method, params))
        if method == "initialize":
            assert params and params.get("protocolVersion") and params.get("clientInfo")
            self.initialized = True
            return {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}}}
        if not self.initialized:
            raise MCPError(f"{method} error: {{'code': -32602, 'message': 'Invalid request parameters'}}")
        if method == "tools/list":
            return {"tools": [{"name": "git_status", "description": "status",
                               "inputSchema": {"type": "object",
                                               "properties": {"repo_path": {"type": "string"}},
                                               "required": ["repo_path"]}}]}
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "clean"}]}
        raise MCPError(f"unknown {method}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.log.append((method, params))

    def close(self) -> None:
        return None


def test_suite_builder_handshakes_before_listing_tools():
    server = _StrictServer()
    suite = build_mcp_suite_from_transport("git", server)
    assert [m for m, _ in server.log[:3]] == ["initialize", "notifications/initialized", "tools/list"]
    assert "git_status" in suite.tools
    assert suite.tools["git_status"].input_schema["required"] == ["repo_path"]
    run = suite.tool_executor_factory(suite.make_env())
    assert run("git_status", {"repo_path": "/r"}) == "clean"


def test_initialize_mcp_returns_server_capabilities():
    server = _StrictServer()
    result = initialize_mcp(server)
    assert result["capabilities"] == {"tools": {}}


def test_strict_server_without_handshake_is_refused():
    server = _StrictServer()
    with pytest.raises(MCPError, match="32602"):
        server.rpc("tools/list")


def test_text_tools_advertise_string_return_and_structured_content_wins():
    class _Server(_StrictServer):
        def rpc(self, method, params=None):
            if method == "tools/list":
                self.initialized = True
                return {"tools": [
                    {"name": "read", "description": "", "inputSchema": {"type": "object", "properties": {}}},
                    {"name": "stat", "description": "", "inputSchema": {"type": "object", "properties": {}},
                     "outputSchema": {"type": "object", "properties": {"size": {"type": "integer"}}}},
                ]}
            if method == "tools/call" and params["name"] == "stat":
                return {"content": [{"type": "text", "text": "{\"size\": 3}"}],
                        "structuredContent": {"size": 3}}
            return super().rpc(method, params)
    server = _Server()
    suite = build_mcp_suite_from_transport("s", server)
    assert suite.tools["read"].doc.returns.startswith("string")
    assert "size: integer" in suite.tools["stat"].doc.returns
    run = suite.tool_executor_factory(suite.make_env())
    assert run("stat", {}) == {"size": 3}
