# Stateful dynamic SDN load balancer (Ryu + Open vSwitch + Mininet)

Clients reach one virtual IP (`10.0.3.100:8080`). Open vSwitch picks a backend
for each **new** TCP connection from a weighted OpenFlow SELECT group, pins the
connection with conntrack NAT, and forwards everything in the data plane — no
packet ever goes to the controller. A **Ryu** OpenFlow 1.3 controller
continuously measures how loaded each backend is and rewrites the group
weights so that every backend runs at the same load per worker, i.e. each one
receives traffic in proportion to its capacity.


## Architecture

```
 h1 h2 h3 (clients)                    p1 (health probe, own namespace)
      \  |  /                              |  GET /health to each backend IP
       \ | /  TCP to VIP 10.0.3.100:8080   |
   +------------------- OVS s1 (fail-mode secure) --------------------+
   | table 0  classify: client->VIP, backend replies, probe bypass    |
   | table 10 conntrack: committed (ct_mark=1) -> table 30            |
   |          new SYN -> group 100 (SELECT, weighted buckets)         |
   |            bucket: ct(commit, nat(dst=backend), ct_mark=1)       |
   | table 30 route: dec_ttl, rewrite MACs, output                    |
   +-------------------------------^----------------------------------+
        |        |        |        | OpenFlow 1.3 (127.0.0.1:6653)
       b1       b2       b3        |  PortDesc, GroupDesc, GroupStats, GroupMod
  (2 / 4 / 6 workers x 50 ms)  Ryu controller (controller/lb_app.py)
        \_______ telemetry, probe results, drains: /tmp/sdn-lb-*/ files
```

Changing group weights only affects **new** connections; established ones keep
their backend through conntrack (verified by the self-test below).

## How the dynamic balancing works

**Capacity.** b1, b2, b3 are a small, medium and large server: 2, 4 and 6
workers at 50 ms per request, i.e. 40, 80 and 120 req/s (240 in total,
`controller/config.py`). Extra requests queue, so an overloaded server gets
slow, as a real one would. Injecting a 100 ms delay on a backend keeps its
workers busy 3x longer, cutting it to a third of its capacity while `/health`
still passes (a gray failure).

**Measurement.** Backends publish cumulative counters (`arrived`, `busy_s`,
`completed`). Every 2 s the controller differences them into the **offered
load per worker**

```
rho_i = arrival_rate_i * mean_service_time_i / workers_i
```

which equals utilization below saturation and exceeds 1 when requests arrive
faster than the server can serve them. Unlike queue length or latency it has no
memory of past overload, which keeps the control loop from oscillating.

**Control law.** Starting from the weights *actually installed in the switch*:

```
rho_bar = traffic-weighted mean of rho over healthy backends
share_i <- share_i * clamp((rho_bar / rho_i) ** 0.5, 1/1.5, 1.5)
```

Busier-than-average backends lose share, quieter ones gain it, until all run
at the same load. At that point traffic is proportional to capacity — without
the capacity ever being configured. Safeguards: a 10 % deadband, no changes
when the cluster is idle (no signal), a 3 % minimum share so every healthy
backend keeps being measured, and switch updates at most every 5 s unless a
backend joins or leaves (then immediately).

**Health.** Independent of load, the `p1` probe checks `/health` on each
backend directly every 2 s. Three failures mark a backend `DOWN` (weight 0);
three successes start `RECOVERING`, which ramps from 10 % to 100 % of an
average share over 30 s, then `HEALTHY`. Manual drain sets weight 0 without
touching existing connections.

**Evidence.** `tests/test_policy.py` closes the loop around a simulated
cluster of the lab's servers at 150 req/s: from 100 : 100 : 100 the weights
reach the size ratio (≈ 50 : 100 : 150) in about 8 s. When b2 drops to a third
of its capacity mid-run they move to ≈ 66 : 40 : 194 within about 8 s and b2's
queue drains, and they return once b2 recovers. The simulation omits the 5 s
minimum between switch updates, so in the real lab expect roughly 10–20 s.

## Requirements

- Conda (Anaconda or Miniconda)
- Python 3.9 in a Conda env named `sdn-ryu` (Ryu 4.34 does not support newer Pythons)

## Setup (once)

```bash
git clone https://github.com/Zmey1/sdn-stateful-lb.git && cd sdn-stateful-lb
conda create -n sdn-ryu python=3.9 -y
conda activate sdn-ryu
sudo -E bash scripts/setup_ubuntu.sh   # apt packages, Mininet into the env, requirements
python -m pytest                        # 57 tests, no root needed
```

`setup_ubuntu.sh` installs Mininet and Open vSwitch with apt, copies Mininet's
Python package into the env, and installs `requirements.txt`, so every
component (Mininet lab, Ryu, probe, dashboard, experiments) runs on the
`sdn-ryu` interpreter.

