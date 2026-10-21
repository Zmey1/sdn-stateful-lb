# Milestone 4: Reproducible SDN load-balancer benchmarks

This extends **the tested Milestone 3 topology**. The OpenFlow NAT/conntrack
pipeline, health-probe host, and recovery mechanisms are unchanged. Mode changes
modify only new-connection group selection. Connection continuity relies on
existing OVS conntrack mappings.

## Installation

Start from a new project directory, **not** your Milestone 1/3 directory.
Stop any *manually restarted* Milestone 3 controller with Ctrl+C before exiting
its Mininet CLI. Stop the old Mininet lab with `exit`.

From this project directory:

```bash
python3 -m pytest tests -q
sudo python3 -m topology.lab --adaptive
```

The console prints a fresh runtime directory, such as `/tmp/sdn-lb-abcdef`.
Leave Mininet running. In a **second normal Ubuntu terminal** in this same
Milestone 4 project directory:

```bash
RUNTIME=/tmp/sdn-lb-abcdef # replace with YOUR printed runtime
sudo python3 -m controller.cli --runtime "$RUNTIME" status
sudo python3 -m controller.cli --runtime "$RUNTIME" mode equal
sudo python3 -m controller.cli --runtime "$RUNTIME" status
sudo python3 -m controller.cli --runtime "$RUNTIME" mode adaptive
```

The `mode` command writes a policy control file. Allow the policy daemon to
process it; `status.benchmark_mode` displays the applied mode.

## Small smoke benchmark

```bash
sudo python3 -m experiments.benchmark_suite \
  --runtime "$RUNTIME" --modes equal adaptive \
  --scenarios uniform --qps 30 --duration 10 \
  --warmup 4 --settle 5 --repeats 1
```

## Full comparison

```bash
sudo python3 -m experiments.benchmark_suite \
  --runtime "$RUNTIME" \
  --scenarios uniform degraded-b1 cpu-b1 \
  --modes equal capacity adaptive \
  --qps 60 --warmup 8 --duration 30 --settle 10 \
  --repeats 3 --seed 42
```

The runner launches three clients in their **Mininet network namespaces**
using `nsenter`; it does not install external dependencies such as Locust.
Each request opens a new TCP connection to the VIP, sends one HTTP request,
records response time and backend identity, then closes the connection.
Clients offer requests at a fixed rate, with bounded concurrency. Requested
connections exceeding the cap are counted as **backpressure rejections**.

During a run, avoid manually changing group weights, stopping the controller,
or restarting servers. The controller must be running from this updated
Milestone 4 project, not the Milestone 3 code.

## Policies

- `equal`: 100:100:100 static weights.
- `capacity`: 50:125:125 fixed weights. These encode a **hypothetical prior**
  that b1 should receive less traffic; they are not empirically measured
  physical server capacities. Capacity-aware weights should be independently
  calibrated before using this as a research baseline.
- `adaptive`: original CPU/latency/connections-aware Milestone 3 policy.

All three use the *same* OVS SELECT/conntrack/NAT pipeline. All three exclude
DOWN and DRAINING backends using the same health state machine. The adaptive
recovery ramp remains specific to the adaptive strategy.

## Controlled workloads

- `uniform`: all three backends respond without synthetic delay.
- `degraded-b1`: 160ms server-side delay applied only to b1's `/work`.
- `cpu-b1`: roughly 75ms of CPU-bound Python work for each `/work` on b1;
  `/health` remains unaffected. As all hosts share one physical machine,
  CPU stress can also affect other emulated hosts, so interpret carefully.

Workload profiles apply to `/work`, never to `/health`. They are restored to
uniform when the suite completes or raises a handled exception. Control mode
is restored to adaptive on exit. If killed forcibly, manually restore mode and
profile using the CLI and `workload_profile.json`.

## Results

Saved under:

```
/tmp/sdn-lb-XXXX/bench_results/YYYYMMDDTHHMMSSZ/
  settings.json
  runs.csv
  summary.csv
  uniform_equal_1.json
  uniform_capacity_1.json
  uniform_adaptive_1.json
  ...
```

The per-run JSON includes client latencies, errors, backend response counts,
controller CPU use, backend telemetry snapshots, policy before/after, and raw
OVS group statistics. `runs.csv` provides individual replicates; `summary.csv`
provides means across replicates (not confidence intervals).

- `throughput_rps`: successful HTTP responses / intended measurement duration.
- `failure_rate_pct`: timeouts/errors/backpressure / all offered requests.
- P50/P95/P99: **successful requests only**. Always interpret alongside failure
  rate, because a policy that times out slow requests may have an artificially
  low success-only P95.
- Backend distribution: identities in successful HTTP response bodies, not
  buckets sampled or controller decisions; this measures actual service.
- `controller_cpu_pct_one_core`: controller process CPU time / test wall time.
  It is not total system CPU and excludes OVS and probe overhead.

The workloads are randomized in order within a scenario using a fixed seed.
There is a warmup interval for each run. There is no reset of Linux conntrack
or independent re-randomization of server performance between runs. State
carry-over, limited sample size, a single emulation host and OVS datapath
caching constrain causal/performance claims. Repeat with different seeds,
request rates and on independent hosts before claiming production benefit.

## Completion criteria

1. All 26 pure-Python tests pass locally.
2. Smoke test succeeds in Mininet using both policies.
3. Full suite produces valid `runs.csv` and `summary.csv`.
4. All results report backend identities, errors, and nonzero offered load.
5. Adaptive weights change under induced server degradation where expected.
6. Verify session persistence again under concurrent policy changes.

**Limitations:** We cannot validate this live OVS/Mininet integration in the
artifact-generation environment. This milestone does not yet test packet loss
injected by `tc netem`, full switch failure, cross-host scalability, or
high-availability conntrack replication. Those are separate experiments.
