"""Session persistence tests (call interception)."""

from __future__ import annotations

import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.serving.session_store import SessionStore, SessionStoreCorruptionError


def test_record_and_reload_survives_restart(tmp_path):
    path = tmp_path / "sessions.json"
    s = SessionStore(path)
    s.record("sid-1", "buy the aurora headphones", {"strategy": "auto", "suite": "shopping"})
    s.record("sid-2", "send a slack message", {"strategy": "auto"})
    assert len(s) == 2

    # A fresh store (as after a restart) loads the persisted sessions.
    reloaded = SessionStore(path)
    assert len(reloaded) == 2
    assert reloaded.get("sid-1")["prompt"] == "buy the aurora headphones"
    assert reloaded.get("sid-1")["config"]["suite"] == "shopping"
    assert reloaded.get("sid-1")["execution_state"] is None


def test_remove_persists(tmp_path):
    path = tmp_path / "sessions.json"
    s = SessionStore(path)
    s.record("sid-1", "p1")
    s.remove("sid-1")
    assert SessionStore(path).get("sid-1") is None


def test_update_overwrites(tmp_path):
    path = tmp_path / "sessions.json"
    s = SessionStore(path)
    s.record("sid-1", "old")
    s.record("sid-1", "new")
    assert SessionStore(path).get("sid-1")["prompt"] == "new"


def test_corrupt_file_fails_closed(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{ not valid json")
    with pytest.raises(SessionStoreCorruptionError, match="invalid session store JSON"):
        SessionStore(path)


@pytest.mark.parametrize(
    "document, message",
    [
        ([], "root must be a JSON object"),
        ({"sid": []}, "entry must be a JSON object"),
        ({"sid": {"config": {}}}, "missing fields: prompt"),
        ({"sid": {"prompt": "p", "config": []}}, "config must be a JSON object"),
        (
            {"sid": {"prompt": "p", "config": {}, "owner": 7}},
            "owner must be a string",
        ),
        (
            {"sid": {"prompt": "p", "config": {}, "execution_state": []}},
            "execution_state must be an object or null",
        ),
        (
            {"sid": {"prompt": "p", "config": {}, "unexpected": True}},
            "unknown fields: unexpected",
        ),
    ],
)
def test_malformed_document_fails_closed(tmp_path, document, message):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps(document))
    with pytest.raises(SessionStoreCorruptionError, match=message):
        SessionStore(path)


def test_legacy_entry_without_execution_state_loads_without_synthesizing_it(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"sid": {"prompt": "p", "config": {}}}))
    entry = SessionStore(path).get("sid")
    assert entry is not None
    assert "execution_state" not in entry


