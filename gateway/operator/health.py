"""Read-only deployment health probes shared by HTTP and hook entry points.

HTTP: deployment_health() returns value-free checks. Hook: run this module
with --url; exit 1 signals a failed or unverifiable protection check.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import shlex
import subprocess
import sys
import urllib.request


def check(status: str, code: str) -> dict:
    return {'status': status, 'code': code}


HOOK_DIRECTORY = Path(__file__).resolve().parents[1] / 'hooks'


def _hook_command(command: str, script: str) -> dict:
    """Verify a supported invocation without executing untrusted settings text.

    Shell programs, wrappers, and environment expansion cannot be established
    by filename matching. Report them as unknown instead of claiming coverage.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return check('unknown', 'hook_command_unverifiable')
    interpreted = False
    if len(words) == 2 and words[0] in ('bash', '/bin/bash', '/usr/bin/bash'):
        if shutil.which(words[0]) is None:
            return check('fail', 'hook_interpreter_missing')
        interpreted = True
        words = words[1:]
    if len(words) != 1 or any(c in words[0] for c in ('$', '`', ';', '|', '&', '<', '>', '\n')):
        return check('unknown', 'hook_command_unverifiable')
    target = Path(words[0])
    if not target.is_absolute() or target.name != script:
        return check('unknown', 'hook_command_unverifiable')
    if not target.is_file():
        return check('fail', 'hook_script_missing')
    # A same-named script elsewhere is not evidence for this installation.
    if target.resolve() != (HOOK_DIRECTORY / script).resolve():
        return check('unknown', 'hook_script_unverified')
    mode = os.R_OK if interpreted else os.R_OK | os.X_OK
    if not os.access(target, mode):
        return check('fail', 'hook_script_inaccessible')
    return check('ok', 'hooks_registered')


def hook_health(settings_path: str | None = None) -> dict:
    path = Path(settings_path or os.environ.get('PAUTH_HOOK_SETTINGS', str(Path.home() / '.claude/settings.json')))
    try:
        data = json.loads(path.read_text())
        if data.get('disableAllHooks'):
            return check('fail', 'hooks_disabled')
        hooks = data.get('hooks', {})
        required = {'UserPromptSubmit': 'submit_prompt.sh', 'PreToolUse': 'pretool.sh'}
        for event, script in required.items():
            candidates = []
            covered = False
            for entry in hooks.get(event, []):
                # Restricted matchers do not cover all tool calls / prompts.
                if entry.get('matcher', '') not in ('', '*'):
                    candidates.append(check('fail', 'hook_coverage_incomplete'))
                    continue
                for hook in entry.get('hooks', []):
                    if hook.get('type') != 'command':
                        continue
                    if hook.get('async'):
                        candidates.append(check('fail', 'hook_async'))
                        continue
                    result = _hook_command(hook.get('command', ''), script)
                    candidates.append(result)
                    covered = covered or result['status'] == 'ok'
            if not covered:
                return candidates[0] if candidates else check('fail', 'hook_unregistered')
        return check('ok', 'hooks_registered')
    except FileNotFoundError:
        return check('fail', 'hook_unregistered')
    except (OSError, ValueError, TypeError, AttributeError):
        return check('unknown', 'hook_settings_unreadable')


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=3, check=True).stdout


def egress_health(agent_user: str | None = None) -> dict:
    user = agent_user or os.environ.get('AGENT_USER')
    if not user:
        return check('unknown', 'agent_user_unconfigured')
    try:
        uid = pwd.getpwnam(user).pw_uid
        if uid == 0:
            return check('fail', 'agent_user_privileged')
        if platform.system() == 'Darwin':
            info = _run(['pfctl', '-s', 'info'])
            if not re.search(r'Status:\s+Enabled', info):
                return check('fail', 'egress_disabled')
            root = _run(['pfctl', '-s', 'rules'])
            rules = _run(['pfctl', '-a', 'pauth_egress', '-s', 'rules'])
            if not re.search(r'anchor "pauth_egress"', root):
                return check('fail', 'egress_anchor_detached')
            matches = [line for line in rules.splitlines() if re.search(r'\buser\s+(?:=\s*)?(?:' + str(uid) + '|' + re.escape(user) + r')\b', line)]
            if not any('block' in line and 'out' in line for line in matches):
                return check('fail', 'egress_rules_missing')
            # Detect presence, not full firewall policy correctness.
            return check('ok', 'egress_rules_present')
        if platform.system() == 'Linux':
            if shutil.which('nft') is None:
                return _iptables_health(uid)
            raw = _run(['nft', '-j', 'list', 'ruleset'])
            objects = json.loads(raw)['nftables']
            chains = [x['chain'] for x in objects if 'chain' in x]
            if not any(c.get('family') == 'inet' and c.get('table') == 'pauth_egress' and c.get('name') == 'out' and c.get('hook') == 'output' for c in chains):
                return check('fail', 'egress_anchor_detached')
            for item in objects:
                rule = item.get('rule', {})
                if rule.get('table') != 'pauth_egress' or rule.get('chain') != 'out':
                    continue
                expr = rule.get('expr', [])
                owner = any(e.get('match', {}).get('left') == {'meta': {'key': 'skuid'}} and e['match'].get('op') == '==' and e['match'].get('right') in (uid, user) for e in expr)
                if owner and any('drop' in e or 'reject' in e for e in expr):
                    return check('ok', 'egress_rules_present')
            return check('fail', 'egress_rules_missing')
        return check('unknown', 'egress_platform_unsupported')
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return check('unknown', 'egress_probe_unavailable')


def _iptables_health(uid: int) -> dict:
    # Both families are required; an IPv4-only rule leaves an IPv6 bypass.
    for binary in ('iptables', 'ip6tables'):
        output = _run([binary, '-S', 'OUTPUT'])
        attached = re.compile(r'^-A OUTPUT -m owner --uid-owner ' + str(uid) + r' -j pauth_egress$')
        if not any(attached.fullmatch(line.strip()) for line in output.splitlines()):
            return check('fail', 'egress_anchor_detached')
        rules = _run([binary, '-S', 'pauth_egress'])
        if not any(re.fullmatch(r'-A pauth_egress -j (?:REJECT(?: --reject-with \S+)?|DROP)', line.strip()) for line in rules.splitlines()):
            return check('fail', 'egress_rules_missing')
    return check('ok', 'egress_rules_present')


def deployment_health(*, settings_path: str | None = None, agent_user: str | None = None) -> dict:
    checks = {'hooks': hook_health(settings_path), 'egress': egress_health(agent_user)}
    return {'healthy': all(c['status'] == 'ok' for c in checks.values()), 'checks': checks}


def daemon_health(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip('/') + '/health', timeout=12) as response:
            data = json.load(response)
        if not isinstance(data, dict) or data.get('status') != 'ok':
            return check('fail', 'daemon_unhealthy')
        return check('ok', 'daemon_running')
    except (OSError, ValueError):
        return check('fail', 'daemon_unreachable')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('GATEWAY_URL', 'http://127.0.0.1:8081'))
    parser.add_argument('--settings')
    parser.add_argument('--agent-user')
    args = parser.parse_args(argv)
    report = deployment_health(settings_path=args.settings, agent_user=args.agent_user)
    report['checks']['daemon'] = daemon_health(args.url)
    report['healthy'] = all(c['status'] == 'ok' for c in report['checks'].values())
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['healthy'] else 1


if __name__ == '__main__':
    sys.exit(main())
