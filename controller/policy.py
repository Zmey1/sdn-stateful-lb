"""Dynamic load-balancing policy: health state machine + load-equalizing weights.

Pure Python; no traffic is forwarded by this module.

Control law (one evaluation every controller cycle):

    rho_i   = offered load per worker of backend i
            = arrival rate * mean service time / workers
    rho_bar = traffic-weighted mean of rho over healthy backends
    share_i <- installed_share_i * clamp((rho_bar / rho_i) ** GAIN, 1/MAX_STEP, MAX_STEP)

Backends that are busier than average lose share and quieter ones gain it,
until every backend runs at the same load per worker. At that fixed point
each backend's traffic is proportional to its capacity, whatever that
capacity is, without configuring it. Updates start from the weights
actually installed in the switch, so a delayed or skipped install cannot
make the controller wind up.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Mapping
from .config import BACKENDS

TOTAL_WEIGHT = 300


@dataclass
class BackendState:
    state: str = 'DOWN'         # DOWN, RECOVERING, HEALTHY
    successes: int = 0
    failures: int = 0
    recovering_since: float = 0.0
    probe_ok: bool = False
    probe_error: str = ''
    load: float | None = None   # smoothed offered load per worker (rho)
    service_s: float | None = None  # mean service time per request
    sample: tuple[float, ...] | None = None  # (timestamp, arrived, busy_s, completed)

    PERSISTED = ('state', 'successes', 'failures', 'recovering_since')


class AdaptivePolicy:
    def __init__(self, stale_after: float = 4.0, recovery_checks: int = 3,
                 down_checks: int = 3, probe_stale_after: float = 5.0,
                 recovery_ramp: float = 30.0, gain: float = 0.5,
                 max_step: float = 1.5, deadband: float = 0.1,
                 idle_load: float = 0.05, min_share: float = 0.03,
                 load_alpha: float = 0.5):
        self.states = {b.name: BackendState() for b in BACKENDS}
        self.stale_after = stale_after
        self.recovery_checks = recovery_checks
        self.down_checks = down_checks
        self.probe_stale_after = probe_stale_after
        self.recovery_ramp = recovery_ramp
        self.gain = gain              # fraction of the log-error corrected per step
        self.max_step = max_step      # largest multiplicative change per step
        self.deadband = deadband      # ignore load differences below 10 %
        self.idle_load = idle_load    # below this mean load there is no signal
        self.min_share = min_share    # keep sampling every healthy backend
        self.load_alpha = load_alpha  # EMA smoothing of the measured load
        self.manual_drain: set[str] = set()
        self.last_weights = {b.name: 0 for b in BACKENDS}

    def drain(self, name: str, enabled=True):
        if name not in self.states:
            raise ValueError(name)
        if enabled:
            self.manual_drain.add(name)
        else:
            self.manual_drain.discard(name)

    def reconcile_existing(self, weights: dict[str, int]) -> None:
        """Recover policy state when no state snapshot is available.

        Nonzero installed weights mean the backend was previously admitted.
        Do not unnecessarily drain it on a controller-only restart. Actual
        current health is checked immediately on subsequent evaluations.
        """
        for name, state in self.states.items():
            if weights.get(name, 0) > 0:
                state.state = 'HEALTHY'
                state.successes = self.recovery_checks
        self.last_weights = dict(weights)

    def snapshot(self) -> dict:
        return {'states': {name: {k: getattr(state, k) for k in BackendState.PERSISTED}
                           for name, state in self.states.items()},
                'weights': dict(self.last_weights)}

    def restore(self, snapshot: dict) -> None:
        """Load trusted lab-local state, ignoring unknown/invalid entries."""
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('states'), dict):
            raise ValueError('Malformed controller state snapshot')
        for name, data in snapshot['states'].items():
            if name not in self.states or not isinstance(data, dict):
                continue
            if data.get('state') not in ('HEALTHY', 'RECOVERING', 'DOWN'):
                continue
            state = self.states[name]
            state.state = data['state']
            state.successes = max(0, min(1000, int(data.get('successes', 0))))
            state.failures = max(0, min(1000, int(data.get('failures', 0))))
            state.recovering_since = float(data.get('recovering_since', 0))
        weights = snapshot.get('weights', {})
        if isinstance(weights, dict):
            self.last_weights = {n: int(weights.get(n, 0)) for n in self.states}

    def _update_health(self, state: BackendState, probe: dict | None, now: float) -> None:
        """Health comes ONLY from independent probes, never self-reported metrics."""
        fresh = bool(probe) and isinstance(probe.get('timestamp'), (float, int)) \
            and 0 <= now - probe['timestamp'] <= self.probe_stale_after
        alive = fresh and probe.get('ok') is True
        state.probe_ok = alive
        state.probe_error = '' if alive else (
            str(probe.get('error', 'probe failed')) if fresh else 'probe missing or stale')
        if not alive:
            state.failures += 1
            state.successes = 0
            if state.failures >= self.down_checks:
                state.state = 'DOWN'
                state.recovering_since = 0
                state.load = state.service_s = state.sample = None
            return
        state.failures = 0
        state.successes += 1
        if state.state == 'DOWN' and state.successes >= self.recovery_checks:
            state.state = 'RECOVERING'
            state.recovering_since = now
        elif state.state == 'RECOVERING' and (
            now - state.recovering_since >= self.recovery_ramp
        ):
            state.state = 'HEALTHY'

    def _update_load(self, state: BackendState, metric: dict | None, now: float) -> None:
        """Difference cumulative counters into offered load per worker."""
        try:
            sample = (float(metric['timestamp']), float(metric['arrived']),
                      float(metric['busy_s']), float(metric['completed']))
            workers = int(metric['workers'])
        except (KeyError, TypeError, ValueError):
            state.load = state.sample = None
            return
        if not (0 <= now - sample[0] <= self.stale_after and workers > 0
                and all(math.isfinite(x) for x in sample)):
            state.load = state.sample = None
            return
        previous, state.sample = state.sample, sample
        if previous is None or sample[0] <= previous[0]:
            return
        elapsed, arrived, busy, completed = (a - b for a, b in zip(sample, previous))
        # A restarted backend resets its counters; wait for a second sample.
        if min(arrived, busy, completed) < 0:
            return
        if completed > 0:
            state.service_s = busy / completed
        if state.service_s is None:
            return
        rho = arrived / elapsed * state.service_s / workers
        state.load = rho if state.load is None else (
            self.load_alpha * rho + (1 - self.load_alpha) * state.load)

    def _ramp(self, state: BackendState, now: float) -> float:
        """Recovering backends get 10 % -> 100 % of an average share."""
        if state.state != 'RECOVERING':
            return 1.0
        elapsed = max(0.0, now - state.recovering_since) if state.recovering_since else 0.0
        return 0.1 + 0.9 * min(1.0, elapsed / max(1.0, self.recovery_ramp))

    def _healthy_shares(self, healthy: list[str], joining: set[str]) -> dict[str, float]:
        """Next share of each HEALTHY backend, starting from installed weights."""
        if not healthy:
            return {}
        base = {n: 0 if n in joining else max(0, self.last_weights.get(n, 0))
                for n in healthy}
        known = [w for w in base.values() if w > 0]
        # A backend that just (re)joined starts at the average share, which
        # is also where the recovery ramp ends.
        default = sum(known) / len(known) if known else 1.0
        shares = {n: (w if w > 0 else default) for n, w in base.items()}
        total = sum(shares.values())
        shares = {n: s / total for n, s in shares.items()}

        measured = {n: self.states[n].load for n in healthy
                    if self.states[n].load is not None}
        weight_sum = sum(shares[n] for n in measured)
        if weight_sum > 0:
            mean = sum(shares[n] * rho for n, rho in measured.items()) / weight_sum
            if mean >= self.idle_load:
                for name, rho in measured.items():
                    rho = max(rho, 1e-3)
                    if abs(rho / mean - 1) < self.deadband:
                        continue
                    step = (mean / rho) ** self.gain
                    shares[name] *= min(self.max_step, max(1 / self.max_step, step))

        total = sum(shares.values())
        shares = {n: max(self.min_share, s / total) for n, s in shares.items()}
        total = sum(shares.values())
        return {n: s / total for n, s in shares.items()}

    def evaluate(self, metrics: Mapping[str, dict], health: Mapping[str, dict],
                 now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        joining = set()
        for name, state in self.states.items():
            was_healthy = state.state == 'HEALTHY'
            self._update_health(state, health.get(name), now)
            if state.state == 'HEALTHY' and not was_healthy:
                joining.add(name)
            if state.state != 'DOWN':
                self._update_load(state, metrics.get(name), now)

        eligible = [n for n, s in self.states.items()
                    if s.state != 'DOWN' and n not in self.manual_drain]
        healthy = [n for n in eligible if self.states[n].state == 'HEALTHY']
        shares = self._healthy_shares(healthy, joining)
        average = 1 / len(healthy) if healthy else 1.0
        for name in eligible:
            if name not in shares:
                shares[name] = average * self._ramp(self.states[name], now)

        total = sum(shares.values())
        weights = {name: max(1, round(TOTAL_WEIGHT * shares[name] / total))
                   if name in shares else 0 for name in self.states}
        self.last_weights = weights
        return weights

    def summary(self) -> dict[str, dict]:
        return {name: {'state': ('DRAINING' if name in self.manual_drain
                                and state.state != 'DOWN' else state.state),
                       'probe_ok': state.probe_ok, 'error': state.probe_error,
                       'failures': state.failures, 'successes': state.successes,
                       'load': None if state.load is None else round(state.load, 3),
                       'weight': self.last_weights[name]}
                for name, state in self.states.items()}


def meaningful_change(previous: dict[str, int] | None,
                      proposed: dict[str, int], threshold: float = .10) -> bool:
    if previous is None:
        return True
    if any((previous.get(k, 0) == 0) != (v == 0) for k, v in proposed.items()):
        return True
    return any(abs(previous.get(k, 0) - v) >= max(5, threshold * max(1, previous.get(k, 0)))
               for k, v in proposed.items())
