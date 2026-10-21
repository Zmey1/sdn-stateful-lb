"""Transport-independent control step of the load balancer.

Each step reads the lab runtime files (probe results, backend telemetry,
drains, benchmark mode), evaluates the policy, decides whether new SELECT
weights should be installed, and publishes status.json. The Ryu app
(controller/lb_app.py) supplies the switch's installed weights and group
counters and sends the resulting OpenFlow messages.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from .benchmark_modes import read_mode, select_weights
from .config import BACKENDS
from .policy import AdaptivePolicy, meaningful_change
from .runtime import atomic_json, read_dict, read_drains


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


class PolicyLoop:
    def __init__(self, runtime: Path, installed: dict[str, int],
                 minimum_update: float = 5.0):
        self.runtime = runtime
        self.minimum_update = minimum_update
        self.policy = initialize_policy(runtime, installed)
        self.last_update = time.monotonic() - minimum_update
        self.last_mode = None
        self.mode_error = ''
        self.last_states = None
        self.last_counters = None

    def _read_mode(self) -> str:
        try:
            mode = read_mode(self.runtime)
            self.mode_error = ''
            return mode
        except ValueError as exc:
            # Never switch to an unintended policy: keep the last one.
            if str(exc) != self.mode_error:
                print(f'Benchmark control error: {exc}; keeping previous mode', flush=True)
            self.mode_error = str(exc)
            return self.last_mode or 'adaptive'

    def step(self, installed: dict[str, int], counters: dict | None,
             connected: bool = True) -> dict[str, int] | None:
        """Run one control cycle; return weights to install now, or None.

        `installed` is what the switch reported; `counters` the latest
        SELECT group counters. The caller treats a returned weight set as
        installed unless the switch reports otherwise.
        """
        observed = connection_shares(self.last_counters, counters, installed)
        self.last_counters = counters
        self.policy.manual_drain = read_drains(self.runtime)
        adaptive = self.policy.evaluate(read_metrics(self.runtime),
                                        read_health(self.runtime))
        mode = self._read_mode()
        desired = select_weights(mode, adaptive, self.policy.summary())
        due = time.monotonic() - self.last_update >= self.minimum_update
        install = connected and should_install_weights(
            installed, desired, mode, mode != self.last_mode, due)
        if install:
            installed = dict(desired)
            self.last_update = time.monotonic()
            self.last_counters = None  # buckets are rebuilt by the group MOD
        # Remember a mode only after its weights are installed; otherwise the
        # next cycle retries the forced switch.
        if installed == desired:
            self.last_mode = mode

        summary = self.policy.summary()
        states = {n: s['state'] for n, s in summary.items()}
        if install or states != self.last_states:
            print(f'weights={installed}, desired={desired}, states={states}', flush=True)
            self.last_states = states
        # Snapshot actual installed weights, not a pending proposal.
        self.policy.last_weights = dict(installed)
        atomic_json(self.runtime / 'policy_state.json', self.policy.snapshot())
        atomic_json(self.runtime / 'status.json', {
            'timestamp': time.time(), 'controller_pid': os.getpid(),
            'switch_connected': connected,
            'installed_weights': installed, 'desired_weights': desired,
            'observed_share': observed,
            'group_packets': counters.get('packet_count') if counters else None,
            'benchmark_mode': mode, 'mode_error': self.mode_error,
            'backends': summary,
        })
        return desired if install else None
