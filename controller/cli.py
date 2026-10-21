"""Operator commands for the Mininet SDN load-balancer lab."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from .ofctl import show, update_weights
from .config import BACKENDS, BRIDGE
from .benchmark_modes import MODE_NAMES

ROOT = Path(__file__).resolve().parent.parent


def atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def restart_backend(name: str, runtime: Path) -> int:
    """LAB ONLY: restart an HTTP worker in its original Mininet namespace."""
    pids = json.loads((runtime / 'host_pids.json').read_text())
    host_pid = int(pids[name])
    # Ensure the namespace anchor remains alive: if the Mininet lab has
    # ended, do not start an accidental backend on the host network.
    os.kill(host_pid, 0)
    metrics_file = runtime / f'{name}.json'
    try:
        old_pid = int(json.loads(metrics_file.read_text())['pid'])
        # The original worker can remain a zombie until the parent lab
        # process reaps it. A zombie does not own a listening TCP socket.
        stat = Path(f'/proc/{old_pid}/stat').read_text()
        if stat.rsplit(') ', 1)[1].startswith('Z '):
            raise ProcessLookupError('worker has exited (zombie)')
        os.kill(old_pid, 0)
    except (OSError, ValueError, KeyError, ProcessLookupError):
        pass
    else:
        raise RuntimeError(f'{name} server PID {old_pid} still exists; stop it first')

    env = os.environ.copy()
    env['PYTHONPATH'] = str(ROOT)
    log = (runtime / f'{name}-restarted.log').open('a')
    try:
        proc = subprocess.Popen(
            ['mnexec', '-a', str(host_pid), sys.executable, '-u',
             '-m', 'backend.server', '--name', name, '--runtime', str(runtime)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
    finally:
        log.close()
    registry = runtime / 'restarted_pids.json'
    try:
        recorded = json.loads(registry.read_text())
        if not isinstance(recorded, dict):
            recorded = {}
    except (OSError, ValueError):
        recorded = {}
    # mnexec forks/execs into the namespace without further forking here.
    recorded[name] = proc.pid
    atomic_write(registry, recorded)
    return proc.pid


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['weights', 'show', 'drain', 'undrain',
                                       'status', 'health', 'restart-backend', 'mode'])
    p.add_argument('weights', nargs='*', help='b1=10 b2=70 b3=20')
    p.add_argument('--bridge', default=BRIDGE)
    p.add_argument('--runtime', type=Path, help='Runtime path printed by Mininet')
    args = p.parse_args()
    if args.command == 'mode':
        if not args.runtime or len(args.weights) != 1 or args.weights[0] not in MODE_NAMES:
            p.error('--runtime is required; mode must be adaptive, equal or capacity')
        atomic_write(args.runtime / 'benchmark_mode.json', {'mode': args.weights[0]})
        print(f'Benchmark mode requested: {args.weights[0]} (wait for status confirmation)')
        return
    if args.command == 'show':
        print(show(args.bridge))
        return
    if args.command in ('status', 'health'):
        if not args.runtime:
            p.error('--runtime /tmp/sdn-lb-... is required')
        path = args.runtime / ('status.json' if args.command == 'status' else 'health.json')
        try:
            print(json.dumps(json.loads(path.read_text()), indent=2))
        except (OSError, ValueError) as exc:
            p.error(f'No readable {path}: {exc}')
        return
    if args.command in ('drain', 'undrain', 'restart-backend'):
        if not args.runtime or len(args.weights) != 1:
            p.error(f'Usage: {args.command} b1 --runtime /tmp/sdn-lb-...')
        name = args.weights[0]
        if name not in {b.name for b in BACKENDS}:
            p.error(f'unknown backend: {name}')
        if args.command == 'restart-backend':
            try:
                pid = restart_backend(name, args.runtime)
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                p.error(str(exc))
            print(f'Started {name} backend process PID={pid}; monitor health/status')
            return
        file = args.runtime / 'drain.json'
        try:
            desired = set(json.loads(file.read_text()))
        except (OSError, ValueError):
            desired = set()
        if args.command == 'drain':
            desired.add(name)
        else:
            desired.discard(name)
        atomic_write(file, sorted(desired))
        print(f'{args.command}: {name} (policy loop picks up next cycle)')
        return
    if not args.weights:
        p.error('Give weights like b1=10 b2=70 b3=20')
    try:
        weights = {key: int(value) for key, value in
                   (x.split('=', 1) for x in args.weights)}
    except ValueError:
        p.error('Each weight must have form b1=10')
    update_weights(weights, args.bridge)
    print('Updated new-connection selection weights:', weights)


if __name__ == '__main__':
    main()
