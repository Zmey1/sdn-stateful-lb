"""Independent backend HTTP checks from Mininet probe namespace p1.

Started with net.get('p1').popen(), never through the load balancer's VIP.
Atomic shared-file telemetry is deliberately limited to this Mininet lab.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from http.client import HTTPConnection
from .config import BACKENDS
from .runtime import atomic_json


def probe_backend(backend, timeout: float = .75) -> dict:
    started = time.monotonic()
    try:
        # Direct backend IP, NEVER virtual service IP.
        # HTTPConnection connects directly, ignoring HTTP_PROXY variables.
        conn = HTTPConnection(backend.ip, backend.port, timeout=timeout)
        try:
            conn.request('GET', '/health', headers={'Connection': 'close'})
            response = conn.getresponse()
            body = json.loads(response.read(2048).decode())
            good = response.status == 200 and body.get('ok') is True \
                   and body.get('server') == backend.name
            if not good:
                raise ValueError('unexpected /health HTTP response or backend identity')
        finally:
            conn.close()
        return {'ok': True, 'timestamp': time.time(),
                'latency_ms': round((time.monotonic() - started) * 1000, 2)}
    except Exception as exc:
        return {'ok': False, 'timestamp': time.time(),
                'error': f'{type(exc).__name__}: {str(exc)[:160]}',
                'latency_ms': round((time.monotonic() - started) * 1000, 2)}


def run_probe_cycle(timeout: float = .75) -> dict:
    with ThreadPoolExecutor(max_workers=len(BACKENDS)) as pool:
        checks = list(pool.map(lambda b: probe_backend(b, timeout), BACKENDS))
    return {backend.name: result for backend, result in zip(BACKENDS, checks)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--runtime', required=True, type=Path)
    p.add_argument('--interval', type=float, default=2)
    p.add_argument('--timeout', type=float, default=.75)
    args = p.parse_args()
    if args.interval <= 0 or args.timeout <= 0:
        p.error('interval and timeout must be positive')
    args.runtime.mkdir(parents=True, exist_ok=True)
    while True:
        cycle = time.monotonic()
        checks = run_probe_cycle(args.timeout)
        atomic_json(args.runtime / 'health.json', checks)
        failures = [n for n, v in checks.items() if not v['ok']]
        if failures:
            print(f'health failure: {failures}', flush=True)
        time.sleep(max(0, args.interval - (time.monotonic() - cycle)))


if __name__ == '__main__':
    main()
