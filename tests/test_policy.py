import pytest

from controller.config import BACKENDS
from controller.policy import TOTAL_WEIGHT, AdaptivePolicy, meaningful_change

NAMES = [b.name for b in BACKENDS]
WORKERS = 4
STEP = 2.0


def healthy(now, down=()):
    return {n: {'timestamp': now, 'ok': n not in down} for n in NAMES}


class FluidLab:
    """Closed-loop stand-in for the switch + backends.

    Each backend serves `capacity` req/s with `workers` workers; arrivals split
    by the installed weights; excess work queues. Publishes the same
    cumulative counters as backend/server.py.
    """

    def __init__(self, capacity, offered, workers=(WORKERS,) * 3):
        self.capacity = dict(zip(NAMES, capacity))
        self.workers = dict(zip(NAMES, workers))
        self.offered = offered
        self.queue = {n: 0.0 for n in NAMES}
        self.counters = {n: {'arrived': 0.0, 'busy_s': 0.0, 'completed': 0.0}
                         for n in NAMES}
        self.now = 100.0

    def advance(self, weights):
        total = sum(weights.values())
        for n in NAMES:
            arrivals = self.offered * weights[n] / total if total else 0
            served = min(self.capacity[n], arrivals + self.queue[n] / STEP)
            self.queue[n] = max(0.0, self.queue[n] + (arrivals - served) * STEP)
            c = self.counters[n]
            c['arrived'] += arrivals * STEP
            c['completed'] += served * STEP
            c['busy_s'] += served * STEP * self.workers[n] / self.capacity[n]
        self.now += STEP

    def metrics(self):
        return {n: {'timestamp': self.now, 'workers': self.workers[n], **self.counters[n]}
                for n in NAMES}


def run(capacity, offered, steps=40, start=None):
    lab = FluidLab(capacity, offered)
    policy = AdaptivePolicy(recovery_checks=1)
    policy.reconcile_existing(start or {n: 100 for n in NAMES})
    history = []
    weights = dict(policy.last_weights)
    for _ in range(steps):
        lab.advance(weights)
        weights = policy.evaluate(lab.metrics(), healthy(lab.now), now=lab.now)
        history.append(weights)
    return policy, lab, history


def test_converges_to_capacity_proportional_weights():
    # b1 slowed to a third of the others' capacity; 62 % offered load.
    policy, lab, history = run(capacity=(80 / 3, 80, 80), offered=150)
    final = history[-1]
    expected_b1 = TOTAL_WEIGHT * (1 / 7)
    assert final['b1'] == pytest.approx(expected_b1, rel=0.15)
    assert final['b2'] == pytest.approx(final['b3'], abs=3)
    # Overload on b1 was cleared and every backend runs at a similar load.
    assert lab.queue['b1'] == pytest.approx(0, abs=1)
    loads = [policy.states[n].load for n in NAMES]
    assert max(loads) / min(loads) < 1.25


def test_learns_heterogeneous_server_sizes():
    # The lab's servers: 2, 4, 6 workers at 50 ms = 40, 80, 120 req/s.
    lab = FluidLab((40, 80, 120), offered=150, workers=(2, 4, 6))
    policy = AdaptivePolicy(recovery_checks=1)
    policy.reconcile_existing({n: 100 for n in NAMES})
    weights = dict(policy.last_weights)
    for _ in range(40):
        lab.advance(weights)
        weights = policy.evaluate(lab.metrics(), healthy(lab.now), now=lab.now)
    for name, expected in zip(NAMES, (50, 100, 150)):
        assert weights[name] == pytest.approx(expected, rel=0.12)


def test_gray_failure_mid_run_and_recovery():
    # b2 drops to a third of its capacity while healthy, then recovers.
    lab = FluidLab((40, 80, 120), offered=150, workers=(2, 4, 6))
    policy = AdaptivePolicy(recovery_checks=1)
    policy.reconcile_existing({'b1': 50, 'b2': 100, 'b3': 150})
    weights = dict(policy.last_weights)
    for step in range(120):
        if step == 40:
            lab.capacity['b2'] = 80 / 3
        if step == 80:
            assert weights['b2'] == pytest.approx(300 * 80 / 3 / (40 + 80 / 3 + 120), rel=0.2)
            assert lab.queue['b2'] == pytest.approx(0, abs=1)
            lab.capacity['b2'] = 80
        lab.advance(weights)
        weights = policy.evaluate(lab.metrics(), healthy(lab.now), now=lab.now)
    assert weights['b2'] == pytest.approx(100, rel=0.12)


