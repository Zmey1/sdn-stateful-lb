"""Persistent HTTP/1.1 backend and local Mininet telemetry publisher.

The filesystem telemetry transport is intentionally lab-only: a filesystem
shared by Mininet network namespaces is not a production telemetry network.
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.parse import urlparse, parse_qs


class Stats:
    def __init__(self, name):
        self.name = name
        self.lock = threading.Lock()
        self.active = 0
        self.requests = 0
        self.latency_ms = 0.0
        self.errors = 0

    def snapshot(self):
        with self.lock:
            return {'backend': self.name, 'active': self.active,
                    'completed': self.requests, 'latency_ms': self.latency_ms,
                    'errors': self.errors}


def publish(stats: Stats, file: Path, shutdown: threading.Event):
    file.parent.mkdir(parents=True, exist_ok=True)
    hz = os.sysconf('SC_CLK_TCK')
    last_wall = time.monotonic()
    last_cpu = 0.0
    while not shutdown.is_set():
        try:
            fields = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()
            # utime and stime are columns 14 and 15, or indexes 11 and 12
            # after stripping pid and parenthesized comm.
            cpu_time = (int(fields[11]) + int(fields[12])) / hz
            now = time.monotonic()
            elapsed = max(.001, now - last_wall)
            cpu_pct = min(100.0, max(0.0, (cpu_time - last_cpu) / elapsed * 100.0))
            last_cpu, last_wall = cpu_time, now
            data = stats.snapshot()
            data.update({'cpu_pct': round(cpu_pct, 2), 'timestamp': time.time(),
                         'pid': os.getpid()})
            tmp = file.with_suffix('.tmp')
            tmp.write_text(json.dumps(data))
            tmp.replace(file)
        except Exception as exc:
            print(f'telemetry: {exc}', flush=True)
        shutdown.wait(1.0)


def runtime_workload(runtime: Path, backend: str) -> dict:
    """Lab-only fault injection, applied on /work (never /health)."""
    try:
        record = json.loads((runtime / 'workload_profile.json').read_text())
        item = record.get(backend, {})
        if not isinstance(item, dict):
            return {}
        return {k: max(0, min(1000, int(item.get(k, 0))))
                for k in ('delay_ms', 'burn_ms')}
    except (OSError, ValueError, TypeError):
        return {}


def make_handler(stats: Stats, runtime: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == '/health':
                payload = {'ok': True, 'server': stats.name}
            elif parsed.path == '/metrics':
                payload = stats.snapshot()
            elif parsed.path in ('/', '/work'):
                with stats.lock:
                    stats.active += 1
                start = time.monotonic()
                try:
                    params = parse_qs(parsed.query)
                    profile = runtime_workload(runtime, stats.name)
                    delay = max(0, min(1000, int(params.get('delay_ms', ['0'])[0]) +
                                             profile.get('delay_ms', 0)))
                    burn = max(0, min(300, int(params.get('burn_ms', ['0'])[0]) +
                                         profile.get('burn_ms', 0)))
                    if delay: time.sleep(delay / 1000)
                    if burn:
                        finish = time.monotonic() + burn / 1000
                        acc = 1
                        while time.monotonic() < finish:
                            acc = (acc * 1664525 + 1013904223) & 0xffffffff
                    payload = {'server': stats.name, 'pid': os.getpid(),
                               'request': self.path}
                    elapsed_ms = (time.monotonic() - start) * 1000
                    with stats.lock:
                        stats.requests += 1
                        stats.latency_ms = (0.25 * elapsed_ms
                                            + 0.75 * stats.latency_ms)
                finally:
                    with stats.lock: stats.active -= 1
            else:
                self.send_json(404, {'error': 'not found'})
                return
            self.send_json(200, payload)

        def send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            return

    return Handler


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--name', required=True)
    p.add_argument('--port', type=int, default=8080)
    p.add_argument('--runtime', type=Path, required=True)
    a = p.parse_args()
    stats = Stats(a.name)
    stop = threading.Event()
    # Bind/listen before announcing readiness via the first metrics file.
    server = ThreadingHTTPServer(('0.0.0.0', a.port), make_handler(stats, a.runtime))
    thread = threading.Thread(target=publish,
                              args=(stats, a.runtime / f'{a.name}.json', stop),
                              daemon=True)
    thread.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
