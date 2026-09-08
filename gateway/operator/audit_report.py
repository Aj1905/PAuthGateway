"""Operator-only, session-grouped report of the persistent audit JSONL."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import unicodedata

LABELS = {'accept': '受理', 'reject': '棄却', 'permit': '許可', 'deny': '拒否',
          'pending': '保留', 'error': 'ツールエラー', 'indeterminate': '結果不明'}


def safe(value: object) -> str:
    text = str(value)
    return ''.join(f'\\u{ord(c):04x}' if unicodedata.category(c).startswith('C') else c for c in text)


def read_events(path: Path) -> tuple[dict[str, list[dict]], list[str]]:
    sessions: dict[str, list[dict]] = {}
    errors = []
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not all(isinstance(event.get(k), str) for k in ('kind', 'decision', 'reason_code', 'reason')):
                    raise ValueError('invalid audit event')
                sid = event.get('session_id')
                if sid is not None and not isinstance(sid, str):
                    raise ValueError('invalid session_id')
                sessions.setdefault(sid or '(セッション不明: 旧形式)', []).append(event)
            except (ValueError, TypeError):
                errors.append(f'{number}行目: 不正な監査記録（読み飛ばし）')
    return sessions, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit-log', required=True, type=Path)
    parser.add_argument('--session', help='表示するセッションID')
    parser.add_argument('--show-args', action='store_true', help='運用者向けの引数値を表示')
    args = parser.parse_args(argv)
    try:
        sessions, errors = read_events(args.audit_log)
    except (OSError, UnicodeError) as exc:
        print(f'監査ログを読めません: {safe(exc)}', file=sys.stderr)
        return 2
    for sid, events in sessions.items():
        if args.session is not None and sid != args.session:
            continue
        print(f'セッション: {safe(sid)}')
        counts = Counter(e['decision'] for e in events)
        print('  ' + ' / '.join(f'{LABELS.get(k, safe(k))}: {v}' for k, v in counts.items()))
        for e in events:
            print(f"  #{safe(e.get('seq', '?'))} {LABELS.get(e['decision'], safe(e['decision']))} {safe(e['kind'])} {safe(e.get('tool') or '-')} [{safe(e['reason_code'])}] {safe(e['reason'])}")
            if args.show_args and e.get('args') is not None:
                print('    引数: ' + safe(json.dumps(e['args'], ensure_ascii=False)))
    if not sessions or (args.session is not None and args.session not in sessions):
        print('該当する監査記録はありません。')
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
