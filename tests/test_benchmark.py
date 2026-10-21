import json
import pytest
from controller.benchmark_modes import read_mode, select_weights, MODE_NAMES
from experiments.benchmark_suite import percentile, summarize, scenario_profile
from backend.server import runtime_workload


def _states():
    return {n: {'state': 'HEALTHY', 'probe_ok': True}
            for n in ('b1', 'b2', 'b3')}


def test_equal_static_health_mask():
    states = _states()
    states['b1']['state'] = 'DOWN'
    weights = select_weights('equal', {'b1': 100, 'b2': 100, 'b3': 100}, states)
    assert weights == {'b1': 0, 'b2': 100, 'b3': 100}


def test_capacity_static():
    assert select_weights('capacity', {}, _states()) == {
        'b1': 50, 'b2': 125, 'b3': 125}


def test_adaptive_preserved():
    adaptive = {'b1': 10, 'b2': 120, 'b3': 170}
    assert select_weights('adaptive', adaptive, _states()) == adaptive


def test_invalid_mode_fails_closed(tmp_path):
    (tmp_path / 'benchmark_mode.json').write_text('{"mode":"typo"}')
    with pytest.raises(ValueError):
        read_mode(tmp_path)
    (tmp_path / 'benchmark_mode.json').unlink()
    assert read_mode(tmp_path) == 'adaptive'


def test_scenario_profile():
    assert scenario_profile('uniform')['b1']['delay_ms'] == 0
    assert scenario_profile('degraded-b1')['b1']['delay_ms'] == 160
    assert scenario_profile('degraded-b1')['b3']['delay_ms'] == 0


def test_backend_profile(tmp_path):
    assert runtime_workload(tmp_path, 'b1') == {}
    (tmp_path / 'workload_profile.json').write_text(json.dumps({
        'b1': {'delay_ms': 80, 'burn_ms': 20}}))
    assert runtime_workload(tmp_path, 'b1') == {'delay_ms': 80, 'burn_ms': 20}
    assert runtime_workload(tmp_path, 'b2') == {'delay_ms': 0, 'burn_ms': 0}


def test_percentiles_and_accounting():
    assert percentile([], 95) is None
    assert percentile([5], 95) == 5
    samples = [
        {'offered': 4, 'backpressure_rejected': 1, 'success': 2,
         'errors': {'timeout': 1}, 'backends': {'b1': 1, 'b2': 1},
         'latencies_ms': [10, 30]},
        {'offered': 2, 'backpressure_rejected': 0, 'success': 2,
         'errors': {}, 'backends': {'b3': 2}, 'latencies_ms': [20, 40]},
    ]
    result = summarize(samples, 2)
    assert result['success'] == 4
    assert result['failed'] == 2
    assert result['throughput_rps'] == 2
    assert result['p50_ms'] == 25
    assert result['b3'] == 2


def test_cpu_scenario():
    assert scenario_profile('cpu-b1')['b1']['burn_ms'] == 75
    assert scenario_profile('cpu-b1')['b2']['burn_ms'] == 0


from controller.policy_loop import should_install_weights


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
