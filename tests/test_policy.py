from controller.policy import AdaptivePolicy, meaningful_change


def metrics(now, b1=10, b2=10, b3=10):
    return {name: {'timestamp': now, 'cpu_pct': cpu, 'latency_ms': 10,
                   'active': 2} for name, cpu in zip(('b1', 'b2', 'b3'),
                                                     (b1, b2, b3))}


def test_load_response():
    policy = AdaptivePolicy(recovery_checks=2)
    policy.evaluate(metrics(100), now=100)
    weights = policy.evaluate(metrics(102, b1=95, b2=10, b3=10), now=102)
    assert weights['b1'] < weights['b2']
    assert weights['b2'] == weights['b3']


def test_dead_backend_removed():
    policy = AdaptivePolicy(recovery_checks=2)
    policy.evaluate(metrics(100), now=100)
    policy.evaluate(metrics(102), now=102)
    for t in (108, 110, 112):
        weights = policy.evaluate({n: m for n, m in metrics(t).items() if n != 'b1'}, now=t)
    assert weights['b1'] == 0
    assert weights['b2'] > 0


def test_recovery_gradual():
    policy = AdaptivePolicy(recovery_checks=3)
    for t in (100, 101, 102, 103):
        w = policy.evaluate(metrics(t), now=t)
    assert all(v > 0 for v in w.values())
    policy.drain('b1')
    assert policy.evaluate(metrics(104), now=104)['b1'] == 0
    policy.drain('b1', False)
    assert policy.evaluate(metrics(105), now=105)['b1'] > 0


def test_meaningful_change():
    assert meaningful_change(None, {'b1': 100})
    assert not meaningful_change({'b1': 100}, {'b1': 101})
    assert meaningful_change({'b1': 100}, {'b1': 0})
    assert meaningful_change({'b1': 0}, {'b1': 10})
