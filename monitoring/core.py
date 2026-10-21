"""Safe, bounded reads of the lab runtime directory; no datapath changes."""
from __future__ import annotations

from collections import deque
import csv
import math
import os
from pathlib import Path
import threading
import time

from controller.config import BACKENDS, VIP, VPORT
from controller.ofctl import OpenFlowError, read_group_stats
from controller.runtime import pid_alive, read_dict, read_drains

NAMES = tuple(b.name for b in BACKENDS)


def finite_float(value, fallback=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else fallback
    except (ValueError, TypeError):
        return fallback


def controller_events(runtime: Path, limit=30):
    path = runtime / 'controller.log'
    try:
        # Cap log input. Never read indefinitely large logs per request.
        with path.open('rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 32768))
            lines = f.read().decode('utf-8', errors='replace').splitlines()
    except OSError:
        return []
    return [{'source': 'controller.log', 'message': s[-600:]}
            for s in lines[-limit:] if s.strip()]


def benchmark_data(root: Path):
    """Read summaries produced by the existing benchmark suite."""
    if not root.is_dir():
        return {'available': False, 'runs': [], 'summary': [], 'directory': str(root)}
    trials = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    for folder in trials:
        summary = folder / 'summary.csv'
        runs = folder / 'runs.csv'
        if not (summary.exists() or runs.exists()):
            continue
        def parse(path):
            try:
                if path.stat().st_size > 2000000:
                    return []
                with path.open(newline='') as stream:
                    items = list(csv.DictReader(stream))[:300]
                result = []
                for item in items:
                    row = {}
                    for key, value in item.items():
                        if key in ('scenario', 'mode'):
                            row[key] = value
                        elif value is None or value == '':
                            row[key] = None
                        else:
                            try:
                                v = float(value)
                                row[key] = v if math.isfinite(v) else None
                            except ValueError:
                                row[key] = value[:150]
                    result.append(row)
                return result
            except (OSError, UnicodeError, csv.Error):
                return []
        return {'available': True, 'directory': str(folder),
                'summary': parse(summary), 'runs': parse(runs)}
    return {'available': False, 'directory': str(root), 'runs': [], 'summary': []}


class Monitor:
    def __init__(self, runtime: Path, benchmark_root: Path | None = None):
        self.runtime = runtime.resolve()
        self.benchmark_root = (benchmark_root or runtime / 'bench_results').resolve()
        self.history = deque(maxlen=100)
        self._lock = threading.Lock()
        self._last_counters = {}
        self._last_sample_at = 0.0
        self._group_cache = {'updated': 0.0, 'packet_count': None,
                             'bucket_counts': []}

    def _group_stats(self):
        now = time.monotonic()
        if now - self._group_cache['updated'] < 4:
            return dict(self._group_cache)
        group = dict(self._group_cache)
        try:
            group.update(read_group_stats())
        except OpenFlowError:
            pass  # Optional counters cannot make the dashboard unavailable.
        group['updated'] = now
        self._group_cache = group
        return dict(group)

    def snapshot(self):
        with self._lock:
            runtime = self.runtime
            now = time.time()
            status = read_dict(runtime / 'status.json')
            probes = read_dict(runtime / 'health.json')
            drains = read_drains(runtime)
            profile = read_dict(runtime / 'workload_profile.json')
            installed = status.get('installed_weights') or {}
            desired = status.get('desired_weights') or {}
            summary = status.get('backends') or {}
            observed = status.get('observed_share') or {}
            status_age = max(0.0, now - finite_float(status.get('timestamp'), 0))
            controller_online = status_age < 8 and pid_alive(status.get('controller_pid'))
            servers = []
            counts = {}
            for backend in BACKENDS:
                name = backend.name
                data = read_dict(runtime / f'{name}.json')
                health = probes.get(name) or {}
                state = summary.get(name) or {}
                baseline = profile.get(name) or {}
                count = max(0, int(finite_float(data.get('completed'))))
                counts[name] = count
                servers.append({
                    'name': name, 'ip': backend.ip,
                    'state': state.get('state', 'UNKNOWN') if controller_online else 'UNKNOWN',
                    'weight': int(installed.get(name, 0) or 0),
                    'desired_weight': int(desired.get(name, 0) or 0),
                    # Load per worker as measured by the controller (rho).
                    'load': (round(finite_float(state.get('load')), 3)
                             if state.get('load') is not None else None),
                    'response_ema_ms': round(finite_float(data.get('response_ms')), 2),
                    'in_system': int(finite_float(data.get('in_system'))),
                    'workers': int(finite_float(data.get('workers'))),
                    'completed': count,
                    'observed_share_pct': (round(100 * finite_float(observed.get(name)), 1)
                                           if name in observed else None),
                    'telemetry_age_s': round(max(0., now - finite_float(data.get('timestamp'))), 1),
                    'probe_ok': health.get('ok') is True and
                         0 <= now - finite_float(health.get('timestamp')) < 5,
                    'probe_ms': round(finite_float(health.get('latency_ms')), 2),
                    'probe_error': str(health.get('error', '') or '')[:160],
                    'draining': name in drains,
                    'workload': {'delay_ms': int(finite_float(baseline.get('delay_ms')))},
                })
            delta = {n: 0 for n in NAMES}
            period = now - self._last_sample_at
            if 0.4 <= period <= 30 and self._last_counters:
                for name in NAMES:
                    delta[name] = max(0, counts[name] - self._last_counters.get(name, counts[name]))
            self._last_counters = counts
            self._last_sample_at = now
            for server in servers:
                server['rps'] = round(delta[server['name']] / period, 2) if 0.4 <= period <= 30 else 0.0
            total_rate = round(sum(server['rps'] for server in servers), 2)
            total_delta = sum(delta.values())
            for server in servers:
                server['window_share_pct'] = round(100 * delta[server['name']] / total_delta, 1) if total_delta else 0
            controller = {
                'pid': status.get('controller_pid'),
                'alive': controller_online,
                'age_s': round(status_age, 1) if status else None,
                'mode': status.get('benchmark_mode', 'unknown'),
                'synced': bool(status) and installed == desired,
                'installed_weights': installed,
                'desired_weights': desired,
            }
            self.history.append({'timestamp': now, 'rps': total_rate,
                                 'load': {s['name']: s['load'] for s in servers},
                                 'weight': {s['name']: s['weight'] for s in servers}})
            active_job = read_dict(runtime / 'dashboard_traffic.json')
            benchmark_active = pid_alive(
                read_dict(runtime / 'benchmark_active.json').get('pid'))
            return {
                'timestamp': now, 'runtime': str(runtime), 'vip': f'{VIP}:{VPORT}',
                'controller': controller, 'backends': servers,
                'total_rps': total_rate, 'window_requests': total_delta,
                'history': list(self.history),
                'events': controller_events(runtime),
                'group': self._group_stats(),
                'traffic_job': active_job, 'benchmark_active': benchmark_active,
            }
