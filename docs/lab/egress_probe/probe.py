"""Real-kernel verification of the mounted lockdown script, inside a private netns."""
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import threading

spec = importlib.util.spec_from_file_location('health', '/probe/health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)
UID = pwd.getpwnam('pauth-agent').pw_uid
CASES = [
    ('gateway', socket.AF_INET, socket.SOCK_STREAM, '127.0.0.1', 18081),
    ('wrong_port', socket.AF_INET, socket.SOCK_STREAM, '127.0.0.1', 18082),
    ('other_address', socket.AF_INET, socket.SOCK_STREAM, '127.0.0.2', 18081),
    ('udp', socket.AF_INET, socket.SOCK_DGRAM, '127.0.0.1', 18083),
    ('ipv6', socket.AF_INET6, socket.SOCK_STREAM, '::1', 18084),
]
CLIENT = '''
import socket, sys
s=socket.socket(int(sys.argv[1]), int(sys.argv[2])); s.settimeout(0.5)
try:
 s.connect((sys.argv[3], int(sys.argv[4]))); s.send(b"probe")
 ok=s.recv(16)==b"ack"
except OSError:
 ok=False
s.close(); sys.exit(0 if ok else 1)
'''


def serve(sock, datagram):
    while True:
        try:
            if datagram:
                data, addr = sock.recvfrom(16)
                sock.sendto(b'ack', addr)
            else:
                conn, _ = sock.accept()
                with conn:
                    conn.settimeout(1)
                    conn.recv(16)
                    conn.sendall(b'ack')
        except OSError:
            return


def reachability():
    results = {}
    for name, family, kind, host, port in CASES:
        child = subprocess.run([sys.executable, '-c', CLIENT, str(int(family)), str(int(kind)), host, str(port)],
                               user=UID, group=UID, extra_groups=[], timeout=3, capture_output=True)
        if child.returncode not in (0, 1):
            raise RuntimeError(f'probe failed for {name}: {child.stderr.decode()}')
        results[name] = child.returncode == 0
    return results


def lockdown(action):
    env = dict(os.environ, AGENT_USER='pauth-agent', GATEWAY_HOST='127.0.0.1', GATEWAY_PORT='18081')
    run = subprocess.run(['/bin/bash', '/probe/egress_lockdown.sh', action], env=env,
                         capture_output=True, text=True, timeout=15)
    if run.returncode:
        raise RuntimeError(run.stderr)


def exercise(backend):
    before = reachability()
    assert all(before.values()), before
    lockdown('apply')
    during = reachability()
    assert during == {name: name == 'gateway' for name, *_ in CASES}, during
    active = health.egress_health('pauth-agent')
    assert active['status'] == 'ok', active
    lockdown('remove')
    removed = health.egress_health('pauth-agent')
    assert removed['status'] == 'fail', removed
    after = reachability()
    assert all(after.values()), after
    return {'backend': backend, 'before': before, 'during': during, 'after': after,
            'health_active': active, 'health_removed': removed}


def main():
    sockets = []
    try:
        for _, family, kind, host, port in CASES:
            sock = socket.socket(family, kind)
            sockets.append(sock)
            sock.bind((host, port))
            if kind == socket.SOCK_STREAM:
                sock.listen()
            threading.Thread(target=serve, args=(sock, kind == socket.SOCK_DGRAM), daemon=True).start()
        results = [exercise('nftables')]
        # Exercise the actual deploy script's iptables fallback without altering
        # host binaries. A private PATH exposes its dependencies but omits nft.
        private_bin = Path('/tmp/iptables-path')
        private_bin.mkdir()
        for name in ('bash', 'id', 'uname', 'cat', 'iptables', 'ip6tables'):
            target = shutil.which(name)
            assert target, name
            (private_bin / name).symlink_to(target)
        os.environ['PATH'] = str(private_bin)
        results.append(exercise('iptables'))
        print(json.dumps({'environment': 'private Linux container, network none',
                          'agent_uid': UID, 'passed': True, 'results': results}, indent=2), flush=True)
    finally:
        for sock in sockets:
            sock.close()


if __name__ == '__main__':
    main()
