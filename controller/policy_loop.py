"""Out-of-band control plane. Does not handle forwarded packets.

Health is measured by an independent Mininet p1 probe process. Controller
restarts read existing group weights and cached health/policy state rather than
reinstalling flows (which would disrupt established conntrack sessions).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import time
from .config import BACKENDS, BRIDGE
from .runtime import atomic_json, read_dict, read_drains
from .ofctl import read_group_stats, read_installed_weights, update_weights, OpenFlowError
from .policy import AdaptivePolicy, meaningful_change
from .benchmark_modes import read_mode, select_weights


def read_metrics(directory: Path) -> dict[str, dict]:
    return {b.name: entry for b in BACKENDS
            if (entry := read_dict(directory / f'{b.name}.json'))}


def read_health(directory: Path) -> dict[str, dict]:
    return read_dict(directory / 'health.json')


def connection_shares(before: dict | None, after: dict | None,
                      weights: dict[str, int]) -> dict[str, float] | None:
    """Share of new connections the switch sent to each backend between two
    group-stats readings, from SELECT bucket counters (only SYNs reach the
    group). Bucket i is the i-th backend with a nonzero installed weight.
    """
    live = [b.name for b in BACKENDS if weights.get(b.name, 0) > 0]
    if not before or not after or not live:
        return None
    old, new = before['bucket_counts'], after['bucket_counts']
    if not len(old) == len(new) == len(live):
        return None
    deltas = [n - o for o, n in zip(old, new)]
    total = sum(deltas)
    if total <= 0 or min(deltas) < 0:
        return None
    shares = {b.name: 0.0 for b in BACKENDS}
    shares.update({name: round(d / total, 3) for name, d in zip(live, deltas)})
    return shares


def initialize_policy(runtime: Path, installed: dict[str, int]) -> AdaptivePolicy:
    policy = AdaptivePolicy()
    file = runtime / 'policy_state.json'
    try:
        policy.restore(json.loads(file.read_text()))
        print('Restored saved policy state; using installed switch weights as authority',
              flush=True)
    except (OSError, ValueError, TypeError, OverflowError):
        policy.reconcile_existing(installed)
        print('Reconstructed health state from installed group (no valid snapshot)',
              flush=True)
    policy.last_weights = dict(installed)
    return policy



def should_install_weights(previous: dict[str, int], desired: dict[str, int],
                           mode: str, mode_changed: bool, due: bool) -> bool:
    """Honor explicit benchmark modes without weakening adaptive hysteresis.

    Static modes are exact, and switches into adaptive force its initial
    weights. Membership changes (a backend added or removed) are immediate.
    Other adaptive updates use minimum-update interval + hysteresis.
    """
    if previous == desired:
        return False
    changed_members = any((previous.get(k, 0) == 0) != (v == 0)
                          for k, v in desired.items())
    if mode != 'adaptive' or mode_changed or changed_members:
        return True
    return due and meaningful_change(previous, desired)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--runtime', required=True, type=Path)
    p.add_argument('--bridge', default=BRIDGE)
    p.add_argument('--interval', type=float, default=2.0)
    p.add_argument('--minimum-update', type=float, default=5.0)
    args = p.parse_args()
    if args.interval <= 0 or args.minimum_update <= 0:
        p.error('interval and minimum-update must be positive')
    args.runtime.mkdir(parents=True, exist_ok=True)

    # Prevent two independent control loops from fighting over SELECT weights.
    with (args.runtime / 'controller.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another controller is already managing this runtime')
        try:
            previous = read_installed_weights(args.bridge)
        except OpenFlowError as exc:
            raise SystemExit(f'Unable to reconcile live switch: {exc}. '
                             'Start the Mininet lab first.') from exc

        policy = initialize_policy(args.runtime, previous)
        (args.runtime / 'controller.pid').write_text(str(os.getpid()))
        print(f'Controller PID {os.getpid()}, reconciled installed weights={previous}',
              flush=True)
        last_update = time.monotonic() - args.minimum_update
        last_states = None
        last_mode = None
        mode_error = ''
        last_counters = None
        try:
            while True:
                try:
                    counters = read_group_stats(args.bridge)
                except OpenFlowError:
                    counters = None
                observed = connection_shares(last_counters, counters, previous)
                last_counters = counters
                policy.manual_drain = read_drains(args.runtime)
                health = read_health(args.runtime)
                adaptive_weights = policy.evaluate(read_metrics(args.runtime), health=health)
                try:
                    benchmark_mode = read_mode(args.runtime)
                    mode_error = ''
                except ValueError as exc:
                    # Never switch to an unintended policy: keep the last one.
                    if str(exc) != mode_error:
                        print(f'Benchmark control error: {exc}; keeping previous mode',
                              flush=True)
                    mode_error = str(exc)
                    benchmark_mode = last_mode or 'adaptive'
                desired = select_weights(benchmark_mode, adaptive_weights, policy.summary())
                due = time.monotonic() - last_update >= args.minimum_update
                mode_changed = benchmark_mode != last_mode
                updated = False
                if should_install_weights(previous, desired, benchmark_mode,
                                          mode_changed, due):
                    try:
                        update_weights(desired, args.bridge)
                        previous = dict(desired)
                        last_update = time.monotonic()
                        updated = True
                        last_counters = None  # buckets were rebuilt
                    except OpenFlowError as exc:
                        print(f'OpenFlow update failed: {exc}', flush=True)
                        # Resync to switch as source of truth if command
                        # succeeded but its readback failed (or manual edit).
                        try:
                            previous = read_installed_weights(args.bridge)
                        except OpenFlowError:
                            pass
                        last_counters = None
                # Remember a mode only after its desired weights are installed.
                # If an update fails, retry on the next controller iteration.
                if previous == desired:
                    last_mode = benchmark_mode
                summary = policy.summary()
                states = {n: s['state'] for n, s in summary.items()}
                if updated or states != last_states:
                    print(f'weights={previous}, desired={desired}, states={states}',
                          flush=True)
                    last_states = states
                # Snapshot actual installed weights, not a pending proposal.
                policy.last_weights = dict(previous)
                atomic_json(args.runtime / 'policy_state.json', policy.snapshot())
                atomic_json(args.runtime / 'status.json', {
                    'timestamp': time.time(), 'controller_pid': os.getpid(),
                    'installed_weights': previous, 'desired_weights': desired,
                    'observed_share': observed,
                    'benchmark_mode': benchmark_mode, 'mode_error': mode_error,
                    'backends': summary,
                })
                time.sleep(args.interval)
        finally:
            try:
                (args.runtime / 'controller.pid').unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == '__main__':
    main()
