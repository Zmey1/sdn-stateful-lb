# Stateful dynamic SDN load balancer (Ryu + Open vSwitch + Mininet)

Clients reach one virtual IP (`10.0.3.100:8080`). Open vSwitch picks a backend
for each **new** TCP connection from a weighted OpenFlow SELECT group, pins the
connection with conntrack NAT, and forwards everything in the data plane — no
packet ever goes to the controller. A **Ryu** OpenFlow 1.3 controller
continuously measures how loaded each backend is and rewrites the group
weights so that every backend runs at the same load per worker, i.e. each one
receives traffic in proportion to its capacity.

> Research lab, not a production HA appliance. Validate on Ubuntu with Mininet.

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
  (4 workers x 50 ms each)   Ryu controller (controller/lb_app.py)
        \_______ telemetry, probe results, drains: /tmp/sdn-lb-*/ files
```

Changing group weights only affects **new** connections; established ones keep
their backend through conntrack (verified by the self-test below).

## How the dynamic balancing works

**Capacity.** Each backend has 4 workers and needs 50 ms per request
(80 req/s). Extra requests queue, so an overloaded server gets slow, as a real
one would. Injecting a 100 ms delay on b1 keeps its workers busy 3x longer: b1
drops to one third of the capacity of b2/b3.

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
cluster: with b1 at one third capacity and 62 % offered load, the weights
converge to the capacity ratio (≈ 43 : 128 : 128) in about 8 s and stay there.
Against the real backend servers the same controller converged in about 20 s to
≈ 52 : 121 : 127, with all three backends near 80 % load per worker.

## Requirements

- Ubuntu 22.04 or newer (Mininet and Open vSwitch need Linux and `sudo`);
  on a Mac, run it in an Ubuntu VM — see [macOS](#macos-apple-silicon-or-intel)
- Conda (Anaconda or Miniconda)
- Python 3.9 in a Conda env named `sdn-ryu` (Ryu 4.34 does not support newer Pythons)

## Setup (once)

```bash
git clone https://github.com/Zmey1/sdn-stateful-lb.git && cd sdn-stateful-lb
conda create -n sdn-ryu python=3.9 -y
conda activate sdn-ryu
sudo -E bash scripts/setup_ubuntu.sh   # apt packages, Mininet into the env, requirements
python -m pytest                        # 55 tests, no root needed
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

## macOS (Apple Silicon or Intel)