def test_execution_state_and_owner_survive_restart_and_rerecord(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    state = {"plan_source_sha256": "abc", "attempts": {"token": "indeterminate"}}
    store.record("sid", "first", {"suite": "shopping"}, owner="alice", execution_state=state)

    # A prompt/config refresh cannot silently erase the ownership or safety ledger.
    store.record("sid", "second", {"suite": "shopping"})
    entry = SessionStore(path).get("sid")
    assert entry == {
        "prompt": "second",
        "config": {"suite": "shopping"},
        "owner": "alice",
        "execution_state": state,
    }


def test_update_execution_state_requires_existing_session_and_persists(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    with pytest.raises(KeyError, match="missing"):
        store.update_execution_state("missing", {"attempts": {}})

    store.record("sid", "p")
    state = {"attempts": {"token": "succeeded"}}
    store.update_execution_state("sid", state)
    assert SessionStore(path).get("sid")["execution_state"] == state


def test_get_and_all_return_deep_copies(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.record(
        "sid",
        "p",
        {"nested": {"value": 1}},
        execution_state={"attempts": {"token": "started"}},
    )

    one = store.get("sid")
    one["config"]["nested"]["value"] = 99
    one["execution_state"]["attempts"]["token"] = "succeeded"
    everything = store.all()
    everything["sid"]["prompt"] = "mutated"

    unchanged = store.get("sid")
    assert unchanged["prompt"] == "p"
    assert unchanged["config"]["nested"]["value"] == 1
    assert unchanged["execution_state"]["attempts"]["token"] == "started"


def test_concurrent_records_do_not_lose_sessions(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)

    def record(index):
        store.record(f"sid-{index}", f"prompt-{index}", execution_state={"n": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(32)))

    reloaded = SessionStore(path)
    assert len(reloaded) == 32
    assert reloaded.get("sid-17")["execution_state"] == {"n": 17}


def test_atomic_write_leaves_no_tmp(tmp_path):
    path = tmp_path / "sub" / "sessions.json"
    s = SessionStore(path)
    s.record("sid-1", "p")
    # only the final file exists, no leftover .tmp
    assert path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_commit_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "sessions.json"
    calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    SessionStore(path).record("sid", "p")
    assert len(calls) == 2


def test_restore_channel_rebuilds_plan(tmp_path):
    """A persisted session is rebuilt (plan re-established) after a 'restart'."""
    from pauth.suites.shopping import build_suite
    from gateway.ingress.agent_channel import AgentChannel
    from gateway.serving.http_server import restore_channel

    def loader(name):
        if name != "shopping":
            raise ValueError(name)
        return build_suite()

    aurora = (
        'If the product "Aurora Noise Cancelling Headphones" is in stock '
        "and costs less than $150.00, add 1 to my cart and pay the cart "
        'total to IBAN GB33BUKB20201555555555 with subject "Order payment" '
        "on 2024-06-11."
    )
    store = SessionStore(tmp_path / "s.json")
    # Simulate the HTTP accept path: prompt and empty execution ledger are
    # published together before the process restarts.
    original = AgentChannel(loader)
    accepted = original.receive_json({"kind": "prompt", "prompt": aurora})
    assert accepted["accepted"]
    store.record("sid", aurora, {}, execution_state=original.execution_state())

    # After restart (fresh process), restore rebuilds the channel with a plan.
    ch = restore_channel(loader, store, "sid")
    assert ch is not None
    # The plan is active: a tool call is now enforced (not "no prompt submitted").
    resp = ch.receive_json({"kind": "tool_call", "tool": "get_product_details",
                            "kwargs": {"name": "Aurora Noise Cancelling Headphones"}})
    assert resp["permit"] is True

    # Unknown session -> None.
    assert restore_channel(loader, store, "missing") is None


def test_restore_channel_rejects_legacy_missing_execution_state(tmp_path):
    from gateway.serving.http_server import SessionRestoreError, restore_channel

    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"sid": {"prompt": "p", "config": {}}}))
    store = SessionStore(path)

    with pytest.raises(SessionRestoreError, match="legacy state is quarantined"):
        restore_channel(lambda _name: None, store, "sid")


def test_restore_channel_rejects_plan_fingerprint_mismatch(tmp_path):
    from gateway.ingress.agent_channel import AgentChannel
    from gateway.serving.http_server import SessionRestoreError, restore_channel
    from pauth.suites.shopping import build_suite

    def loader(name):
        if name != "shopping":
            raise ValueError(name)
        return build_suite()

    prompt = (
        'If the product "Aurora Noise Cancelling Headphones" is in stock '
        "and costs less than $150.00, add 1 to my cart and pay the cart "
        'total to IBAN GB33BUKB20201555555555 with subject "Order payment" '
        "on 2024-06-11."
    )
    original = AgentChannel(loader)
    accepted = original.receive_json({"kind": "prompt", "prompt": prompt})
    assert accepted["accepted"]
    state = original.execution_state()
    assert state is not None
    state["plan_source_sha256"] = "0" * 64
    store = SessionStore(tmp_path / "mismatch.json")
    store.record("sid", prompt, {}, execution_state=state)

    with pytest.raises(SessionRestoreError, match="plan fingerprint mismatch"):
        restore_channel(loader, store, "sid")


