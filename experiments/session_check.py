"""Mininet client namespace worker: preserve same TCP sockets across MOD.

The parent topology runner creates a 'go' file only after changing group
weights, making the experiment repeatable without shell timing races.
"""
import argparse
import http.client
import json
from pathlib import Path
import time
from controller.config import VIP, VPORT


def probe_once(connection: http.client.HTTPConnection):
    connection.request('GET', '/work')
    response = connection.getresponse()
    assert response.status == 200, f'HTTP status={response.status}'
    return json.loads(response.read())['server']


def run(vip, port, connections, ready: Path, go: Path, result: Path):
    sockets = []
    try:
        for _ in range(connections):
            c = http.client.HTTPConnection(vip, port, timeout=6)
            first = probe_once(c)
            local_port = c.sock.getsockname()[1]
            sockets.append((c, first, local_port))
        ready.write_text(json.dumps({'opened': len(sockets), 'first': [s[1] for s in sockets]}))
        deadline = time.monotonic() + 30
        while not go.exists():
            if time.monotonic() > deadline:
                raise TimeoutError('parent did not modify SELECT group')
            time.sleep(.1)
        changes = []
        for conn, expected, source_port in sockets:
            new_server = probe_once(conn)
            new_source_port = conn.sock.getsockname()[1]
            if new_server != expected or new_source_port != source_port:
                changes.append({'expected': expected, 'observed': new_server,
                                'port_before': source_port, 'port_after': new_source_port})
        outcome = {'connections': len(sockets), 'failed': len(changes), 'details': changes}
        result.write_text(json.dumps(outcome, indent=2))
        if changes: raise AssertionError(outcome)
    finally:
        for conn, _, _ in sockets:
            conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--vip', default=VIP)
    p.add_argument('--port', type=int, default=VPORT)
    p.add_argument('--connections', type=int, default=30)
    for name in ['ready', 'go', 'result']:
        p.add_argument('--' + name, required=True, type=Path)
    a = p.parse_args()
    run(a.vip, a.port, a.connections, a.ready, a.go, a.result)


if __name__ == '__main__':
    main()
