# FlowScope: How to use the dashboard (Milestone 5 UX update)

This is a **frontend-only update** to your existing working Milestone 5 project. It does not alter `controller/`, `topology/`, the OpenFlow pipeline, conntrack, NAT, or benchmarks.

## Install into your existing project

1. Keep a backup of the existing frontend files, or simply copy the old frontend folder to a safe location.
2. From the root of your **Milestone 5** `sdn-stateful-lb` project, extract the hotfix archive so its `monitoring/static/` files overwrite the existing frontend:

```bash
cd ~/Documents/SRM/sem7/sdn/sdn-stateful-lb-milestone5/sdn-stateful-lb
cp -a monitoring/static monitoring/static.before-ux-fix
unzip -o ~/Downloads/flowscope-dashboard-ux-fix.zip -d .
```

3. Hard refresh the dashboard browser: Ctrl+Shift+R. No Mininet or OVS restart is required. `index.html` now references cache-busted assets.

## Starting dashboard using your Conda environment

Terminal A, in the project directory:

```bash
conda activate sdn-ryu
sudo python3 -m topology.lab --adaptive
```

Terminal B:

```bash
conda activate sdn-ryu
RUNTIME=/tmp/sdn-lb-REPLACE_WITH_CURRENT_RUNTIME
sudo "$(which python)" -m monitoring.server --runtime "$RUNTIME" --allow-controls --bench-root /tmp/sdn-lb-m7vcx_p7/bench_results
```

Open http://127.0.0.1:8765/ on the same Ubuntu laptop.

## First guided demo

1. **Run baseline traffic**: Sends 40 requests/second for 15 seconds from Mininet client h1. Expect the RPS chart to rise. The completed-request distribution should show b1/b2/b3 serving requests.
2. **Add 160 ms to b1**: The server remains healthy, but `/work` operations on b1 become slow. The controller only learns about the slowdown when new requests arrive.
3. **Run degraded traffic**: Sends 40 requests/second for 30 seconds. Watch **installed weights** change as the adaptive controller responds, and watch **completed request share** change (not necessarily in exact proportion to weights).
4. **Reset the lab**: Removes the injected workload, clears drains, and returns to adaptive mode. If you previously crashed a worker, use **Restart** in advanced controls. Reset does **not** restart crashed processes.

## What the dashboard sections mean

- **Backend monitoring**: CPU%, health state, `/health` probe round-trip time, server-side processing delay, cumulative completed requests, and installed weight.
- **Traffic rate (RPS)**: HTTP requests completed per second, calculated between dashboard polls. **0 RPS is normal when idle**.
- **Backend traffic distribution**: Share of completed HTTP requests during a short sampling window. It is *not* an exact new-connection distribution.
- **Installed / desired weights**: Whether the group state matches controller policy. Weights apply to **new TCP connections**; existing sessions retain conntrack assignments.
- **Benchmark comparison**: Historic, saved experiments; not live P95 latency. P95 means 95% of sampled request times were at or below that value.
- **Controller event log**: Expand an event to view the full, raw log entry. The existing log lines do not include timestamps.

## Advanced controls

Click **Show advanced controls** under Experiment Workbench to reveal:

- **Adaptive**: Controller computes server weights dynamically from observed load.
- **Equal**: All healthy backends get the same weight (benchmark baseline).
- **Capacity**: A preset unequal static-weight baseline.
- **Drain / Undrain**: Stops/starts sending *new* connections to a backend. Existing healthy sessions continue.
- **Crash / Restart**: Stops or restarts the backend HTTP worker. Existing connections to a crashed worker can fail.
- **Delay (ms) / CPU Burn (ms)**: Per-request synthetic load applied to `/work`, not `/health`.
- **Start traffic test**: Generates traffic at your chosen request rate and duration, within the API limits.

Do not change controls during a benchmark. Dashboard controls are disabled during registered benchmark runs.

## Verification

```bash
node --check monitoring/static/app.js # optional, if Node installed
python3 -m pytest tests -q
```
