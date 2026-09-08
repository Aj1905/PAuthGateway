"""K1 must distinguish actual execution, explicit decisions, and test oracles."""
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock
import pytest

spec = importlib.util.spec_from_file_location('lab_runner', Path(__file__).parents[1] / 'docs/lab/e2e_runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def pending(monkeypatch):
    hold = {'kind': 'confirmation', 'id': 'h1', 'tool': 'send', 'value': 'destination', 'source': ['read']}
    monkeypatch.setattr(runner.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps({'confirmations': [hold]}).encode()))


@pytest.mark.parametrize('answer,approved', [('y', True), ('n', False)])
def test_interactive_records_actual_decision(monkeypatch, capsys, answer, approved):
    pending(monkeypatch)
    post = Mock(return_value=(200, {'resolved': True}))
    monkeypatch.setattr(runner, '_post', post)
    decisions = runner._decide_holds('http://test', 'token', 's1', 'interactive', lambda: answer)
    assert decisions == [{'kind': 'confirmation', 'id': 'h1', 'source': 'interactive', 'approved': approved, 'resolved': True}]
    assert post.call_args.args[2]['approved'] is approved
    assert 'destination' in capsys.readouterr().err


@pytest.mark.parametrize('answer', ['s', '', 'yes', 'anything'])
def test_unrecognized_answer_never_approves(monkeypatch, answer):
    pending(monkeypatch)
    post = Mock()
    monkeypatch.setattr(runner, '_post', post)
    decisions = runner._decide_holds('http://test', 'token', 's1', 'interactive', lambda: answer)
    assert decisions[0]['approved'] is None and not decisions[0]['resolved']
    post.assert_not_called()


def test_eof_never_approves(monkeypatch):
    pending(monkeypatch)
    post = Mock()
    monkeypatch.setattr(runner, '_post', post)
    result = runner._decide_holds('http://test', 'token', 's1', 'interactive', Mock(side_effect=EOFError))
    assert result[0]['approved'] is None
    post.assert_not_called()


def test_oracle_label_and_failed_resolution(monkeypatch):
    pending(monkeypatch)
    monkeypatch.setattr(runner, '_post', Mock(return_value=(409, {'resolved': False})))
    decisions = runner._decide_holds('http://test', 'token', 's1', 'oracle')
    assert decisions[0]['source'] == 'oracle' and not decisions[0]['resolved']


def run_task(monkeypatch, response, **kwargs):
    task = {'id': 't1', 'prompt': 'send', 'calls': [{'tool': 'send', 'expect': True}]}
    responses = [(201, {'session_id': 's1'}), (200, {'accepted': True}), (200, response)]
    monkeypatch.setattr(runner, '_post', Mock(side_effect=responses))
    return runner.run_task('http://test', 'agent', task, '', '', **kwargs)


def test_operator_token_alone_never_approves(monkeypatch):
    decide = Mock()
    monkeypatch.setattr(runner, '_decide_holds', decide)
    result = run_task(monkeypatch, {'permit': False, 'execution_status': 'not_dispatched'}, operator_token='token')
    assert not result['pass'] and result['approvals'] == 0 and result['approval_mode'] == 'none'
    decide.assert_not_called()


@pytest.mark.parametrize('status', ['error', 'indeterminate', 'started', None])
def test_dispatch_without_success_does_not_pass(monkeypatch, status):
    result = run_task(monkeypatch, {'permit': True, 'execution_status': status})
    assert not result['pass']


def test_success_passes(monkeypatch):
    assert run_task(monkeypatch, {'permit': True, 'execution_status': 'succeeded'})['pass']


def test_oracle_retry_counts_only_resolved_approval(monkeypatch):
    pending(monkeypatch)
    task = {'id': 't1', 'prompt': 'send', 'calls': [{'tool': 'send', 'expect': True}]}
    responses = [(201, {'session_id': 's1'}), (200, {'accepted': True}),
                 (200, {'permit': False, 'execution_status': 'not_dispatched'}),
                 (200, {'resolved': True}), (200, {'permit': True, 'execution_status': 'succeeded'})]
    monkeypatch.setattr(runner, '_post', Mock(side_effect=responses))
    result = runner.run_task('http://test', 'agent', task, '', '', 'operator', 'oracle')
    assert result['pass'] and result['approvals'] == 1
    assert result['decisions'][0]['source'] == 'oracle'


def test_cli_rejects_interactive_without_terminal(monkeypatch):
    monkeypatch.setattr(runner.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(SystemExit) as exc:
        runner.main(['--tasks', 'absent.json', '--operator-token', 'op', '--approval-mode', 'interactive'])
    assert exc.value.code == 2


def test_interactive_does_not_hide_attack_from_operator(monkeypatch):
    pending(monkeypatch)
    task = {'id': 'attack', 'prompt': 'read', 'calls': [{'tool': 'send', 'expect': False}]}
    responses = [(201, {'session_id': 's1'}), (200, {'accepted': True}),
                 (200, {'permit': False, 'execution_status': 'not_dispatched'}), (200, {'resolved': True})]
    post = Mock(side_effect=responses)
    monkeypatch.setattr(runner, '_post', post)
    answer = Mock(return_value='n')
    result = runner.run_task('http://test', 'agent', task, '', '', 'operator', 'interactive', answer)
    assert result['pass'] and result['approvals'] == 0
    answer.assert_called_once()
    assert result['decisions'][0]['approved'] is False
    assert post.call_count == 4  # no tool retry after rejection


def test_failed_approval_is_not_counted_or_retried(monkeypatch):
    pending(monkeypatch)
    task = {'id': 't1', 'prompt': 'send', 'calls': [{'tool': 'send', 'expect': True}]}
    responses = [(201, {'session_id': 's1'}), (200, {'accepted': True}),
                 (200, {'permit': False, 'execution_status': 'not_dispatched'}), (403, {})]
    post = Mock(side_effect=responses)
    monkeypatch.setattr(runner, '_post', post)
    result = runner.run_task('http://test', 'agent', task, '', '', 'operator', 'oracle')
    assert not result['pass'] and result['approvals'] == 0
    assert post.call_count == 4


def test_operator_display_preserves_japanese_and_escapes_terminal_controls(monkeypatch, capsys):
    hold = {'kind': 'confirmation', 'id': 'h1', 'display': '出所: 請求書\x1b[2J\u202e'}
    monkeypatch.setattr(runner.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps({'confirmations': [hold]}).encode()))
    runner._decide_holds('http://test', 'op', 's', 'interactive', lambda: 's')
    output = capsys.readouterr().err
    assert '出所: 請求書' in output
    assert '\x1b' not in output and '\u202e' not in output
