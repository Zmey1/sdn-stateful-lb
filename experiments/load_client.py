"""One TCP connection per request; run inside a Mininet client namespace.

Produces one compact JSON document on stdout. Uses a fixed offered request
rate (open-loop) and bounded in-flight requests instead of closed-loop curl.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import math
import threading
from pathlib import Path
import time
from controller.config import BACKENDS, VIP, VPORT
from controller.runtime import atomic_json

BACKEND_NAMES = {b.name for b in BACKENDS}


def request_once(timeout: float = 3.0) -> tuple[float, str | None, str | None]:
    begin = time.perf_counter()
    conn = http.client.HTTPConnection(VIP, VPORT, timeout=timeout)
    try:
        conn.request('GET', '/work', headers={'Connection': 'close'})
        reply = conn.getresponse()
        body = reply.read()
        if reply.status != 200:
            raise RuntimeError(f'HTTP {reply.status}')
        server = json.loads(body).get('server')
        if server not in BACKEND_NAMES:
            raise RuntimeError('Response without valid backend identity')
        return (1000 * (time.perf_counter() - begin), server, None)
    except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
        return (1000 * (time.perf_counter() - begin), None,
                f'{type(exc).__name__}: {str(exc)[:90]}')
    finally:
        conn.close()


def timeline_points(outcomes: list) -> list[dict]:
    """Per-second client view: [(second sent, latency_ms or None)] ->
    requests sent, failed, and p95 of the successful ones."""
    seconds = {}
    for second, latency in outcomes:
        seconds.setdefault(second, []).append(latency)
    points = []
    for second in sorted(seconds):
        ok = sorted(x for x in seconds[second] if x is not None)
        points.append({'t': second, 'sent': len(seconds[second]),
                       'failed': len(seconds[second]) - len(ok),
                       'p95_ms': round(ok[min(len(ok) - 1, int(.95 * len(ok)))], 1)
                       if ok else None})
    return points


def run(rate: float, seconds: float, max_inflight: int, timeout: float,
        timeline: Path | None = None):
    """Generate starts at a fixed rate; flag any excess instead of hiding load.

    With `timeline`, also publish per-second latency and failures while
    running, so the dashboard can chart what clients experience.
    """
    if rate <= 0 or seconds <= 0 or max_inflight < 1:
        raise ValueError('rate, seconds and max-inflight must be positive')
    lock = threading.Lock()
    sem = threading.BoundedSemaphore(max_inflight)
    latencies = []
    backends = Counter()
    errors = Counter()
    rejected = 0
    offered = 0
    workers = ThreadPoolExecutor(max_workers=max_inflight)
    outcomes = []  # (second the request was sent, latency_ms or None)

    def task(second):
        try:
            latency, backend, error = request_once(timeout)
            with lock:
                outcomes.append((second, latency if backend else None))
                if backend:
                    latencies.append(round(latency, 4))
                    backends[backend] += 1
                else:
                    errors[error or 'unknown'] += 1
        finally:
            sem.release()

    start = time.perf_counter()
    done = threading.Event()

    def publish():
        while not done.wait(1.0):
            with lock:
                points = timeline_points(outcomes)
            atomic_json(timeline, {'points': points})

    if timeline is not None:
        threading.Thread(target=publish, daemon=True).start()
    n = math.ceil(rate * seconds)
    for idx in range(n):
        target = start + idx / rate
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        offered += 1
        second = int(idx / rate)
        if not sem.acquire(blocking=False):
            rejected += 1
            with lock:
                outcomes.append((second, None))
        else:
            workers.submit(task, second)
    workers.shutdown(wait=True)
    done.set()
    if timeline is not None:
        atomic_json(timeline, {'points': timeline_points(outcomes)})
    elapsed = time.perf_counter() - start
    return {'duration_target_s': seconds, 'duration_actual_s': round(elapsed, 4),
            'offered': offered, 'backpressure_rejected': rejected,
            'success': sum(backends.values()), 'errors': dict(errors),
            'backends': dict(backends), 'latencies_ms': latencies}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--rate', type=float, required=True)
    p.add_argument('--warmup', type=float, default=5)
    p.add_argument('--duration', type=float, default=15)
    p.add_argument('--max-inflight', type=int, default=50)
    p.add_argument('--timeout', type=float, default=3)
    p.add_argument('--timeline', type=Path,
                   help='write per-second latency/failures here while running')
    a = p.parse_args()
    if a.warmup > 0:
        run(a.rate, a.warmup, a.max_inflight, a.timeout)
    result = run(a.rate, a.duration, a.max_inflight, a.timeout, a.timeline)
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