def test_http_session_path_persists_ledger_and_blocks_restart_replay(tmp_path):
    from gateway.serving import http_server
    from pauth.suites.shopping import build_suite

    def loader(name):
        if name != "shopping":
            raise ValueError(name)
        return build_suite()

    prompt = (
        'If the product "Aurora Noise Cancelling Headphones" is in stock '
        "and costs less than $150.00, add 1 to my cart and pay the cart "
        'total to IBAN GB33BUKB20201555555555 with subject "Order payment" '
        "on 2024-06-11."
    )
    store = SessionStore(tmp_path / "http-sessions.json")
    handler = object.__new__(http_server._Handler)
    http_server._Handler.suite_loader = staticmethod(loader)
    http_server._Handler.sessions = {}
    http_server._Handler.session_owners = {}
    http_server._Handler.session_store = store
    http_server._Handler.audit_log = None
    try:
        status, accepted = handler._handle_session_message(
            "sid", "local", {"kind": "prompt", "prompt": prompt}
        )
        assert status == 200 and accepted["accepted"]
        assert store.get("sid")["execution_state"]["calls"] == []

        call = {
            "kind": "tool_call",
            "tool": "get_product_details",
            "kwargs": {"name": "Aurora Noise Cancelling Headphones"},
        }
        status, first = handler._handle_session_message("sid", "local", call)
        assert status == 200 and first["permit"]
        assert first["execution_status"] == "succeeded"
        assert store.get("sid")["execution_state"]["calls"][0]["state"] == "succeeded"

        # A fresh channel after restart restores the tombstone, not an empty plan.
        http_server._Handler.sessions = {}
        http_server._Handler.session_owners = {}
        status, replay = handler._handle_session_message("sid", "local", call)
        assert status == 200
        assert not replay["permit"]
        assert replay["execution_status"] == "not_dispatched"
    finally:
        http_server._Handler.sessions = {}
        http_server._Handler.session_owners = {}
        http_server._Handler.session_store = None


def test_simultaneous_http_restore_dispatches_persisted_call_once(tmp_path):
    from gateway.ingress.agent_channel import AgentChannel
    from gateway.serving import http_server
    from pauth.suites.shopping import build_suite

    tool_executor_entered = threading.Event()
    duplicate_tool_executor_entered = threading.Event()
    release_tool_executor = threading.Event()
    executions: list[str] = []
    suite = build_suite()
    base_tool_executor_factory = suite.tool_executor_factory

    def tool_executor_factory(env):
        base_tool_executor = base_tool_executor_factory(env)

        def tool_executor(tool, kwargs):
            if tool == "get_product_details":
                executions.append(tool)
                if len(executions) > 1:
                    duplicate_tool_executor_entered.set()
                tool_executor_entered.set()
                assert release_tool_executor.wait(timeout=5)
            return base_tool_executor(tool, kwargs)

        return tool_executor

    suite.tool_executor_factory = tool_executor_factory

    def loader(name):
        if name != "shopping":
            raise ValueError(name)
        return suite

    prompt = (
        'If the product "Aurora Noise Cancelling Headphones" is in stock '
        "and costs less than $150.00, add 1 to my cart and pay the cart "
        'total to IBAN GB33BUKB20201555555555 with subject "Order payment" '
        "on 2024-06-11."
    )
    original = AgentChannel(loader)
    assert original.receive_json({"kind": "prompt", "prompt": prompt})["accepted"]
    store = SessionStore(tmp_path / "concurrent-restore.json")
    store.record(
        "sid",
        prompt,
        {},
        owner="local",
        execution_state=original.execution_state(),
    )

    http_server._Handler.suite_loader = staticmethod(loader)
    http_server._Handler.sessions = {}
    http_server._Handler.session_owners = {}
    http_server._Handler.session_store = store
    http_server._Handler.audit_log = None
    http_server._Handler.auth = None
    http_server._Handler.llm_upstream = None
    responses: list[tuple[int, dict]] = []
    call = {
        "kind": "tool_call",
        "tool": "get_product_details",
        "kwargs": {"name": "Aurora Noise Cancelling Headphones"},
    }

    def invoke() -> None:
        body = json.dumps(call).encode()
        handler = object.__new__(http_server._Handler)
        handler.path = "/sessions/sid/messages"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._authenticate = lambda: "local"
        handler._send_json = lambda status, payload: responses.append((status, payload))
        handler.do_POST()

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    try:
        first.start()
        assert tool_executor_entered.wait(timeout=5)
        second.start()
        try:
            assert not duplicate_tool_executor_entered.wait(timeout=0.5)
        finally:
            release_tool_executor.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()
        assert executions == ["get_product_details"]
        assert len(responses) == 2
        assert sum(bool(payload.get("permit")) for _, payload in responses) == 1
    finally:
        release_tool_executor.set()
        first.join(timeout=5)
        second.join(timeout=5)
        http_server._Handler.sessions = {}
        http_server._Handler.session_owners = {}
        http_server._Handler.session_store = None
