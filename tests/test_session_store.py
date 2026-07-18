"""Session persistence tests (call interception)."""

from __future__ import annotations

from gateway.serving.session_store import SessionStore


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


def test_corrupt_file_is_tolerated(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{ not valid json")
    s = SessionStore(path)  # must not raise
    assert len(s) == 0
    s.record("sid-1", "p")
    assert SessionStore(path).get("sid-1")["prompt"] == "p"


def test_atomic_write_leaves_no_tmp(tmp_path):
    path = tmp_path / "sub" / "sessions.json"
    s = SessionStore(path)
    s.record("sid-1", "p")
    # only the final file exists, no leftover .tmp
    assert path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_restore_channel_rebuilds_plan(tmp_path):
    """A persisted session is rebuilt (plan re-established) after a 'restart'."""
    from pauth.suites.shopping import build_suite
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
    # Simulate: a prompt was accepted and persisted before the restart.
    store.record("sid", aurora, {})

    # After restart (fresh process), restore rebuilds the channel with a plan.
    ch = restore_channel(loader, store, "sid")
    assert ch is not None
    # The plan is active: a tool call is now enforced (not "no prompt submitted").
    resp = ch.receive_json({"kind": "tool_call", "tool": "get_product_details",
                            "kwargs": {"name": "Aurora Noise Cancelling Headphones"}})
    assert resp["permit"] is True

    # Unknown session -> None.
    assert restore_channel(loader, store, "missing") is None
