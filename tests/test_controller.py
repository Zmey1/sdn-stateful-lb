"""Controller tests: the policy step and the Ryu app's switch handlers,
driven by a fake datapath that records the OpenFlow messages sent."""
import json
import time
from types import SimpleNamespace

import pytest
from ryu.ofproto import ofproto_v1_3 as ofp
from ryu.ofproto import ofproto_v1_3_parser as parser

from controller.config import ALL, BACKENDS
from controller.lb_app import RUNTIME_ENV, LoadBalancer
from controller.policy_loop import PolicyLoop, should_install_weights

NAMES = [b.name for b in BACKENDS]


def write_health(runtime, ok=True):
    now = time.time()
    (runtime / 'health.json').write_text(json.dumps(
        {n: {'timestamp': now, 'ok': ok} for n in NAMES}))


def test_step_admits_backends_once_probes_pass(tmp_path):
    loop = PolicyLoop(tmp_path, {n: 0 for n in NAMES})
    loop.policy.recovery_checks = 1
    write_health(tmp_path)
    desired = loop.step({n: 0 for n in NAMES}, None)
    assert desired and all(desired[n] > 0 for n in NAMES)  # membership change: immediate
    status = json.loads((tmp_path / 'status.json').read_text())
    assert status['installed_weights'] == desired
    assert status['switch_connected'] is True


def test_step_publishes_status_with_bad_mode_file(tmp_path):
    loop = PolicyLoop(tmp_path, {n: 100 for n in NAMES})
    write_health(tmp_path)
    (tmp_path / 'benchmark_mode.json').write_text('{"mode": "typo"}')
    loop.step({n: 100 for n in NAMES}, None)
    status = json.loads((tmp_path / 'status.json').read_text())
    assert status['benchmark_mode'] == 'adaptive'
    assert 'unknown mode' in status['mode_error']


def test_step_never_installs_while_switch_disconnected(tmp_path):
    loop = PolicyLoop(tmp_path, {n: 0 for n in NAMES})
    loop.policy.recovery_checks = 1
    write_health(tmp_path)
    assert loop.step({n: 0 for n in NAMES}, None, connected=False) is None


class FakeDatapath:
    ofproto = ofp
    ofproto_parser = parser
    id = 1

    def __init__(self):
        self.sent = []

    def send_msg(self, message):
        self.sent.append(message)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv(RUNTIME_ENV, str(tmp_path))
    monkeypatch.setattr('controller.lb_app.hub.spawn', lambda *a, **k: None)
    instance = LoadBalancer()
    instance.ports = {x.name: i + 1 for i, x in enumerate(ALL)}
    return instance


def event(datapath, body):
    return SimpleNamespace(msg=SimpleNamespace(datapath=datapath, body=body))


def test_second_controller_is_refused(app, tmp_path):
    with pytest.raises(SystemExit):
        LoadBalancer()


def test_fresh_switch_gets_pipeline_and_fail_closed_group(app):
    datapath = FakeDatapath()
    app._group_desc(event(datapath, []))
    flows = [m for m in datapath.sent if isinstance(m, parser.OFPFlowMod)]
    groups = [m for m in datapath.sent if isinstance(m, parser.OFPGroupMod)]
    assert len(flows) > 20
    assert len(groups) == 1 and groups[0].command == ofp.OFPGC_ADD
    assert groups[0].buckets[0].actions == []  # drop bucket
    assert app.installed == {n: 0 for n in NAMES}
    assert app.loop is not None


def test_restart_adopts_existing_group_without_touching_flows(app):
    bucket = parser.OFPBucket(weight=7, watch_port=ofp.OFPP_ANY, watch_group=ofp.OFPG_ANY,
                              actions=[])
    existing = parser.OFPGroupDescStats(type_=ofp.OFPGT_SELECT, group_id=100,
                                        buckets=[bucket])
    datapath = FakeDatapath()
    app._group_desc(event(datapath, [existing]))
    assert datapath.sent == []  # no flow or group modifications
    assert app.installed == {n: 0 for n in NAMES}
    assert app.loop is not None


def test_unexpected_group_is_not_managed(app):
    foreign = parser.OFPGroupDescStats(type_=ofp.OFPGT_INDIRECT, group_id=100, buckets=[])
    datapath = FakeDatapath()
    app._group_desc(event(datapath, [foreign]))
    assert datapath.sent == [] and app.loop is None


def test_mode_switch_bypasses_hysteresis():
    current = {'b1': 101, 'b2': 99, 'b3': 100}
    equal = {'b1': 100, 'b2': 100, 'b3': 100}
    assert should_install_weights(current, equal, 'equal', True, False)
    assert should_install_weights(current, equal, 'equal', False, False)


def test_adaptive_small_drift_keeps_hysteresis():
    current = {'b1': 101, 'b2': 99, 'b3': 100}
    desired = {'b1': 100, 'b2': 100, 'b3': 100}
    assert not should_install_weights(current, desired, 'adaptive', False, True)
    assert should_install_weights(current, desired, 'adaptive', True, False)
    assert not should_install_weights(desired, desired, 'equal', True, True)


def test_adaptive_large_update_and_membership_change():
    current = {'b1': 100, 'b2': 100, 'b3': 100}
    desired = {'b1': 50, 'b2': 125, 'b3': 125}
    assert should_install_weights(current, desired, 'adaptive', False, True)
    assert not should_install_weights(current, desired, 'adaptive', False, False)
    assert should_install_weights(current, {'b1': 0, 'b2': 150, 'b3': 150},
                                  'adaptive', False, False)
