# FlowScope development launcher (Conda)

The scripts in this update are additive. They do not replace your SDN controller,
Open vSwitch rules, Mininet topology, backend servers, or dashboard source.

## Install

From your project directory:

```bash
unzip -o ~/Downloads/sdn-dev-tools.zip -d .
chmod +x start_dev.sh scripts/cleanup_project.sh
```

Merge the optional ignore rules without overwriting an existing `.gitignore`:

```bash
cat .gitignore.sdn >> .gitignore
```

## Clean generated files safely

```bash
./scripts/cleanup_project.sh           # preview
./scripts/cleanup_project.sh --apply   # remove caches, archive static.backup
```

No source files, markdown docs, benchmark results, Mininet experiment logs,
Conda environments, or saved dashboard experiments are deleted.

## Start

```bash
conda activate sdn-ryu
./start_dev.sh
```

The launcher keeps the Mininet `mininet>` CLI interactive in the same terminal
and starts FlowScope automatically at http://127.0.0.1:8765. The current runtime
is recorded in `.dev/current_runtime`, so other terminals can run:

```bash
RUNTIME="$(cat .dev/current_runtime)"
sudo python3 -m controller.cli --runtime "$RUNTIME" status
```

If an older benchmark directory still exists in `/tmp/sdn-lb-*/bench_results`,
the launcher picks the most recently modified saved summary automatically. To
choose one explicitly:

```bash
./start_dev.sh --bench-root /tmp/sdn-lb-m7vcx_p7/bench_results
```

For a read-only dashboard:

```bash
./start_dev.sh --read-only
```

To use a different dashboard port:

```bash
./start_dev.sh --port 8766
```

## Stop

Type `exit` at the Mininet CLI. That stops the network and dashboard.
The last runtime remains available in `.dev/last_runtime`. Logs are stored in
`.dev/mininet.log` and `.dev/dashboard.log`. All experiment results in `/tmp`
remain available until the operating system cleans `/tmp`.

If the prior Mininet instance crashed and left bridge `s1`, close any other
Mininet labs first and then run `sudo mn -c` before restarting. The launcher
never runs destructive Mininet cleanup automatically.

## Troubleshooting

- `Activate your Conda environment`: `conda activate sdn-ryu`.
- `Dashboard dependencies missing`: `python -m pip install -r requirements-dashboard.txt`.
- `OVS bridge s1 already exists`: exit the old Mininet prompt; if truly stale, `sudo mn -c`.
- `Dashboard port unavailable`: stop previous dashboard with Ctrl+C or `--port 8766`.
- No dashboard after topology starts: inspect `.dev/dashboard.log`.
- Missing bench results: previous `/tmp` files may have been removed after a reboot.

The launcher uses `/usr/bin/python3` for the Mininet topology (matching your
working `sudo python3` setup), and the active Conda interpreter for the
FastAPI dashboard. This avoids requiring `mininet` in the Conda environment.