## Run

```bash
conda activate sdn-ryu
./start_dev.sh            # Mininet + Ryu + probe + dashboard in one terminal
```

1. Enter your password when `sudo` asks.
2. Wait for `[FlowScope] SDN network + dashboard ready`.
3. Open http://127.0.0.1:8765. The header shows **SYSTEM ONLINE**, and within
   about 10 s b1, b2 and b3 go DOWN → RECOVERING → HEALTHY.
4. Follow the four steps on the dashboard (or the demo below).
5. Type `exit` at the `mininet>` prompt to stop everything.

Only one lab can run at a time; a second `start_dev.sh` or `topology.lab`
refuses to start while one is running. To inspect a running lab, use another
terminal:

```bash
RUNTIME="$(cat .dev/current_runtime)"
PY="$CONDA_PREFIX/bin/python"
sudo "$PY" -m controller.cli status --runtime "$RUNTIME"   # weights, loads, states
sudo "$PY" -m controller.cli show                          # OVS groups/flows
sudo tail -f "$RUNTIME/controller.log"
```

Traffic by hand, at the `mininet>` prompt: `h1 curl -s http://10.0.3.100:8080/work`.

Options: `./start_dev.sh --read-only`, `--port 8766`, `--bench-root DIR`.
Without the dashboard: `sudo "$PY" -m topology.lab`.

**Self-test** (after exiting any running lab): starts its own lab, opens 30 TCP
connections, changes the weights, and checks that every connection keeps its
backend. It ends with `PASS: session identity and source ports stable across
group MOD`.

```bash
sudo "$CONDA_PREFIX/bin/python" -m topology.lab --self-test
```

## Presentation demo (about 5 minutes)

All from the dashboard's guided panel; watch the **latency chart** (p95 per
second, measured by the client) and the weights.

1. **Learn the sizes** — *Run steady traffic* (150 req/s, 20 s). Nobody tells
   the controller the server sizes; the weights move from 100 : 100 : 100 to
   about 50 : 100 : 150.
2. **Gray failure, adaptive** — *Run with ADAPTIVE* (45 s). From 15 s to 30 s
   b2 is 100 ms slower but still HEALTHY. p95 rises, then falls as the
   controller moves traffic off b2; weights return when b2 recovers.
3. **Same failure, static weights** — *Run with STATIC weights*: fixed
   50 : 100 : 150, what an operator would configure from the sizes. b2 stays
   overloaded until it recovers; the chart overlays this run on the previous
   one (dashed).
4. **Failure** — *Crash* b2: 3 probe failures later it is DOWN with weight 0.
   *Restart*: DOWN → RECOVERING (ramping) → HEALTHY.
5. **Overload** — custom traffic at 280 req/s: above the 240 req/s total no
   policy helps. The balancer spreads load; it cannot create capacity.
6. **Reset the lab.**

**Compared with production load balancers.** The control law is the one used
by gRPC's `weighted_round_robin` with backend-reported utilization (ORCA):
weight ∝ request rate / utilization, i.e. equal load per worker. Reweighting a
server that is slow but still healthy is what AWS ALB's automatic target
weights do; the recovery ramp and drain mirror HAProxy/Envoy slow start and
connection draining. Least-connections, the usual per-request alternative,
does not fit here: an OVS SELECT group cannot count connections, and sending
every new connection to the controller (Packet-In) would not scale. Limits:
one switch, emulated capacity (worker slots, not real CPU), a 2 s control loop
rather than per-request decisions.

## Experiments

```bash
# Failure + recovery: crash b1, verify exclusion and gradual re-entry.
sudo "$PY" -m experiments.recovery_check --runtime "$RUNTIME" --backend b1

# Benchmark: adaptive vs equal vs capacity (static 50:100:150 from the sizes)
# for scenarios uniform and step-b2 (b2 slowed for the middle third of a 45 s
# run). Reports p99, p95, failures and peak load per worker; saves per-run JSON
# (with a weight/load timeline), runs.csv and summary.csv under
# $RUNTIME/bench_results/; the dashboard shows the newest summary.
sudo "$PY" -m experiments.benchmark_suite --runtime "$RUNTIME" --repeats 3

# Session persistence across weight changes. Starts its own lab, so first exit
# the running one (it refuses to start beside it); exits when done.
sudo "$PY" -m topology.lab --self-test --connections 30
```

**Controller restart without disruption:**
`sudo kill -TERM "$(cat "$RUNTIME/controller.pid")"`. OVS keeps forwarding with
the last weights (fail-mode secure). Restart with
`sudo "$PY" -m controller.lb_app --runtime "$RUNTIME"`: on reconnect Ryu reads
the existing group, adopts its weights and saved health state, and sends no
flow or group changes, so conntrack sessions are untouched.
