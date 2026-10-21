"""Operator commands for the Mininet SDN load-balancer lab."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from .ofctl import show
from .config import BRIDGE
from .benchmark_modes import MODE_NAMES
from .runtime import BACKEND_NAMES, atomic_json, pid_alive, read_dict, set_drain

ROOT = Path(__file__).resolve().parent.parent


def restart_backend(name: str, runtime: Path) -> int:
    """LAB ONLY: restart an HTTP worker in its original Mininet namespace."""
    pids = json.loads((runtime / 'host_pids.json').read_text())
    host_pid = int(pids[name])
    # Ensure the namespace anchor remains alive: if the Mininet lab has
    # ended, do not start an accidental backend on the host network.
    os.kill(host_pid, 0)
    # The original worker can remain a zombie until the parent lab process
    # reaps it; pid_alive() treats that as exited (no listening socket).
    old_pid = read_dict(runtime / f'{name}.json').get('pid')
    if pid_alive(old_pid):
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
    recorded = read_dict(registry)
    # mnexec forks/execs into the namespace without further forking here.
    recorded[name] = proc.pid
    atomic_json(registry, recorded)
    return proc.pid


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['show', 'drain', 'undrain', 'status',
                                       'health', 'restart-backend', 'mode'])
    p.add_argument('target', nargs='?', help='backend name, or mode for "mode"')
    p.add_argument('--bridge', default=BRIDGE)
    p.add_argument('--runtime', type=Path, help='Runtime path printed by Mininet')
    args = p.parse_args()
    if args.command == 'mode':
        if not args.runtime or args.target not in MODE_NAMES:
            p.error('--runtime is required; mode must be adaptive, equal or capacity')
        atomic_json(args.runtime / 'benchmark_mode.json', {'mode': args.target})
        print(f'Benchmark mode requested: {args.target} (wait for status confirmation)')
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
    # drain, undrain, restart-backend
    if not args.runtime or args.target not in BACKEND_NAMES:
        p.error(f'Usage: {args.command} b1 --runtime /tmp/sdn-lb-...')
    if args.command == 'restart-backend':
        try:
            pid = restart_backend(args.target, args.runtime)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            p.error(str(exc))
        print(f'Started {args.target} backend process PID={pid}; monitor health/status')
        return
    set_drain(args.runtime, args.target, args.command == 'drain')
    print(f'{args.command}: {args.target} (policy loop picks up next cycle)')


if __name__ == '__main__':
    main()
