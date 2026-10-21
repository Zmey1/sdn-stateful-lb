import json
from unittest.mock import patch

import pytest

from controller.config import ALL, BACKENDS
from controller.health_probe import probe_backend
from controller.ofctl import OpenFlowError, parse_installed_weights
from controller.policy import AdaptivePolicy
from controller.policy_loop import connection_shares, initialize_policy, read_health


def checks(now, b1=True, b2=True, b3=True):
    return {name: {'timestamp': now, 'ok': ok}
            for name, ok in zip(('b1', 'b2', 'b3'), (b1, b2, b3))}


def metrics(now):
    return {b.name: {'timestamp': now, 'cpu_pct': 20, 'latency_ms': 5,
                     'active': 2} for b in BACKENDS}


def test_independent_probe_overrules_fresh_metrics():
    policy = AdaptivePolicy(recovery_checks=2, down_checks=3)
    for t in (100, 102, 104, 106):
        policy.evaluate(metrics(t), now=t, health=checks(t))
    assert policy.states['b1'].state != 'DOWN'
    for t in (108, 110, 112):
        weights = policy.evaluate(metrics(t), now=t, health=checks(t, b1=False))
    assert policy.states['b1'].state == 'DOWN'
    assert weights['b1'] == 0
    assert weights['b2'] > 0


def test_probe_staleness_excludes_backend_even_if_telemetry_updates():
    policy = AdaptivePolicy(recovery_checks=1, down_checks=2)
    policy.evaluate(metrics(100), now=100, health=checks(100))
    policy.evaluate(metrics(108), now=108, health=checks(100))
    result = policy.evaluate(metrics(110), now=110, health=checks(100))
    assert result == {'b1': 0, 'b2': 0, 'b3': 0}


def test_gradual_recovery_and_snapshot_restoration():
    policy = AdaptivePolicy(recovery_checks=2, recovery_ramp=30)
    policy.reconcile_existing({'b1': 0, 'b2': 150, 'b3': 150})
    w0 = policy.evaluate(metrics(100), now=100, health=checks(100))
    assert w0['b1'] == 0
    w1 = policy.evaluate(metrics(102), now=102, health=checks(102))
    assert policy.states['b1'].state == 'RECOVERING'
    assert 0 < w1['b1'] < w1['b2']
    w2 = policy.evaluate(metrics(112), now=112, health=checks(112))
    assert w2['b1'] > w1['b1']
    cloned = AdaptivePolicy(recovery_checks=2, recovery_ramp=30)
    cloned.restore(policy.snapshot())
    assert cloned.states['b1'].state == 'RECOVERING'
    assert cloned.states['b1'].recovering_since == 102
    w3 = cloned.evaluate(metrics(135), now=135, health=checks(135))
    assert cloned.states['b1'].state == 'HEALTHY'
    assert w3['b1'] > w2['b1']


def test_draining_stops_new_selection_but_not_health_check():
    policy = AdaptivePolicy(recovery_checks=1)
    policy.evaluate(metrics(100), now=100, health=checks(100))
    policy.drain('b1')
    w = policy.evaluate(metrics(102), now=102, health=checks(102))
    assert w['b1'] == 0
    assert policy.summary()['b1']['state'] == 'DRAINING'
    policy.drain('b1', False)
    assert policy.evaluate(metrics(104), now=104, health=checks(104))['b1'] > 0


def test_parse_group_weights_and_drop():
    text = ('OFPST_GROUP_DESC reply (OF1.3) (xid=0x2):\n'
            ' group_id=100,type=select,'
            'bucket=weight:100,actions=ct(commit,table=30,zone=10,nat(dst=10.0.2.11:8080),exec(set_field:0x1->ct_mark)),'
            'bucket=weight:150,actions=ct(commit,table=30,zone=10,nat(dst=10.0.2.13:8080),exec(set_field:0x1->ct_mark))\n')
    assert parse_installed_weights(text) == {'b1': 100, 'b2': 0, 'b3': 150}
    assert parse_installed_weights('group_id=100,type=select,bucket=actions=drop') == {
        'b1': 0, 'b2': 0, 'b3': 0}
    with pytest.raises(OpenFlowError):
        parse_installed_weights('group_id=100,type=select,bucket=weight:100,actions=output:1')
    with pytest.raises(OpenFlowError):
        parse_installed_weights('group_id=99,type=select,bucket=actions=drop')


def test_controller_restart_reads_installed_group_without_reinstalling(tmp_path):
    # No state snapshot: initialize from readback, no flush/re-install calls.
    current = {'b1': 30, 'b2': 160, 'b3': 110}
    recovered = initialize_policy(tmp_path, current)
    assert recovered.last_weights == current
    assert recovered.states['b1'].state == 'HEALTHY'

    recovered.states['b1'].state = 'RECOVERING'
    recovered.states['b1'].recovering_since = 222
    (tmp_path / 'policy_state.json').write_text(json.dumps(recovered.snapshot()))
    recovered_again = initialize_policy(tmp_path, current)
    assert recovered_again.states['b1'].state == 'RECOVERING'
    assert recovered_again.states['b1'].recovering_since == 222


def test_probe_checks_identity_not_just_http_status():
    from controller.config import BACKENDS
    class FakeResponse:
        status = 200
        def read(self, _): return b'{"ok": true, "server": "b3"}'

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.args = args
        def request(self, *args, **kwargs): pass
        def getresponse(self): return FakeResponse()
        def close(self): pass
    with patch('controller.health_probe.HTTPConnection', FakeConnection):
        result = probe_backend(BACKENDS[0])
    assert result['ok'] is False  # /health from b3 isn't proof b1 is healthy


def test_health_file_missing_is_unhealthy(tmp_path):
    assert read_health(tmp_path) == {}
    (tmp_path / 'health.json').write_text('{bad')
    assert read_health(tmp_path) == {}


def test_connection_shares_from_select_bucket_counters():
    weights = {'b1': 0, 'b2': 150, 'b3': 150}
    before = {'packet_count': 10, 'bucket_counts': [4, 6]}
    after = {'packet_count': 50, 'bucket_counts': [14, 36]}
    assert connection_shares(before, after, weights) == {'b1': 0.0, 'b2': 0.25, 'b3': 0.75}
    assert connection_shares(None, after, weights) is None
    assert connection_shares(before, before, weights) is None  # no traffic
    assert connection_shares(before, {'bucket_counts': [1, 2, 3]}, weights) is None
