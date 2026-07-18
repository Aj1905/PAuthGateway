"""StdioTransport health-check + auto-restart supervisor tests (envelope signing)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gateway.providers.mcp_suite import MCPError, StdioTransport

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
