> **Milestone 4 added:** See [MILESTONE4.md](MILESTONE4.md) for the automated benchmark suite and smoke-test commands.

# Stateful adaptive SDN load balancer: Milestone 3

This is **data-plane load balancing** with Open vSwitch/OpenFlow, not per-connection Python Packet-In forwarding. Existing connections are pinned using OVS conntrack DNAT (`ct_mark=1`), and new TCP connections enter a weighted SELECT group.

**New in Milestone 3:** independent active HTTP probes, automatic DOWN/RECOVERING/HEALTHY transitions, gradual recovery weight ramp, manual drain, restart reconciliation without deleting flows or conntrack mappings, duplicate-controller protection, and an end-to-end failure/recovery experiment.

> This is a Mininet research lab, **not a production-ready HA appliance**. Controller restart does not reset installed forwarding rules, but an OVS switch crash can still lose connections. We could run pure-Python tests in the code-generation environment, not a live Mininet datapath. Validate on Ubuntu.

## 1. Setup

Requirements: Ubuntu, root access, `mininet`, `openvswitch-switch`, `ovs-ofctl`, `mnexec`, and Python 3.10+.

```bash
sudo bash scripts/setup_ubuntu.sh
python3 -m pytest tests -q
```

Important: **exit your previous Mininet lab before starting this one**. This version adds a seventh host, `p1`, and rewrites the OVS pipeline. Do not copy new source files into an already-running milestone-1 switch and expect the new health paths to appear.

## 2. Start the new adaptive lab (terminal A)

```bash
sudo python3 -m topology.lab --adaptive
```

The lab prints a runtime path similar to `/tmp/sdn-lb-abcdef12`. Use the **new** runtime path in all commands below; it is not the same as the previous lab's path.

The topology includes:

- `h1/h2/h3`, client IPs `10.0.1.11-13`.
- `b1/b2/b3`, backend IPs `10.0.2.11-13`.
- `p1`, independent health probe IP `10.0.1.50`.
- VIP `10.0.3.100:8080`.
- OVS bridge `s1`, with default-drop OpenFlow table rules.

The `p1` probe sends direct `GET /health` requests to each server IP, using explicit high-priority OpenFlow forwarding in both directions. It **does not** send checks via the VIP or SELECT group, and it runs as a process separate from the policy controller. The controller reads the probe results and separately reads backend telemetry for capacity estimation.

Inside Mininet:

```text
mininet> h1 curl -s http://10.0.3.100:8080/work
mininet> p1 curl -s http://10.0.2.11:8080/health
```

## 3. Watch state (terminal B)

From the project root:

```bash
RUNTIME=/tmp/sdn-lb-REPLACE_WITH_NEW_VALUE
sudo python3 -m controller.cli health --runtime "$RUNTIME"
sudo python3 -m controller.cli status --runtime "$RUNTIME"
sudo python3 -m controller.cli show
sudo tail -f "$RUNTIME/controller.log"
```

Wait until all three backends are `HEALTHY`. Their states progress from `DOWN -> RECOVERING -> HEALTHY` with a 30-second recovery window.

