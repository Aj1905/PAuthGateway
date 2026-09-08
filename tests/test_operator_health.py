import json
from unittest.mock import Mock
import pytest
from gateway.operator import health


def test_hook_missing_disabled_and_registered(tmp_path):
    p = tmp_path / 'settings.json'
    assert health.hook_health(str(p))['code'] == 'hook_unregistered'
    p.write_text('{}')
    assert health.hook_health(str(p))['status'] == 'fail'
    hooks = {event: [{'hooks': [{'type': 'command', 'command': str(health.HOOK_DIRECTORY / script)}]}]
             for event, script in [('UserPromptSubmit', 'submit_prompt.sh'), ('PreToolUse', 'pretool.sh')]}
    p.write_text(json.dumps({'hooks': hooks}))
    assert health.hook_health(str(p))['status'] == 'ok'
    p.write_text(json.dumps({'hooks': hooks, 'disableAllHooks': True}))
    assert health.hook_health(str(p))['status'] == 'fail'
    p.write_text('null')
    assert health.hook_health(str(p))['status'] == 'unknown'


def test_egress_permission_unknown(monkeypatch):
    monkeypatch.setattr(health.pwd, 'getpwnam', lambda _: Mock(pw_uid=501))
    monkeypatch.setattr(health.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(health, '_run', Mock(side_effect=PermissionError))
    assert health.egress_health('agent')['status'] == 'unknown'


@pytest.mark.parametrize('enabled,root,rules,code', [
    ('Status: Disabled', '', '', 'egress_disabled'),
    ('Status: Enabled', '', 'block drop out user = 501', 'egress_anchor_detached'),
    ('Status: Enabled', 'anchor "pauth_egress" all', '', 'egress_rules_missing'),
    ('Status: Enabled', 'anchor "pauth_egress" all', 'block drop out proto tcp user = 502', 'egress_rules_missing'),
    ('Status: Enabled', 'anchor "pauth_egress" all', 'block drop out proto tcp user = 501', 'egress_rules_present'),
])
def test_pf(monkeypatch, enabled, root, rules, code):
    monkeypatch.setattr(health.pwd, 'getpwnam', lambda _: Mock(pw_uid=501))
    monkeypatch.setattr(health.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(health, '_run', Mock(side_effect=[enabled, root, rules]))
    assert health.egress_health('agent')['code'] == code


def test_nft_attached_uid_drop(monkeypatch):
    monkeypatch.setattr(health.shutil, 'which', lambda _: '/sbin/nft')
    monkeypatch.setattr(health.pwd, 'getpwnam', lambda _: Mock(pw_uid=501))
    monkeypatch.setattr(health.platform, 'system', lambda: 'Linux')
    chain = {'chain': {'family': 'inet', 'table': 'pauth_egress', 'name': 'out', 'hook': 'output'}}
    rule = {'rule': {'table': 'pauth_egress', 'chain': 'out', 'expr': [{'match': {'left': {'meta': {'key': 'skuid'}}, 'op': '==', 'right': 501}}, {'drop': None}]}}
    monkeypatch.setattr(health, '_run', lambda _: json.dumps({'nftables': [chain, rule]}))
    assert health.egress_health('agent')['status'] == 'ok'
    rule['rule']['expr'][0]['match']['op'] = '!='
    assert health.egress_health('agent')['status'] == 'fail'


def test_daemon_down_and_live(monkeypatch):
    monkeypatch.setattr(health.urllib.request, 'urlopen', Mock(side_effect=ConnectionRefusedError))
    assert health.daemon_health('http://127.0.0.1:1')['code'] == 'daemon_unreachable'
    from io import BytesIO
    monkeypatch.setattr(health.urllib.request, 'urlopen', lambda *a, **k: BytesIO(b'{"status":"ok"}'))
    assert health.daemon_health('http://127.0.0.1:1')['status'] == 'ok'


def test_iptables_requires_both_families(monkeypatch):
    monkeypatch.setattr(health.pwd, 'getpwnam', lambda _: Mock(pw_uid=501))
    monkeypatch.setattr(health.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(health.shutil, 'which', lambda _: None)
    outputs = ['-A OUTPUT -m owner --uid-owner 501 -j pauth_egress', '-A pauth_egress -j REJECT']
    monkeypatch.setattr(health, '_run', Mock(side_effect=outputs + outputs))
    assert health.egress_health('agent')['status'] == 'ok'
    monkeypatch.setattr(health, '_run', Mock(side_effect=outputs + ['']))
    assert health.egress_health('agent')['code'] == 'egress_anchor_detached'


@pytest.fixture
def hook_settings(tmp_path, monkeypatch):
    directory = tmp_path / 'hooks with spaces'
    directory.mkdir()
    monkeypatch.setattr(health, 'HOOK_DIRECTORY', directory)
    hooks = {}
    for event, script in [('UserPromptSubmit', 'submit_prompt.sh'), ('PreToolUse', 'pretool.sh')]:
        target = directory / script
        target.write_text('#!/bin/bash\nexit 0\n')
        target.chmod(0o700)
        hooks[event] = [{'hooks': [{'type': 'command', 'command': f'"{target}"'}]}]
    return tmp_path / 'settings.json', hooks


def test_quoted_real_hooks_and_interpreter(hook_settings):
    path, hooks = hook_settings
    path.write_text(json.dumps({'hooks': hooks}))
    assert health.hook_health(str(path))['status'] == 'ok'
    hook = hooks['PreToolUse'][0]['hooks'][0]
    hook['command'] = '/bin/bash ' + hook['command']
    path.write_text(json.dumps({'hooks': hooks}))
    assert health.hook_health(str(path))['status'] == 'ok'


@pytest.mark.parametrize('prefix,suffix', [('echo ', ''), ('', '; true'), ('', ' || true'), ('bash -c ', ''), ('env ', '')])
def test_shell_mentions_are_not_registration(hook_settings, prefix, suffix):
    path, hooks = hook_settings
    hook = hooks['PreToolUse'][0]['hooks'][0]
    hook['command'] = prefix + hook['command'] + suffix
    path.write_text(json.dumps({'hooks': hooks}))
    assert health.hook_health(str(path)) == health.check('unknown', 'hook_command_unverifiable')


def test_deleted_hook_is_detected(hook_settings):
    path, hooks = hook_settings
    path.write_text(json.dumps({'hooks': hooks}))
    (health.HOOK_DIRECTORY / 'pretool.sh').unlink()
    assert health.hook_health(str(path)) == health.check('fail', 'hook_script_missing')


def test_non_executable_hook_is_detected(hook_settings):
    path, hooks = hook_settings
    path.write_text(json.dumps({'hooks': hooks}))
    (health.HOOK_DIRECTORY / 'pretool.sh').chmod(0o600)
    assert health.hook_health(str(path)) == health.check('fail', 'hook_script_inaccessible')


@pytest.mark.parametrize('mutation,code', [('matcher', 'hook_coverage_incomplete'), ('async', 'hook_async')])
def test_partial_or_async_hook_not_healthy(hook_settings, mutation, code):
    path, hooks = hook_settings
    entry = hooks['PreToolUse'][0]
    if mutation == 'matcher':
        entry['matcher'] = 'Read'
    else:
        entry['hooks'][0]['async'] = True
    path.write_text(json.dumps({'hooks': hooks}))
    assert health.hook_health(str(path)) == health.check('fail', code)


def test_same_named_unverified_script_is_not_healthy(hook_settings, tmp_path):
    path, hooks = hook_settings
    substitute = tmp_path / 'pretool.sh'
    substitute.write_text('#!/bin/bash\nexit 0\n')
    substitute.chmod(0o700)
    hooks['PreToolUse'][0]['hooks'][0]['command'] = f'"{substitute}"'
    path.write_text(json.dumps({'hooks': hooks}))
    assert health.hook_health(str(path)) == health.check('unknown', 'hook_script_unverified')


def test_missing_interpreter_is_detected(hook_settings, monkeypatch):
    path, hooks = hook_settings
    hook = hooks['PreToolUse'][0]['hooks'][0]
    hook['command'] = 'bash ' + hook['command']
    monkeypatch.setattr(health.shutil, 'which', lambda _: None)
    path.write_text(json.dumps({'hooks': hooks}))
    assert health.hook_health(str(path)) == health.check('fail', 'hook_interpreter_missing')
