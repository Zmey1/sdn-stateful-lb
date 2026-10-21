import json
import os
import time

from fastapi.testclient import TestClient
import pytest

from monitoring.core import Monitor, benchmark_data, controller_events, pid_alive
from monitoring.server import create_app


@pytest.fixture
def runtime(tmp_path):
    now = time.time()
    (tmp_path / 'host_pids.json').write_text(json.dumps({'h1': os.getpid()}))
    (tmp_path / 'status.json').write_text(json.dumps({
        'timestamp': now, 'controller_pid': os.getpid(), 'benchmark_mode': 'adaptive',
        'installed_weights': {'b1': 100, 'b2': 100, 'b3': 100},
        'desired_weights': {'b1': 100, 'b2': 100, 'b3': 100},
        'backends': {n: {'state': 'HEALTHY', 'load': 0.42} for n in ('b1', 'b2', 'b3')},
        'observed_share': {'b1': 0.2, 'b2': 0.4, 'b3': 0.4},
    }))
    (tmp_path / 'health.json').write_text(json.dumps({
        n: {'timestamp': now, 'ok': True, 'latency_ms': 1.8} for n in ('b1','b2','b3')
    }))
    for n in ('b1','b2','b3'):
        (tmp_path / f'{n}.json').write_text(json.dumps({
            'timestamp': now, 'response_ms': 9, 'in_system': 1, 'workers': 4,
            'arrived': 20, 'busy_s': 1.0, 'completed': 20,
        }))
    return tmp_path


def test_dashboard_reads_real_status(runtime):
    with TestClient(create_app(runtime)) as client:
        page = client.get('/')
        assert page.status_code == 200 and 'FlowScope' in page.text
        data = client.get('/api/snapshot').json()
        assert data['controller']['alive']
        assert data['controller']['mode'] == 'adaptive'
        assert data['controller']['synced']
        assert data['controls_enabled'] is False
        assert len(data['backends']) == 3
        assert all(x['probe_ok'] for x in data['backends'])
        assert data['backends'][0]['response_ema_ms'] == 9
        assert data['backends'][0]['load'] == 0.42
        assert data['backends'][0]['observed_share_pct'] == 20


def test_no_controls_unless_opted_in(runtime):
    with TestClient(create_app(runtime)) as client:
        r = client.post('/api/control/mode', json={'mode': 'equal'})
        assert r.status_code == 403
        assert not (runtime/'benchmark_mode.json').exists()


def test_controls_write_only_bounded_known_files(runtime):
    with TestClient(create_app(runtime, allow_controls=True)) as client:
        assert client.post('/api/control/mode',json={'mode':'equal'}).status_code == 200
        assert json.loads((runtime/'benchmark_mode.json').read_text()) == {'mode':'equal'}
        assert client.post('/api/control/mode',json={'mode':'not-an-option'}).status_code == 400
        assert client.post('/api/control/drain',json={'backend':'b1','enabled':True}).status_code == 200
        assert json.loads((runtime/'drain.json').read_text()) == ['b1']
        assert client.post('/api/control/workload',json={'backend':'b1','delay_ms':160}).status_code == 200
        assert client.post('/api/control/workload',json={'backend':'b2','delay_ms':400}).status_code == 422
        assert client.post('/api/control/workload',json={'backend':'../../etc','delay_ms':0}).status_code == 400
        assert client.post('/api/control/reset',json={}).status_code == 200
        assert json.loads((runtime/'drain.json').read_text()) == []
        assert json.loads((runtime/'benchmark_mode.json').read_text()) == {'mode':'adaptive'}
        assert json.loads((runtime/'workload_profile.json').read_text())['b1']['delay_ms'] == 0


def test_control_rejects_other_browser_origins(runtime):
    with TestClient(create_app(runtime, allow_controls=True)) as client:
        r = client.post('/api/control/drain', json={'backend':'b2','enabled':True},
                        headers={'Origin':'https://bad.example'})
        assert r.status_code == 403


def test_benchmark_blocks_mutations(runtime):
    (runtime/'benchmark_active.json').write_text(json.dumps({'pid':os.getpid()}))
    with TestClient(create_app(runtime, allow_controls=True)) as client:
        assert client.post('/api/control/mode',json={'mode':'capacity'}).status_code == 409
        assert client.get('/api/snapshot').json()['benchmark_active'] is True


def test_stale_controller_blocks_controls(runtime):
    status=json.loads((runtime/'status.json').read_text())
    status['timestamp']-=100
    (runtime/'status.json').write_text(json.dumps(status))
    with TestClient(create_app(runtime, allow_controls=True)) as client:
        assert not client.get('/api/snapshot').json()['controller']['alive']
        assert client.post('/api/control/mode',json={'mode':'equal'}).status_code==409


def test_benchmark_csv_loading(runtime):
    folder=runtime/'bench_results'/'20261011T211533Z'
    folder.mkdir(parents=True)
    (folder/'summary.csv').write_text('scenario,mode,runs,p95_ms,throughput_rps\nslow-b1,adaptive,3,90.758,60\n')
    result=benchmark_data(runtime/'bench_results')
    assert result['available']
    assert result['summary'][0]['p95_ms']==90.758
    assert result['summary'][0]['mode']=='adaptive'


def test_monitor_counts_actual_completions(runtime):
    monitor=Monitor(runtime)
    monitor.snapshot()
    monitor._last_sample_at-=2
    data=json.loads((runtime/'b1.json').read_text())
    data['completed']=40
    (runtime/'b1.json').write_text(json.dumps(data))
    snap=monitor.snapshot()
    assert snap['window_requests']==20
    assert snap['backends'][0]['window_share_pct']==100
    assert 0 < snap['total_rps'] < 11


def test_missing_log_and_unreadable_benchmark(runtime):
    assert controller_events(runtime) == []
    assert benchmark_data(runtime/'missing')['available'] is False
    assert pid_alive(-1) is False


def test_fault_controls_only_named_backends(runtime):
    with TestClient(create_app(runtime, allow_controls=True)) as client:
        assert client.post('/api/control/crash', json={'backend':'../../etc'}).status_code == 400
        # Fixture is not an actual live backend; must not kill arbitrary PID.
        assert client.post('/api/control/crash', json={'backend':'b1'}).status_code == 409
        assert client.post('/api/control/restart',json={'backend':'unknown'}).status_code == 400