Live group counters:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-group-stats s1
sudo ovs-appctl dpctl/dump-conntrack zone=10
```

You may use `sh ovs-ofctl ...` inside Mininet; do not type `sudo ovs-ofctl ...` there, because Mininet treats that as its own CLI command.

## 4. Run automated backend failure and recovery test (terminal B)

**While adaptive Mininet is running**, and from the project root:

```bash
sudo python3 -m experiments.recovery_check --runtime "$RUNTIME" --backend b1 --requests 20
```

The test:

1. Waits until `b1` is `HEALTHY`.
2. Sends SIGTERM to the lab's `b1` HTTP worker.
3. Waits for the controller to mark `b1` as `DOWN` and remove its SELECT bucket.
4. Opens 20 **new TCP connections** through the VIP and verifies that none go to `b1`.
5. Restarts the backend **inside its original Mininet namespace** using `mnexec -a`, without touching OVS conntrack state.
6. Waits for `RECOVERING`, confirms a small positive weight, then waits for `HEALTHY` and a higher weight.

The experiment intentionally kills one backend. Run only in this throwaway lab, not against production workloads.

Alternatively, drain and restore a backend without killing it:

```bash
sudo python3 -m controller.cli drain b1 --runtime "$RUNTIME"
sudo python3 -m controller.cli undrain b1 --runtime "$RUNTIME"
```

Manual worker restart in the Mininet namespace (only when the old worker is stopped):

```bash
sudo python3 -m controller.cli restart-backend b1 --runtime "$RUNTIME"
```

## 5. Verify controller-only restart reconciliation

While Mininet is still open, from terminal B:

```bash
sudo python3 -m controller.cli show > /tmp/sdn-before-controller-restart.txt
sudo kill -TERM "$(cat "$RUNTIME/controller.pid")"
```

The OVS switch continues forwarding using the last installed policy. Existing TCP connections should remain unaffected by **controller-only** termination.

From terminal B, restart the policy controller (the terminal stays occupied until Ctrl+C):

```bash
sudo python3 -u -m controller.policy_loop --runtime "$RUNTIME"
```

It will log a `reconciled installed weights=...` line and restore the saved policy state. It must **not** delete groups, flows, or conntrack entries.

From terminal C:

```bash
sudo python3 -m controller.cli status --runtime "$RUNTIME"
sudo ovs-ofctl -O OpenFlow13 dump-groups s1
sudo ovs-appctl dpctl/dump-conntrack zone=10
```

To stop the lab, Ctrl+C the manually restarted controller, then type `exit` inside the Mininet prompt. The replacement backend workers tracked in `restarted_pids.json` are also terminated by the lab teardown.

## 6. Retest the original session-persistence property

Run on a **fresh** Mininet lab, not concurrently with `--adaptive`:

```bash
sudo python3 -m topology.lab --self-test --connections 30
```

Expected: all 30 sessions retain the original backend and TCP source port across group weight changes.

## Default policy parameters

| Property | Value |
|---|---|
| Probe interval / timeout | 2.0s / 0.75s |
| Probe freshness | 5.0s |
| Consecutive failure evaluations to mark DOWN | 3 |
| Consecutive successful evaluations before reentry | 3 |
| Recovery ramp | 30 seconds (10% to 100% relative capacity factor) |
| Control loop interval | 2 seconds |
| Minimum ordinary group-update interval | 5 seconds |
| Member removal / reentry | Immediate on policy decision, bypassing the update interval |
| Group | SELECT ID 100 |
| Conntrack zone | 10 |

Weights are normalized across the available servers to approximately 300. Thus a *relative* 10% recovering capacity may appear as more than 10 absolute group-weight units. The policy never promises deterministic ratios for a small number of TCP flows.

## Limitations

- Active probes run from the dedicated Mininet probe namespace and check *direct* backend reachability. They do not validate each client-to-VIP route.
- The probe and metrics use shared JSON files **only for the emulator**. Production needs an authenticated management network with agent identity and secure transport.
- The 30-second recovery factor does not guarantee a monotonically increasing **absolute** group weight if other server loads change rapidly.
- The controller reads prior installed weights and cached policy state on restart. It does **not** reinstate an OpenFlow group after total switch loss.
- No conntrack replication between switches. Backend crashes can disrupt established sessions.
- One OVS bridge, IPv4 TCP 8080 only. A production system needs ACLs, secure management, scaling and failure-domain review.
- Counters may differ between OpenFlow and accelerated datapath; verify real traffic, logs, and conntrack rather than relying on a single counter.

## Source map

- `topology/lab.py`: Mininet topology and lifecycle, including `p1` health-probe namespace.
- `controller/pipeline.py`: VIP conntrack NAT pipeline and direct health-probe forwarding rules.
- `controller/health_probe.py`: independent active HTTP checks.
- `controller/policy.py`: probe-driven health states and gradual adaptive recovery.
- `controller/policy_loop.py`: policy controller with OVS readback reconciliation and state persistence.
- `controller/ofctl.py`: OpenFlow install/update/readback, including group parser.
- `controller/cli.py`: show/status/health/drain/restart-backend tools.
- `experiments/recovery_check.py`: live failure-recovery integration experiment.
- `experiments/session_check.py`: unchanged connection-persistence experiment.
- `tests/`: pure-Python tests.

OVS conntrack and OpenFlow reference:
- https://docs.openvswitch.org/en/stable/tutorials/ovs-conntrack/
- https://docs.openvswitch.org/en/stable/ref/ovs-actions.7/

## Milestone 5: FlowScope dashboard

See [MILESTONE5.md](MILESTONE5.md) for the offline React monitoring dashboard, FastAPI commands, real-time backend metrics, experiment controls, benchmark results and troubleshooting.
