"""Stateful adaptive policy with independent probes, graceful drain and recovery.

This is pure Python. No traffic is forwarded by this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import time
from typing import Mapping
from .config import BACKENDS


@dataclass
class BackendState:
    state: str = 'DOWN'         # DOWN, RECOVERING, HEALTHY
    successes: int = 0
    failures: int = 0
    recovering_since: float = 0.0
    cpu_ema: float | None = None
    latency_ema: float | None = None
    active_ema: float | None = None
    probe_ok: bool = False
    probe_error: str = ''


class AdaptivePolicy:
    def __init__(self, stale_after: float = 4.0, recovery_checks: int = 3,
                 down_checks: int = 3, probe_stale_after: float = 5.0,
                 recovery_ramp: float = 30.0):
        self.states = {b.name: BackendState() for b in BACKENDS}
        self.stale_after = stale_after
        self.recovery_checks = recovery_checks
        self.down_checks = down_checks
        self.probe_stale_after = probe_stale_after
        self.recovery_ramp = recovery_ramp
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
        return {'states': {name: asdict(state) for name, state in self.states.items()},
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
            for attr in ('cpu_ema', 'latency_ema', 'active_ema'):
                v = data.get(attr)
                setattr(state, attr, float(v) if isinstance(v, (float, int))
                        and math.isfinite(v) and v >= 0 else None)
        weights = snapshot.get('weights', {})
        if isinstance(weights, dict):
            self.last_weights = {n: int(weights.get(n, 0)) for n in self.states}

    @staticmethod
    def _numeric(value, default=0.0) -> float:
        try:
            number = float(value)
        except (ValueError, TypeError):
            return default
        return max(0., number) if math.isfinite(number) else default

    def evaluate(self, metrics: Mapping[str, dict], now: float | None = None,
                 health: Mapping[str, dict] | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        capacity = {}
        for name, state in self.states.items():
            m = metrics.get(name)
            metric_fresh = bool(m) and isinstance(m.get('timestamp'), (float, int)) \
                and 0 <= now - m['timestamp'] <= self.stale_after

            # In production-style lab mode, health comes ONLY from independent
            # HTTP probes, not from the backend's self-reported metrics file.
            if health is not None:
                h = health.get(name)
                is_fresh = bool(h) and isinstance(h.get('timestamp'), (float, int)) \
                    and 0 <= now - h['timestamp'] <= self.probe_stale_after
                alive = is_fresh and h.get('ok') is True
                state.probe_error = '' if alive else (
                    str(h.get('error', 'probe timed out/stale')) if is_fresh
                    else 'probe missing or stale')
            else:
                # Backward compatibility for pure-Python tests. The live lab
                # ALWAYS supplies health=... and never relies on this branch.
                alive = metric_fresh
                state.probe_error = '' if alive else 'telemetry stale'
            state.probe_ok = bool(alive)

            if not alive:
                state.failures += 1
                state.successes = 0
                if state.failures >= self.down_checks:
                    state.state = 'DOWN'
                    state.recovering_since = 0
            else:
                state.failures = 0
                state.successes += 1
                if state.state == 'DOWN' and state.successes >= self.recovery_checks:
                    state.state = 'RECOVERING'
                    state.recovering_since = now
                elif state.state == 'RECOVERING' and (
                    now - state.recovering_since >= self.recovery_ramp
                ):
                    state.state = 'HEALTHY'

                if metric_fresh:
                    for attr, key in (('cpu_ema', 'cpu_pct'),
                                      ('latency_ema', 'latency_ms'),
                                      ('active_ema', 'active')):
                        sample = self._numeric(m.get(key, 0))
                        old = getattr(state, attr)
                        setattr(state, attr, sample if old is None
                                else 0.3 * sample + 0.7 * old)

            if name in self.manual_drain or state.state == 'DOWN':
                capacity[name] = 0.0
                continue

            headroom = max(.05, 1 - min(100, state.cpu_ema or 0) / 100)
            latency_factor = 1 / (1 + (state.latency_ema or 0) / 100)
            active_factor = 1 / (1 + (state.active_ema or 0) / 20)
            if state.state == 'RECOVERING':
                fraction = 0 if not state.recovering_since else max(
                    0, (now - state.recovering_since) / max(1, self.recovery_ramp))
                recovery_factor = 0.1 + 0.9 * min(1, fraction)
            else:
                recovery_factor = 1.0
            capacity[name] = headroom * latency_factor * active_factor * recovery_factor

        total = sum(capacity.values())
        if total <= 0:
            weights = {name: 0 for name in self.states}
        else:
            weights = {name: max(1, round(300 * capacity[name] / total))
                       if capacity[name] > 0 else 0 for name in self.states}
        self.last_weights = weights
        return weights

    def summary(self) -> dict[str, dict]:
        return {name: {'state': ('DRAINING' if name in self.manual_drain
                                and state.state != 'DOWN' else state.state),
                       'probe_ok': state.probe_ok, 'error': state.probe_error,
                       'failures': state.failures, 'successes': state.successes,
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
