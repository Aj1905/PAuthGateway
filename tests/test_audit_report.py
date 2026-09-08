import json
from gateway.runtime.audit import AuditLog
from gateway.operator.audit_report import main, read_events


def test_sessions_and_report(tmp_path, capsys):
    path = tmp_path / 'audit.jsonl'
    root = AuditLog(path)
    a, b = root.for_session('a'), root.for_session('b')
    a.record('tool_call', 'permit', tool='read', reason_code='ok', reason='matched')
    b.record('tool_call', 'deny', tool='send', reason_code='outside', reason='not planned\x1b[2J', args=['secret'])
    assert len(a.events()) == len(b.events()) == 1
    assert set(read_events(path)[0]) == {'a', 'b'}
    assert main(['--audit-log', str(path)]) == 0
    output = capsys.readouterr().out
    assert '許可' in output and '拒否' in output and 'not planned' in output
    assert '\x1b' not in output and 'secret' not in output
    assert main(['--audit-log', str(path), '--session', 'b', '--show-args']) == 0
    output = capsys.readouterr().out
    assert 'セッション: a' not in output and 'secret' in output


def test_old_and_corrupt_records(tmp_path, capsys):
    path = tmp_path / 'audit.jsonl'
    path.write_text(json.dumps(dict(seq=0, kind='submit', decision='reject', reason_code='rejected', reason='bad')) + '\n{}\n{"torn":')
    assert main(['--audit-log', str(path)]) == 1
    out = capsys.readouterr()
    assert 'セッション不明' in out.out and '棄却' in out.out
    assert '2行目' in out.err and '3行目' in out.err
    assert main(['--audit-log', str(tmp_path / 'absent')]) == 2
