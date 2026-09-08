"""Real filesystem MCP + HTTP confirmation trial with a fixed fixture plan.

No LLM request is made. This measures K4's operator gate, not K1's Planner.
A fresh private directory and an OS-assigned HTTP port isolate the trial.
Run from the repository root:
  .venv/bin/python -m docs.lab.human_confirmation_trial --mcp-entry /path/to/server-filesystem/dist/index.js
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import HTTPServer
import json
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import threading

from docs.lab.e2e_runner import run_task
from gateway.ingress import agent_channel
from gateway.planning.planner import PlanDraft
from gateway.providers.mcp_suite import build_mcp_suite_stdio
from gateway.runtime.audit import AuditLog
from gateway.runtime.confirmation import SourceTrust
from gateway.serving.http_server import TokenAuth, _Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mcp-entry', type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.mcp_entry.is_file():
        parser.error('MCP entry does not exist')
    node = shutil.which('node')
    if not node:
        parser.error('node is required')
    if not sys.stdin.isatty():
        parser.error('a terminal is required for the operator decision')

    root = Path(tempfile.mkdtemp(prefix='pauth-codex-human-')).resolve()
    source, destination, reference = root / 'notes.txt', root / 'moved.txt', root / 'dest.txt'
    source.write_text('PAuth human confirmation trial\n', encoding='utf-8')
    reference.write_text(str(destination), encoding='utf-8')
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    suite = build_mcp_suite_stdio('trial', [node, str(args.mcp_entry.resolve()), str(root)])
    transport = suite.make_env()
    agent_token, operator_token = secrets.token_hex(24), secrets.token_hex(24)
    code = (
        'def run():\n'
        f'    target = read_text_file({json.dumps(str(reference))}, None, None)\n'
        f'    move_file({json.dumps(str(source))}, target.content)\n'
    )
    prompt = f'Read the destination path stored in {reference} and move {source} to that path.'

    class FixturePlanner:
        def generate(self, received_prompt, suite_loader):
            if received_prompt != prompt:
                raise ValueError('fixture accepts only its exact trial prompt')
            return PlanDraft(suite_name='trial', code=code, reason='fixed fixture plan; no LLM generation')

    # Fixture injection is restricted to this dedicated experimental process.
    # The HTTP session, enforcement, MCP transport, and operator API are real.
    original_planner_factory = agent_channel.build_planner
    agent_channel.build_planner = lambda *a, **k: FixturePlanner()

    class Handler(_Handler):
        suite_loader = staticmethod(lambda name: suite)
        sessions = {}
        session_owners = {}
        bindings = {}
        session_store = None
        audit_log = AuditLog(root / 'audit.jsonl')
        source_trust = SourceTrust.fail_closed()
        auth = TokenAuth({'trial-agent': agent_token})
        prompt_suite_loader = None
        operand_policy = None
        llm_upstream = None
        merged_suite_name = 'trial'

    Handler.operator_token = operator_token
    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{server.server_port}'
    before_decisions = []

    def operator_input():
        state = {'source_exists': source.exists(), 'destination_exists': destination.exists(),
                 'source_hash_matches': source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == source_hash}
        before_decisions.append(state)
        if not (state['source_exists'] and not state['destination_exists'] and state['source_hash_matches']):
            raise RuntimeError('filesystem changed before the operator decision')
        checkpoint = {'directory': str(root), 'source': str(source), 'destination': str(destination),
                      'reference': str(reference), 'before_decision': state,
                      'planner_source': 'fixed_fixture', 'api_cost_usd': 0}
        (root / 'checkpoint.json').write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2))
        print('HUMAN_DECISION_REQUIRED ' + json.dumps(checkpoint, ensure_ascii=False), flush=True)
        return input()

    task = {'id': 'human-gate', 'prompt': prompt, 'calls': [
        {'tool': 'read_text_file', 'kwargs': {'path': str(reference), 'head': None, 'tail': None}, 'expect': True},
        {'tool': 'move_file', 'kwargs': {'source': str(source), 'destination': str(destination)}, 'expect': True},
    ]}
    print('TRIAL_DIRECTORY ' + str(root), flush=True)
    try:
        result = run_task(base, agent_token, task, 'deterministic', '', operator_token,
                          approval_mode='interactive', input_fn=operator_input)
        decisions = [d for d in result['decisions'] if d['resolved'] and d['source'] == 'interactive']
        moved = not source.exists() and destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == source_hash
        untouched = source.is_file() and not destination.exists() and hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        verified = bool(before_decisions and decisions) and (
            (all(d['approved'] is True for d in decisions) and moved and result['pass'])
            or (any(d['approved'] is False for d in decisions) and untouched)
        )
        evidence = {'planner_source': 'fixed_fixture', 'api_cost_usd': 0,
                    'before_decisions': before_decisions, 'source_exists': source.exists(),
                    'destination_exists': destination.exists(), 'gate_verified': verified,
                    'result': result}
        (root / 'result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(json.dumps(evidence, ensure_ascii=False), flush=True)
        return 0 if verified else 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
        transport.close()
        agent_channel.build_planner = original_planner_factory


if __name__ == '__main__':
    raise SystemExit(main())
