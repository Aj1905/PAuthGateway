"""Exercise Codex probes through Claude's HTTP and hook wiring, on ephemeral ports."""
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import urllib.request
from http.server import HTTPServer
from gateway.serving import http_server
from gateway.runtime.audit import AuditLog
from gateway.operator.audit_report import read_events
from pauth.suites.shopping import build_suite


def test_http_health_and_session_audit(tmp_path, monkeypatch):
    monkeypatch.setenv('PAUTH_HOOK_SETTINGS', str(tmp_path / 'missing.json'))
    monkeypatch.delenv('AGENT_USER', raising=False)
    path = tmp_path / 'audit.jsonl'
    class Handler(http_server._Handler):
        suite_loader = staticmethod(lambda name: build_suite())
        sessions = {}
        session_owners = {}
        session_store = None
        audit_log = AuditLog(path)
        auth = None
        operator_token = None
        llm_upstream = None
        prompt_suite_loader = None
    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(base + '/health') as response:
            report = json.load(response)
        assert report['deployment']['checks']['hooks']['code'] == 'hook_unregistered'
        assert report['deployment']['healthy'] is False
        for sid in ('first', 'second'):
            data = json.dumps({'kind': 'prompt', 'prompt': 'x', 'strategy': 'deterministic'}).encode()
            request = urllib.request.Request(base + f'/sessions/{sid}/messages', data=data,
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request) as response:
                assert response.status == 200
        sessions, errors = read_events(path)
        assert not errors and set(sessions) == {'first', 'second'}
        assert all(events[0]['kind'] == 'submit' for events in sessions.values())
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_hook_reports_stopped_daemon(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Reserve a port without listening so no other test server is contacted.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        env = dict(os.environ, GATEWAY_URL=f'http://127.0.0.1:{sock.getsockname()[1]}',
            PAUTH_HOOK_SETTINGS=str(tmp_path / 'missing.json'), GATEWAY_HEALTH_CHECK='1',
            GATEWAY_MODE='strict', GATEWAY_MODE_PROMPT='strict')
        result = subprocess.run(['bash', str(root / 'gateway/hooks/submit_prompt.sh')],
            input=json.dumps({'session_id': 'probe', 'prompt': 'x'}), text=True,
            capture_output=True, env=env, timeout=30)
    assert result.returncode == 2
    assert 'daemon_unreachable' in result.stderr
    assert 'hook_unregistered' in result.stderr