Mininet needs Linux kernel features (network namespaces, the Open vSwitch
datapath, conntrack), so on a Mac the lab runs inside an Ubuntu VM.
[Multipass](https://multipass.run) works on both Apple Silicon and Intel.

**1. Create the VM** (Mac terminal):

```bash
brew install --cask multipass
multipass launch 22.04 --name sdn --cpus 4 --memory 4G --disk 20G
multipass shell sdn
```

**2. Inside the VM**, install Miniconda (the `uname -m` picks the ARM or
Intel build), then follow [Setup](#setup-once) and [Run](#run) unchanged:

```bash
curl -fsSLO "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-$(uname -m).sh"
bash "Miniconda3-latest-Linux-$(uname -m).sh" -b && ~/miniconda3/bin/conda init bash
exec bash
sudo apt-get update && sudo apt-get install -y gh && gh auth login   # the repo is private
git clone https://github.com/Zmey1/sdn-stateful-lb.git && cd sdn-stateful-lb
conda create -n sdn-ryu python=3.9 -y && conda activate sdn-ryu
sudo -E bash scripts/setup_ubuntu.sh
./start_dev.sh
```

**3. Open the dashboard on the Mac.** It listens only on the VM's
`127.0.0.1` (its controls change the lab), so tunnel to it over SSH. Once,
add your Mac's SSH key to the VM (use your `.pub` file; create one with
`ssh-keygen` if you have none):

```bash
multipass exec sdn -- bash -c "echo '$(cat ~/.ssh/id_ed25519.pub)' >> ~/.ssh/authorized_keys"
```

Then, while `./start_dev.sh` runs in the VM:

```bash
ssh -N -L 8765:127.0.0.1:8765 ubuntu@"$(multipass info sdn | awk '/IPv4/{print $2}')"
```

and open http://127.0.0.1:8765 on the Mac. Stop the VM with
`multipass stop sdn`; `multipass start sdn` and `multipass shell sdn` resume it.

## Presentation demo (about 5 minutes)

All from the dashboard's "Try the load balancer" panel.

1. **Baseline** — *Run baseline traffic* (150 req/s; the cluster serves 240).
   The three backends sit at similar load per worker; the switch-observed
   connection shares track the weights 100/100/100.
2. **Degrade b1** — *Add 100 ms to b1*. b1 still answers `/health`, so it
   stays HEALTHY: this is a capacity problem, not a failure.
3. **Adapt** — *Run degraded traffic* (150 req/s, 30 s). b1's load per worker
   climbs past 100 %, then its weight drops to about 45–55 while b2/b3 rise,
   until all three loads are equal again.
4. **Contrast** — *Advanced controls → EQUAL*, run traffic again: b1 is
   overloaded at a third of the traffic and its response time explodes. Switch
   back to *ADAPTIVE*.
5. **Failure** — *Crash* b1: 3 probe failures later it is DOWN with weight 0;
   traffic continues on b2/b3. *Restart*: DOWN → RECOVERING (small weight,
   ramping) → HEALTHY.
6. **Reset the lab.**

Talking points: the controller never sees a data packet; weights change only
new-connection selection; the switch is the source of truth (installed weights
are read back after every GroupMod).

## Experiments

```bash
# Failure + recovery: crash b1, verify exclusion and gradual re-entry.
sudo "$PY" -m experiments.recovery_check --runtime "$RUNTIME" --backend b1

# Benchmark: adaptive vs equal vs capacity (hand-computed oracle 42:129:129)
# for scenarios uniform, slow-b1 and step-b1 (b1 slows mid-run, then recovers).
# Saves per-run JSON (with a weight/load timeline), runs.csv and summary.csv
# under $RUNTIME/bench_results/; the dashboard shows the newest summary.
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

## Parameters

| Property | Value |
|---|---|
| Backend capacity | 4 workers × 50 ms = 80 req/s (`controller/config.py`) |
| Probe interval / timeout / freshness | 2 s / 0.75 s / 5 s |
| DOWN after / recovery after | 3 failed / 3 successful checks |
| Recovery ramp | 30 s, 10 % → 100 % of average share |
| Control cycle / minimum update interval | 2 s / 5 s (membership changes immediate) |
| Gain / max step / deadband / idle / floor | 0.5 / ×1.5 / 10 % / ρ̄ < 0.05 / 3 % share |
| Total weight, group, conntrack zone | 300, SELECT 100, zone 10 |

## Dashboard

FastAPI on 127.0.0.1 only, serving an offline React page. Controls
(`--allow-controls`, on by default from `start_dev.sh`) are bounded: policy
mode, drain, injected delay (≤ 350 ms), crash/restart of a verified backend
worker, a 3–30 s traffic test from h1, and reset. Requests from other origins
are rejected, and controls lock while a benchmark runs.

## Troubleshooting

- `OVS bridge s1 already exists` — exit the old lab; if it crashed, `sudo mn -c`.
- `Another lab is running` — exit the other lab first; if it crashed, `sudo mn -c`.
- `Dashboard port unavailable` — a dashboard is still running; stop it or pass `--port 8766`.
- Controller offline or CONTROLLER ERROR — read `sudo tail -80 "$RUNTIME/controller.log"`;
  a failing control step is logged there with its traceback.
- `sdn-ryu Python cannot import Mininet` — rerun `sudo -E bash scripts/setup_ubuntu.sh`.
- 0 req/s — expected until you generate traffic; probes are not counted.
- No benchmark table — run the suite or pass `--bench-root` to `start_dev.sh`.

## Limitations

- Probes check direct backend reachability from `p1`, not each client→VIP path.
- Runtime files are a lab-only telemetry transport; production needs an
  authenticated management network.
- One switch, IPv4 TCP 8080. No conntrack replication: an OVS restart or a
  backend crash can break established sessions.
- With very few connections, SELECT hashing does not give exact ratios.

## Source map

| Path | Role |
|---|---|
| `controller/lb_app.py` | Ryu app: switch connect, reconciliation, control loop |
| `controller/openflow.py` | Flow and group messages (conntrack/NAT pipeline) |
| `controller/policy_loop.py` | One control cycle: files → policy → weights → status |
| `controller/policy.py` | Health state machine and load-equalizing control law |
| `controller/health_probe.py` | Independent HTTP probes from `p1` |
| `controller/benchmark_modes.py` | adaptive / equal / capacity modes |
| `controller/runtime.py` | Atomic runtime JSON files, process checks, drains |
| `controller/cli.py`, `ofctl.py` | Operator commands; read-only OVS inspection |
| `backend/server.py` | Finite-capacity HTTP backend + telemetry |
| `topology/lab.py` | Mininet topology, process lifecycle, self-test |
| `monitoring/` | Dashboard API and static UI |
| `experiments/` | Load generator, benchmark suite, recovery and session checks |
| `tests/` | Unit, simulation and OpenFlow-encoding tests |

References: [OVS conntrack tutorial](https://docs.openvswitch.org/en/stable/tutorials/ovs-conntrack/),
[OVS actions](https://docs.openvswitch.org/en/stable/ref/ovs-actions.7/),
[Ryu](https://ryu.readthedocs.io/).
