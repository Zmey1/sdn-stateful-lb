"""Local-only monitoring API; opt-in bounded demo controls.

Run from project root:
    sudo .venv-dashboard/bin/python -m monitoring.server --runtime /tmp/sdn-lb-...
Add --allow-controls for dashboard control buttons. No shell command or path
is accepted from HTTP clients, and the API never changes OpenFlow rules.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from controller.benchmark_modes import MODE_NAMES
from controller.cli import restart_backend
from controller.runtime import (BACKEND_NAMES, atomic_json, pid_alive, read_dict,
                                set_drain)
from monitoring.core import Monitor, benchmark_data

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / 'static'


class ModeRequest(BaseModel):
    mode: str


class BackendRequest(BaseModel):
    backend: str


class DrainRequest(BaseModel):
    backend: str
    enabled: bool


class WorkloadRequest(BaseModel):
    backend: str
    delay_ms: int = Field(ge=0, le=350)


class TrafficRequest(BaseModel):
    rate: int = Field(ge=1, le=180)
    duration: int = Field(ge=3, le=30)


def create_app(runtime: Path, *, allow_controls=False,
               benchmark_root: Path | None = None, port=8765):
    runtime = runtime.resolve()
    if not runtime.is_dir() or not (runtime / 'host_pids.json').exists():
        raise ValueError(f'Not a live lab runtime directory: {runtime}')
    monitor = Monitor(runtime, benchmark_root)
    app = FastAPI(title='SDN Stateful Load Balancer Dashboard', docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    control_lock = threading.Lock()
    traffic_lock = threading.Lock()

    def verify_control(request: Request):
        if not allow_controls:
            raise HTTPException(403, 'Dashboard controls are disabled. Restart with --allow-controls.')
        # With API bound to 127.0.0.1 and no CORS, reject browser requests
        # originating from other sites (including localhost alternate ports).
        origin = request.headers.get('origin')
        if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            raise HTTPException(403, 'Cross-site dashboard control rejected')
        status = read_dict(runtime / 'status.json')
        if not (pid_alive(status.get('controller_pid')) and
                0 <= time.time() - status.get('timestamp', 0) < 8):
            raise HTTPException(409, 'No healthy controller for this runtime')
        if pid_alive(read_dict(runtime / 'benchmark_active.json').get('pid')):
            raise HTTPException(409, 'A benchmark is running: controls are temporarily locked')

    @app.get('/')
    def homepage():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/snapshot')
    def snapshot():
        data = monitor.snapshot()
        data['controls_enabled'] = allow_controls
        return data

    @app.get('/api/benchmarks')
    def benchmarks():
        return benchmark_data(monitor.benchmark_root)

    @app.post('/api/control/mode')
    def mode(body: ModeRequest, request: Request):
        verify_control(request)
        if body.mode not in MODE_NAMES:
            raise HTTPException(400, 'Invalid mode')
        with control_lock:
            atomic_json(runtime / 'benchmark_mode.json', {'mode': body.mode})
        return {'ok': True, 'requested_mode': body.mode, 'note': 'Wait for controller status confirmation'}

    @app.post('/api/control/drain')
    def drain(body: DrainRequest, request: Request):
        verify_control(request)
        if body.backend not in BACKEND_NAMES:
            raise HTTPException(400, 'Invalid backend')
        names = set_drain(runtime, body.backend, body.enabled)
        return {'ok': True, 'drains': sorted(names)}

    @app.post('/api/control/workload')
    def workload(body: WorkloadRequest, request: Request):
        verify_control(request)
        if body.backend not in BACKEND_NAMES:
            raise HTTPException(400, 'Invalid backend')
        with control_lock:
            current = read_dict(runtime / 'workload_profile.json')
            # Keep all other backend profiles unchanged.
            current[body.backend] = {'delay_ms': body.delay_ms}
            atomic_json(runtime / 'workload_profile.json', current)
        return {'ok': True, 'backend': body.backend, 'profile': current[body.backend]}

    @app.post('/api/control/crash')
    def crash(body: BackendRequest, request: Request):
        """Lab only: terminate a verified backend worker, never arbitrary PIDs."""
        verify_control(request)
        name = body.backend
        if name not in BACKEND_NAMES:
            raise HTTPException(400, 'Invalid backend')
        metrics = read_dict(runtime / f'{name}.json')
        try:
            pid = int(metrics.get('pid', -1))
            command = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            if (not pid_alive(pid) or 'backend.server' not in command or
                    f'--name {name}' not in command or str(runtime) not in command):
                raise HTTPException(409, 'No matching live backend worker to terminate')
            os.kill(pid, signal.SIGTERM)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, f'Cannot terminate backend: {exc}') from exc
        return {'ok': True, 'message': f'Terminated {name} worker PID {pid}; health checks will detect the failure'}

    @app.post('/api/control/restart')
    def restart(body: BackendRequest, request: Request):
        verify_control(request)
        if body.backend not in BACKEND_NAMES:
            raise HTTPException(400, 'Invalid backend')
        try:
            pid = restart_backend(body.backend, runtime)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            raise HTTPException(409, f'Cannot restart backend: {exc}') from exc
        return {'ok': True, 'message': f'Restarted {body.backend} worker PID {pid}; watch recovery state'}

    @app.post('/api/control/reset')
    def reset(request: Request):
        verify_control(request)
        with control_lock:
            atomic_json(runtime / 'workload_profile.json',
                        {name: {'delay_ms': 0} for name in sorted(BACKEND_NAMES)})
            atomic_json(runtime / 'drain.json', [])
            atomic_json(runtime / 'benchmark_mode.json', {'mode': 'adaptive'})
        return {'ok': True, 'note': 'Requested adaptive mode; reset delays and drains'}

    def launch_traffic(rate, duration):
        try:
            pids = read_dict(runtime / 'host_pids.json')
            pid = int(pids['h1'])
            if not pid_alive(pid):
                raise RuntimeError('Mininet h1 namespace not running')
            command = ['nsenter', '-t', str(pid), '-n', '--', sys.executable,
                       '-m', 'experiments.load_client', '--rate', str(rate),
                       '--duration', str(duration), '--warmup', '0',
                       '--timeout', '3', '--max-inflight', '60']
            result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                    text=True, timeout=duration + 30)
            if result.returncode:
                raise RuntimeError(result.stderr[-500:] or 'traffic generator failed')
            report = json.loads(result.stdout)
            atomic_json(runtime / 'dashboard_traffic.json', {
                'state': 'completed', 'timestamp': time.time(),
                'rate': rate, 'duration': duration, 'offered': report.get('offered'),
                'success': report.get('success'), 'backends': report.get('backends'),
                'errors': report.get('errors'),
            })
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
            atomic_json(runtime / 'dashboard_traffic.json',
                        {'state': 'failed', 'timestamp': time.time(), 'error': str(exc)[:250]})
        finally:
            traffic_lock.release()

    @app.post('/api/control/traffic')
    def traffic(body: TrafficRequest, request: Request):
        verify_control(request)
        if not traffic_lock.acquire(blocking=False):
            raise HTTPException(409, 'A dashboard traffic test is already running')
        atomic_json(runtime / 'dashboard_traffic.json', {
            'state': 'running', 'timestamp': time.time(),
            'rate': body.rate, 'duration': body.duration,
        })
        threading.Thread(target=launch_traffic, args=(body.rate, body.duration),
                         daemon=True).start()
        return {'ok': True, 'message': 'Generating requests from h1 inside Mininet'}

    return app


def main():
    p = argparse.ArgumentParser(description='Local SDN lab dashboard')
    p.add_argument('--runtime', required=True, type=Path,
                   help='Current /tmp/sdn-lb-... directory')
    p.add_argument('--bench-root', type=Path,
                   help='Optional existing benchmark results directory')
    p.add_argument('--allow-controls', action='store_true',
                   help='Enable bounded lab experiment actions')
    p.add_argument('--port', type=int, default=8765)
    args = p.parse_args()
    if not 1024 <= args.port <= 65535:
        p.error('Port must be between 1024 and 65535')
    import uvicorn
    app = create_app(args.runtime, allow_controls=args.allow_controls,
                     benchmark_root=args.bench_root, port=args.port)
    print(f'Dashboard: http://127.0.0.1:{args.port} | runtime={args.runtime}',
          flush=True)
    # Never bind to 0.0.0.0 for a root-run lab controller UI.
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
