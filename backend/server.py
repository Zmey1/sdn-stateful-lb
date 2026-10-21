"""HTTP/1.1 backend with finite capacity, and local Mininet telemetry publisher.

Each /work request waits for one of a fixed number of workers, then holds it
for the service time. When offered load exceeds capacity a queue builds and
response time grows, as on a real server. Sleeping (rather than burning CPU)
keeps backends independent of each other on one shared lab machine.

The filesystem telemetry transport is intentionally lab-only: a filesystem
shared by Mininet network namespaces is not a production telemetry network.
"""
import argparse
from contextlib import contextmanager
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.parse import urlparse, parse_qs

from controller.config import BACKEND_SERVICE_MS, BACKEND_WORKERS, VPORT
from controller.runtime import atomic_json, read_dict

MAX_DELAY_MS = 1000


class Stats:
    """Cumulative counters the controller differences over any interval.

    arrived   = requests accepted (queued or not)
    busy_s    = integral of busy workers over time
    completed = requests finished
    Offered load per worker = (Δarrived/Δt) * (Δbusy_s/Δcompleted) / workers:
    the utilization below saturation, above 1 when arrivals exceed capacity.
    Unlike queue length it has no memory of past overload.
    """

    def __init__(self, name: str, workers: int):
        self.name = name
        self.workers = workers
        self.slots = threading.BoundedSemaphore(workers)
        self.lock = threading.Lock()
        self.in_system = 0
        self.busy = 0
        self.busy_s = 0.0
        self.arrived = 0
        self.completed = 0
        self.response_ms = 0.0
        self._mark = time.monotonic()

    def _advance(self) -> None:
        """Accumulate integrals up to now; caller holds the lock."""
        now = time.monotonic()
        self.busy_s += self.busy * (now - self._mark)
        self._mark = now

    @contextmanager
    def counting(self, attribute: str):
        with self.lock:
            self._advance()
            setattr(self, attribute, getattr(self, attribute) + 1)
        try:
            yield
        finally:
            with self.lock:
                self._advance()
                setattr(self, attribute, getattr(self, attribute) - 1)

    def arrive(self) -> None:
        with self.lock:
            self.arrived += 1

    def record(self, response_ms: float) -> None:
        with self.lock:
            self.completed += 1
            self.response_ms = 0.25 * response_ms + 0.75 * self.response_ms

    def snapshot(self) -> dict:
        with self.lock:
            self._advance()
            return {'backend': self.name, 'workers': self.workers,
                    'in_system': self.in_system, 'busy': self.busy,
                    'busy_s': round(self.busy_s, 6),
                    'arrived': self.arrived, 'completed': self.completed,
                    'response_ms': round(self.response_ms, 3)}


def publish(stats: Stats, file: Path, shutdown: threading.Event):
    while not shutdown.is_set():
        try:
            data = stats.snapshot()
            data.update({'timestamp': time.time(), 'pid': os.getpid()})
            atomic_json(file, data)
        except OSError as exc:
            print(f'telemetry: {exc}', flush=True)
        shutdown.wait(1.0)


def injected_delay_ms(runtime: Path, backend: str) -> int:
    """Lab-only fault injection, applied on /work (never /health)."""
    item = read_dict(runtime / 'workload_profile.json').get(backend, {})
    try:
        return max(0, min(MAX_DELAY_MS, int(item.get('delay_ms', 0))))
    except (AttributeError, TypeError, ValueError):
        return 0


def make_handler(stats: Stats, runtime: Path, service_ms: int):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == '/health':
                self.send_json(200, {'ok': True, 'server': stats.name})
            elif parsed.path == '/metrics':
                self.send_json(200, stats.snapshot())
            elif parsed.path in ('/', '/work'):
                try:
                    extra = int(parse_qs(parsed.query).get('delay_ms', ['0'])[0])
                except ValueError:
                    extra = 0
                delay = min(MAX_DELAY_MS, max(0, extra) +
                            injected_delay_ms(runtime, stats.name))
                start = time.monotonic()
                stats.arrive()
                with stats.counting('in_system'):
                    with stats.slots, stats.counting('busy'):
                        time.sleep((service_ms + delay) / 1000)
                stats.record((time.monotonic() - start) * 1000)
                self.send_json(200, {'server': stats.name, 'pid': os.getpid(),
                                     'request': self.path})
            else:
                self.send_json(404, {'error': 'not found'})

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
    p.add_argument('--port', type=int, default=VPORT)
    p.add_argument('--runtime', type=Path, required=True)
    p.add_argument('--workers', type=int,
                   help='default: this backend\'s size in controller/config.py')
    p.add_argument('--service-ms', type=int, default=BACKEND_SERVICE_MS)
    a = p.parse_args()
    if a.workers is None:
        a.workers = BACKEND_WORKERS.get(a.name, 4)
    if a.workers < 1 or a.service_ms < 0:
        p.error('workers must be >= 1 and service-ms >= 0')
    stats = Stats(a.name, a.workers)
    stop = threading.Event()
    # Bind/listen before announcing readiness via the first metrics file.
    server = ThreadingHTTPServer(('0.0.0.0', a.port),
                                 make_handler(stats, a.runtime, a.service_ms))
    server.daemon_threads = True
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
