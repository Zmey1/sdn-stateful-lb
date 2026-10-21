"""Repeatable comparison of OpenFlow SELECT-weight policies.

Runs outside Mininet, entering the three existing client network namespaces
using nsenter. Saves per-run JSON, run-level CSV and a summary CSV. Does not
restart OVS or clear conntrack between experimental runs.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import time
from controller.benchmark_modes import MODE_NAMES
from controller.policy import meaningful_change
from controller.config import BACKENDS, CLIENTS

SCENARIOS = ('uniform', 'degraded-b1', 'cpu-b1')


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'Cannot read {path}: {exc}') from exc


def percentile(values, p):
    if not values:
        return None
    data = sorted(values)
    i = p / 100 * (len(data) - 1)
    lo, hi = int(i), min(len(data) - 1, int(i) + 1)
    return round(data[lo] + (data[hi] - data[lo]) * (i - lo), 3)


def summarize(clients: list[dict], measurement_seconds: float):
    counts = Counter()
    failures = Counter()
    latencies = []
    offered = rejected = 0
    for c in clients:
        offered += c['offered']
        rejected += c['backpressure_rejected']
        counts.update(c['backends'])
        failures.update(c['errors'])
        latencies.extend(c['latencies_ms'])
    success = sum(counts.values())
    failed = sum(failures.values()) + rejected
    if offered != success + failed:
        raise RuntimeError(f'Accounting mismatch: {offered} != {success} + {failed}')
    return {'offered': offered, 'success': success, 'failed': failed,
            'failure_rate_pct': round(100 * failed / max(1, offered), 3),
            'throughput_rps': round(success / measurement_seconds, 3),
            'p50_ms': percentile(latencies, 50),
            'p95_ms': percentile(latencies, 95),
            'p99_ms': percentile(latencies, 99),
            'mean_ms': round(statistics.mean(latencies), 3) if latencies else None,
            'b1': counts['b1'], 'b2': counts['b2'], 'b3': counts['b3'],
            'errors': dict(failures), 'backpressure_rejected': rejected}


def wait_for_mode(runtime: Path, mode: str, timeout=18):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            status = read_json(runtime / 'status.json')
            installed = status.get('installed_weights')
            desired = status.get('desired_weights')
            if not isinstance(installed, dict) or not isinstance(desired, dict):
                time.sleep(.25)
                continue
            # Adaptive targets can differ slightly from installed weights
            # because normal weight changes intentionally use hysteresis.
            synced = (installed == desired if mode != 'adaptive'
                      else not meaningful_change(installed, desired))
            if (status.get('benchmark_mode') == mode and
                    time.time() - status.get('timestamp', 0) < 7 and
                    synced and
                    all(x['state'] == 'HEALTHY' for x in status['backends'].values())):
                return status
        except (KeyError, TypeError, RuntimeError):
            pass
        time.sleep(.25)
    raise RuntimeError(f'Controller did not install mode {mode} with healthy backends. '
                       f'Check {runtime / "controller.log"}')


def scenario_profile(scenario):
    if scenario == 'uniform':
        return {b.name: {'delay_ms': 0, 'burn_ms': 0} for b in BACKENDS}
    if scenario == 'degraded-b1':
        return {b.name: {'delay_ms': 160 if b.name == 'b1' else 0,
                         'burn_ms': 0} for b in BACKENDS}
    if scenario == 'cpu-b1':
        return {b.name: {'delay_ms': 0,
                         'burn_ms': 75 if b.name == 'b1' else 0} for b in BACKENDS}
    raise ValueError(scenario)


def client_command(pid: int, rate: float, args):
    return ['nsenter', '-t', str(pid), '-n', '--', sys.executable,
            '-m', 'experiments.load_client', '--rate', str(rate),
            '--warmup', str(args.warmup), '--duration', str(args.duration),
            '--max-inflight', str(args.max_inflight),
            '--timeout', str(args.timeout)]


def run_load(runtime: Path, args):
    pids = read_json(runtime / 'host_pids.json')
    for c in CLIENTS:
        try:
            os.kill(int(pids[c.name]), 0)
        except (OSError, KeyError, ValueError) as exc:
            raise RuntimeError(f'Client namespace {c.name} is unavailable') from exc
    processes = {}
    for c in CLIENTS:
        processes[c.name] = subprocess.Popen(
            client_command(pids[c.name], args.qps / len(CLIENTS), args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    outputs = []
    for name, proc in processes.items():
        try:
            out, err = proc.communicate(timeout=args.warmup + args.duration +
                                        args.timeout + 40)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise RuntimeError(f'{name} load generator hung') from exc
        if proc.returncode:
            raise RuntimeError(f'{name} load generator failed: {err[-1500:]}')
        try:
            outputs.append(json.loads(out))
        except ValueError as exc:
            raise RuntimeError(f'{name} returned invalid JSON: {out[-600:]}') from exc
    return outputs


def process_cpu_seconds(pid):
    """CPU consumed by a process (user+system), not per-core percent."""
    try:
        raw = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(') ', 1)[1].split()
        ticks = int(raw[11]) + int(raw[12])
        return ticks / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, TypeError, IndexError):
        return None


def group_stats():
    try:
        return subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13',
                               'dump-group-stats', 's1'],
                              capture_output=True, text=True,
                              check=True, timeout=8).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def backend_telemetry(runtime: Path):
    return {b.name: read_json(runtime / f'{b.name}.json')
            for b in BACKENDS}


def controller_snapshot(runtime: Path):
    try:
        return read_json(runtime / 'status.json')
    except RuntimeError:
        return {}


def write_table(file: Path, rows: list[dict]):
    if not rows:
        return
    with file.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--runtime', required=True, type=Path)
    p.add_argument('--duration', type=float, default=15, help='measurement seconds')
    p.add_argument('--warmup', type=float, default=6)
    p.add_argument('--settle', type=float, default=8)
    p.add_argument('--qps', type=float, default=60, help='total offered HTTP requests/s')
    p.add_argument('--repeats', type=int, default=1)
    p.add_argument('--max-inflight', type=int, default=50, help='per client')
    p.add_argument('--timeout', type=float, default=3)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--modes', nargs='+', choices=MODE_NAMES, default=list(MODE_NAMES))
    p.add_argument('--scenarios', nargs='+', choices=SCENARIOS, default=['uniform', 'degraded-b1'])
    args = p.parse_args()
    if os.geteuid():
        p.error('Run with sudo; nsenter needs root privileges')
    if args.duration <= 0 or args.qps <= 0 or args.warmup < 0 or args.settle < 0 or args.repeats < 1:
        p.error('Invalid duration, rate, warmup, settle or repeats')
    if not shutil.which('nsenter'):
        p.error('nsenter missing (install util-linux)')
    runtime = args.runtime.resolve()
    if not (runtime / 'host_pids.json').exists():
        p.error('Must start the Milestone 4 Mininet lab first')
    # Require a current Milestone 4 controller, never silently benchmark M3.
    status = controller_snapshot(runtime)
    if 'benchmark_mode' not in status or time.time() - status.get('timestamp', 0) > 8:
        p.error('No live Milestone 4 controller: use --adaptive on the updated lab')
    root = runtime / 'bench_results' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    root.mkdir(parents=True, exist_ok=True)
    write_json(runtime / 'benchmark_active.json', {'pid': os.getpid(), 'started': time.time()})
    config = {k: v for k, v in vars(args).items() if k != 'runtime'}
    write_json(root / 'settings.json', config)
    rng = random.Random(args.seed)
    rows = []
    try:
        for scenario in args.scenarios:
            print(f'\nSCENARIO: {scenario}', flush=True)
            write_json(runtime / 'workload_profile.json', scenario_profile(scenario))
            combinations = [(mode, repeat) for repeat in range(1, args.repeats+1)
                            for mode in args.modes]
            rng.shuffle(combinations)
            for mode, repeat in combinations:
                write_json(runtime / 'benchmark_mode.json', {'mode': mode})
                wait_for_mode(runtime, mode)
                if args.settle:
                    time.sleep(args.settle)
                before = controller_snapshot(runtime)
                ctl_cpu_before = process_cpu_seconds(before.get('controller_pid'))
                stats_before = group_stats()
                telemetry_before = backend_telemetry(runtime)
                print(f'  {mode} run {repeat}: weights={before.get("installed_weights")}',
                      flush=True)
                clients = run_load(runtime, args)
                after = controller_snapshot(runtime)
                ctl_cpu_after = process_cpu_seconds(after.get('controller_pid'))
                stats_after = group_stats()
                telemetry_after = backend_telemetry(runtime)
                metrics = summarize(clients, args.duration)
                ctl_cpu = (round(100*(ctl_cpu_after-ctl_cpu_before)/args.duration, 3)
                           if ctl_cpu_before is not None and ctl_cpu_after is not None
                           else None)
                row = {'scenario': scenario, 'mode': mode, 'repeat': repeat,
                       'controller_cpu_pct_one_core': ctl_cpu,
                       **{k: v for k, v in metrics.items() if k != 'errors'}}
                rows.append(row)
                artifact = {'summary': row, 'errors': metrics['errors'],
                            'weights_before': before.get('installed_weights'),
                            'weights_after': after.get('installed_weights'),
                            'status_before': before, 'status_after': after,
                            'ovs_group_stats_before': stats_before,
                            'ovs_group_stats_after': stats_after,
                            'telemetry_before': telemetry_before,
                            'telemetry_after': telemetry_after,
                            'clients': clients}
                write_json(root / f'{scenario}_{mode}_{repeat}.json', artifact)
                write_table(root / 'runs.csv', rows)
                print(f'    success={metrics["success"]}/{metrics["offered"]}, '
                      f'p95={metrics["p95_ms"]}ms, '
                      f'throughput={metrics["throughput_rps"]}rps', flush=True)
        groupings = {}
        for r in rows:
            groupings.setdefault((r['scenario'], r['mode']), []).append(r)
        aggregates = []
        for (scenario, mode), items in sorted(groupings.items()):
            aggregate = {'scenario': scenario, 'mode': mode, 'runs': len(items)}
            for key in ('throughput_rps', 'failure_rate_pct', 'p50_ms',
                        'p95_ms', 'p99_ms', 'mean_ms', 'controller_cpu_pct_one_core'):
                values = [r[key] for r in items if r[key] is not None]
                aggregate[key] = round(statistics.mean(values), 3) if values else None
            aggregates.append(aggregate)
        write_table(root / 'summary.csv', aggregates)
        print(f'\nRESULTS: {root}', flush=True)
        for a in aggregates:
            print(f'{a["scenario"]:>12} {a["mode"]:>8} '
                  f'rps={a["throughput_rps"]} p95={a["p95_ms"]} '
                  f'failure%={a["failure_rate_pct"]}', flush=True)
    finally:
        # Dashboard actions must not interfere with an active benchmark.
        active = runtime / 'benchmark_active.json'
        try:
            if read_json(active).get('pid') == os.getpid():
                active.unlink(missing_ok=True)
        except (OSError, RuntimeError):
            pass
        # Restore safe defaults, even if one experiment fails.
        write_json(runtime / 'workload_profile.json', scenario_profile('uniform'))
        write_json(runtime / 'benchmark_mode.json', {'mode': 'adaptive'})


if __name__ == '__main__':
    main()