def test_settles_without_oscillation():
    _, _, history = run(capacity=(80 / 3, 80, 80), offered=150, steps=60)
    late = [w['b1'] for w in history[-20:]]
    assert max(late) - min(late) <= 6


def test_equal_servers_keep_equal_weights():
    _, _, history = run(capacity=(80, 80, 80), offered=150)
    assert max(history[-1].values()) - min(history[-1].values()) <= 2


def test_recovers_share_when_capacity_returns():
    lab = FluidLab((80 / 3, 80, 80), offered=150)
    policy = AdaptivePolicy(recovery_checks=1)
    policy.reconcile_existing({n: 100 for n in NAMES})
    weights = dict(policy.last_weights)
    for step in range(80):
        if step == 40:
            lab.capacity['b1'] = 80
        lab.advance(weights)
        weights = policy.evaluate(lab.metrics(), healthy(lab.now), now=lab.now)
    assert weights['b1'] == pytest.approx(100, abs=10)


def test_idle_cluster_holds_weights():
    _, _, history = run(capacity=(80 / 3, 80, 80), offered=1,
                        start={'b1': 60, 'b2': 120, 'b3': 120})
    assert history[-1] == {'b1': 60, 'b2': 120, 'b3': 120}


def test_every_healthy_backend_keeps_a_minimum_share():
    _, _, history = run(capacity=(1, 80, 80), offered=150, steps=80)
    assert history[-1]['b1'] >= TOTAL_WEIGHT * 0.03 - 1


def test_dead_backend_removed_and_survivors_share():
    policy = AdaptivePolicy(recovery_checks=1, down_checks=3)
    policy.reconcile_existing({n: 100 for n in NAMES})
    for t in (100, 102, 104):
        weights = policy.evaluate({}, healthy(t, down={'b1'}), now=t)
    assert weights['b1'] == 0
    assert weights['b2'] == weights['b3'] == TOTAL_WEIGHT // 2


def test_drain_and_undrain():
    policy = AdaptivePolicy(recovery_checks=1)
    policy.reconcile_existing({n: 100 for n in NAMES})
    policy.drain('b1')
    assert policy.evaluate({}, healthy(100), now=100)['b1'] == 0
    policy.drain('b1', False)
    # Rejoins at the average share, not at zero or the floor.
    assert policy.evaluate({}, healthy(102), now=102)['b1'] == 100


def test_counter_reset_after_backend_restart_is_ignored():
    policy = AdaptivePolicy(recovery_checks=1)
    policy.reconcile_existing({n: 100 for n in NAMES})
    sample = lambda t, count: {n: {'timestamp': t, 'arrived': count, 'busy_s': count / 20,
                                   'completed': count, 'workers': 4} for n in NAMES}
    policy.evaluate(sample(100, 500), healthy(100), now=100)
    policy.evaluate(sample(102, 3), healthy(102), now=102)
    assert all(policy.states[n].load is None for n in NAMES)


def test_meaningful_change():
    assert meaningful_change(None, {'b1': 100})
    assert not meaningful_change({'b1': 100}, {'b1': 101})
    assert meaningful_change({'b1': 100}, {'b1': 0})
    assert meaningful_change({'b1': 0}, {'b1': 10})


def test_backend_finishing_recovery_gets_average_share_on_idle_cluster():
    policy = AdaptivePolicy(recovery_checks=1, recovery_ramp=30)
    policy.reconcile_existing({'b1': 0, 'b2': 150, 'b3': 150})
    for t in range(100, 140, 2):
        weights = policy.evaluate({}, healthy(t), now=t)
    assert policy.states['b1'].state == 'HEALTHY'
    assert weights == {'b1': 100, 'b2': 100, 'b3': 100}
