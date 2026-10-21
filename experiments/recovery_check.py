"""Live Mininet integration experiment: backend crash -> exclusion -> recovery.

Run from a second terminal while the lab (./start_dev.sh)
is active. Only changes the named backend worker in this throwaway lab.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from controller.cli import restart_backend
from controller.config import BACKENDS, VIP, VPORT
from controller.runtime import read_dict


def status(runtime: Path) -> dict:
    return read_dict(runtime / 'status.json')


def wait_state(runtime: Path, name: str, expected: str,
               timeout: float = 65.0, require_positive: bool | None = None) -> dict:
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        data = status(runtime)
        backend = data.get('backends', {}).get(name, {})
        weight = data.get('installed_weights', {}).get(name, 0)
        last = (backend.get('state'), weight)
        if backend.get('state') == expected and (
            require_positive is None or (weight > 0) == require_positive
        ):
            return data
        time.sleep(.5)
    raise RuntimeError(f'Timed out waiting for {name}={expected}, last={last}')


def verify_new_connections(runtime: Path, excluded: str, count: int) -> None:
    hosts = json.loads((runtime / 'host_pids.json').read_text())
    script = '''import http.client,json,sys
for i in range(int(sys.argv[1])):
    conn=http.client.HTTPConnection(sys.argv[3],int(sys.argv[4]),timeout=2)
    try:
        conn.request("GET","/work",headers={"Connection":"close"})
        res=conn.getresponse()
        if res.status!=200: raise RuntimeError("HTTP "+str(res.status))
        backend=json.loads(res.read())["server"]
        if backend==sys.argv[2]: raise RuntimeError("sent new connection to DOWN server "+backend)
        print(backend)
    finally:
        conn.close()
'''
    cmd = ['mnexec', '-a', str(hosts['h1']), sys.executable, '-c',
           script, str(count), excluded, VIP, str(VPORT)]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=max(20, count * 3))
    if proc.returncode:
        raise RuntimeError(f'New-connection test failed: {proc.stdout}\n{proc.stderr}')
    print(f'PASS: {count} new TCP connections avoided {excluded} '
          f'(targets: {dict((n, proc.stdout.splitlines().count(n)) for n in (b.name for b in BACKENDS))})')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--runtime', required=True, type=Path)
    p.add_argument('--backend', choices=[b.name for b in BACKENDS], default='b1')
    p.add_argument('--requests', type=int, default=20)
    args = p.parse_args()
    if os.geteuid():
        p.error('Run as root')
    if not args.runtime.joinpath('host_pids.json').exists():
        p.error('Live Mininet runtime not found; start ./start_dev.sh first')

    name = args.backend
    print(f'Waiting for {name} to be HEALTHY ...', flush=True)
    wait_state(args.runtime, name, 'HEALTHY', timeout=70, require_positive=True)
    pid = int(json.loads((args.runtime / f'{name}.json').read_text())['pid'])
    print(f'Crashing {name} worker PID={pid}', flush=True)
    os.kill(pid, signal.SIGTERM)
    print('Waiting for automatic DOWN + SELECT-group removal ...', flush=True)
    wait_state(args.runtime, name, 'DOWN', timeout=25, require_positive=False)
    print('PASS: failed server removed from new-connection selection')
    verify_new_connections(args.runtime, name, args.requests)

    print('Restarting backend HTTP worker in existing namespace ...', flush=True)
    restart_backend(name, args.runtime)
    recovered = wait_state(args.runtime, name, 'RECOVERING',
                           timeout=25, require_positive=True)
    first = recovered['installed_weights'][name]
    print(f'PASS: recovering backend admitted at reduced weight {first}', flush=True)
    ready = wait_state(args.runtime, name, 'HEALTHY',
                       timeout=70, require_positive=True)
    final = ready['installed_weights'][name]
    if final <= first:
        raise RuntimeError(f'Recovery weights failed to ramp: {first} -> {final}')
    print(f'PASS: {name} recovered and weight increased {first} -> {final}', flush=True)
    print('MILESTONE 3 HEALTH/RECOVERY TEST PASSED')


if __name__ == '__main__':
    main()
