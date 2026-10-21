#!/usr/bin/env bash
# Start the Mininet topology and FlowScope from one terminal.
# Requires: conda activate sdn-ryu
set -Eeuo pipefail

usage() {
  cat <<'HELP'
Usage: ./start_dev.sh [--bench-root PATH] [--port PORT] [--read-only]

Starts the existing SDN topology in this terminal, then starts the
FlowScope dashboard automatically when the new runtime directory appears.
Leave the Mininet prompt with 'exit' to stop both services.

Options:
  --bench-root PATH  Use saved benchmark summaries from PATH.
                     By default, use the most recently modified summary.csv
                     found in /tmp/sdn-lb-*/bench_results, if available.
  --port PORT        Dashboard port on 127.0.0.1 (default: 8765).
  --read-only        Disable dashboard experiment controls.
  -h, --help         Show this help.
HELP
}

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PORT=8765
BENCH_ROOT=""
BENCH_OVERRIDE=0
ENABLE_CONTROLS=1

while (( $# )); do
  case "$1" in
    --bench-root)
      (( $# >= 2 )) || { echo 'Missing path after --bench-root' >&2; exit 2; }
      BENCH_ROOT="$2"; BENCH_OVERRIDE=1; shift 2 ;;
    --port)
      (( $# >= 2 )) || { echo 'Missing port after --port' >&2; exit 2; }
      PORT="$2"; shift 2 ;;
    --read-only) ENABLE_CONTROLS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "${CONDA_PREFIX:-}" || ! -x "${CONDA_PREFIX}/bin/python" ]]; then
  echo 'Activate your Conda environment first: conda activate sdn-ryu' >&2
  exit 1
fi
CONDA_PY="${CONDA_PREFIX}/bin/python"

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( 10#$PORT < 1024 || 10#$PORT > 65535 )); then
  echo 'Port must be an integer between 1024 and 65535.' >&2
  exit 2
fi
if [[ ! -f topology/lab.py || ! -f monitoring/server.py ]]; then
  echo 'Run this script from the SDN project (not a different project).' >&2
  exit 1
fi
if ! "$CONDA_PY" -c 'import fastapi, uvicorn' 2>/dev/null; then
  echo 'Dashboard dependencies missing in Conda. Run:' >&2
  echo '  python -m pip install -r requirements.txt' >&2
  exit 1
fi
if ! command -v ovs-vsctl >/dev/null || ! command -v ovs-ofctl >/dev/null; then
  echo 'Open vSwitch tools not found. Run the project setup_ubuntu.sh.' >&2
  exit 1
fi

# Authenticate while still at an interactive prompt; background jobs never
# attempt an unexpected password prompt.
sudo -v
if ! sudo "$CONDA_PY" -c 'import mininet' 2>/dev/null; then
  echo 'sdn-ryu Python cannot import Mininet. Run: sudo bash scripts/setup_ubuntu.sh' >&2
  exit 1
fi
if sudo ovs-vsctl br-exists s1; then
  echo 'OVS bridge s1 already exists. Exit the previous Mininet lab first.' >&2
  echo 'If it crashed, and no other Mininet lab is running: sudo mn -c' >&2
  exit 1
fi
if ! "$CONDA_PY" - "$PORT" <<'PY'
import socket
import sys
s = socket.socket()
try:
    s.bind(('127.0.0.1', int(sys.argv[1])))
except OSError as exc:
    print('Dashboard port unavailable: {}'.format(exc), file=sys.stderr)
    sys.exit(1)
finally:
    s.close()
PY
then
  echo 'Stop the previous dashboard (Ctrl+C) or pass --port 8766.' >&2
  exit 1
fi

if (( BENCH_OVERRIDE )) && ! sudo test -d "$BENCH_ROOT"; then
  echo "Benchmark directory doesn't exist: $BENCH_ROOT" >&2
  exit 1
fi
if (( ! BENCH_OVERRIDE )); then
  # Only read historic benchmark results; never modify them.
  BENCH_ROOT="$(sudo "$CONDA_PY" - <<'PY'
from pathlib import Path
candidates = []
for results in Path('/tmp').glob('sdn-lb-*/bench_results'):
    if not results.is_dir():
        continue
    for summary in results.glob('*/summary.csv'):
        try:
            candidates.append((summary.stat().st_mtime, str(results)))
        except OSError:
            pass
print(max(candidates)[1] if candidates else '')
PY
)"
fi

mkdir -p .dev
LAUNCH_LOG="$ROOT/.dev/mininet.log"
RUNTIME_FILE="$ROOT/.dev/current_runtime"
DASH_PID_FILE="$ROOT/.dev/dashboard.pid"
LAST_RUNTIME_FILE="$ROOT/.dev/last_runtime"
rm -f "$RUNTIME_FILE" "$DASH_PID_FILE"
: > "$LAUNCH_LOG"
watcher_pid=''

stop_watcher() {
  # Proactively stop the exact dashboard daemon, rather than relying solely
  # on the background watcher being interrupted out of its wait builtin.
  if [[ -f "$DASH_PID_FILE" ]]; then
    local dashboard_to_stop='' cmdline=''
    dashboard_to_stop="$(cat "$DASH_PID_FILE" 2>/dev/null || true)"
    if [[ "$dashboard_to_stop" =~ ^[0-9]+$ && -r "/proc/$dashboard_to_stop/cmdline" ]]; then
      cmdline="$(tr '\0' ' ' < "/proc/$dashboard_to_stop/cmdline")"
      if [[ "$cmdline" == *'monitoring.server'* ]]; then
        sudo -n kill -TERM "$dashboard_to_stop" 2>/dev/null || true
      fi
    fi
  fi
  if [[ -n "$watcher_pid" ]]; then
    kill -TERM "$watcher_pid" 2>/dev/null || true
    wait "$watcher_pid" 2>/dev/null || true
  fi
  if [[ -f "$RUNTIME_FILE" ]]; then
    cp "$RUNTIME_FILE" "$LAST_RUNTIME_FILE"
    rm -f "$RUNTIME_FILE"
  fi
}
trap stop_watcher EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

watch_runtime() (
  # This subprocess alone owns the dashboard process. Its EXIT trap shuts
  # down the exact PID that it started, not other dashboard sessions.
  local runtime='' dashboard_pid='' sudo_pid='' tries=0
  stop_dashboard() {
    if [[ -n "$dashboard_pid" && "$dashboard_pid" =~ ^[0-9]+$ ]]; then
      # Avoid accidentally signalling a recycled PID. The daemon command line
      # must still identify this runtime and our monitoring module.
      if [[ -r "/proc/$dashboard_pid/cmdline" ]]; then
        local cmdline
        cmdline="$(tr '\0' ' ' < "/proc/$dashboard_pid/cmdline")"
        if [[ "$cmdline" == *'monitoring.server'* && "$cmdline" == *"$runtime"* ]]; then
          sudo -n kill -TERM "$dashboard_pid" 2>/dev/null || true
        fi
      fi
    fi
    if [[ -n "$sudo_pid" ]]; then
      kill -TERM "$sudo_pid" 2>/dev/null || true
      wait "$sudo_pid" 2>/dev/null || true
    fi
    rm -f "$DASH_PID_FILE"
  }
  trap stop_dashboard EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT

  for (( tries=0; tries<300; tries++ )); do
    runtime="$(sed -n 's@^VIP: .* | Logs/metrics: \(/tmp/sdn-lb-[^[:space:]]*\).*@\1@p' "$LAUNCH_LOG" | tail -n 1)"
    [[ -n "$runtime" ]] && break
    sleep 0.2
  done
  if [[ -z "$runtime" ]]; then
    echo '[FlowScope] Could not detect an active runtime. Check .dev/mininet.log.' >&2
    exit 1
  fi
  printf '%s\n' "$runtime" > "$RUNTIME_FILE"
  printf '%s\n' "$runtime" > "$LAST_RUNTIME_FILE"
  local dashboard_log="$ROOT/.dev/dashboard.log"
  : > "$dashboard_log"
  local -a args=("$CONDA_PY" -m monitoring.server --runtime "$runtime" --port "$PORT")
  (( ENABLE_CONTROLS )) && args+=(--allow-controls)
  [[ -n "$BENCH_ROOT" ]] && args+=(--bench-root "$BENCH_ROOT")

  # Write the actual daemon PID from inside the sudo process. The root
  # shell execs Python, so PID and process command line are trustworthy.
  sudo /bin/bash -c 'echo "$$" > "$1"; chmod 644 "$1"; shift; exec "$@"' \
    bash "$DASH_PID_FILE" "${args[@]}" > "$dashboard_log" 2>&1 &
  sudo_pid=$!

  for (( tries=0; tries<100; tries++ )); do
    if [[ -f "$DASH_PID_FILE" ]]; then
      dashboard_pid="$(cat "$DASH_PID_FILE" 2>/dev/null || true)"
    fi
    if [[ -n "$dashboard_pid" ]] && curl --silent --fail --max-time 1 \
         "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
      echo "[FlowScope] Dashboard HTTP ready; waiting for SDN controller status..."
      echo "[FlowScope] Logs: .dev/mininet.log and .dev/dashboard.log"
      [[ -n "$BENCH_ROOT" ]] && echo "[FlowScope] Benchmarks: $BENCH_ROOT"
      break
    fi
    if ! kill -0 "$sudo_pid" 2>/dev/null; then
      echo '[FlowScope] Dashboard failed to start. See .dev/dashboard.log.' >&2
      exit 1
    fi
    sleep 0.2
  done
  if (( tries >= 100 )); then
    echo '[FlowScope] Dashboard did not become ready. See .dev/dashboard.log.' >&2
    exit 1
  fi
  # A responsive web page is not enough to mark the SDN system ready.
  # The policy daemon starts asynchronously after Mininet's health probe,
  # and may terminate before it ever writes status.json.
  local ready_controller=0
  for (( tries=0; tries<75; tries++ )); do
    if sudo -n "$CONDA_PY" - "$runtime" <<'CONTROLLER_READY'
import json
import os
from pathlib import Path
import sys
import time

runtime = Path(sys.argv[1])
try:
    state = json.loads((runtime / 'status.json').read_text())
    pid = int(state['controller_pid'])
    age = time.time() - float(state['timestamp'])
    if pid < 2 or not (0 <= age < 10):
        raise ValueError('not a fresh controller status')
    stat = Path('/proc/{}/stat'.format(pid)).read_text().rsplit(') ', 1)[1]
    if stat.startswith('Z '):
        raise ValueError('controller is a zombie')
    cmdline = Path('/proc/{}/cmdline'.format(pid)).read_bytes()
    if b'controller.lb_app' not in cmdline:
        raise ValueError('status PID is not the policy controller')
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
    sys.exit(1)
CONTROLLER_READY
    then
      ready_controller=1
      break
    fi
    sleep 0.2
  done
  if (( ! ready_controller )); then
    echo "[FlowScope] Policy controller never reported status. See $runtime/controller.log." >&2
    exit 1
  fi
  echo
  echo '[FlowScope] SDN network + dashboard ready'
  echo "[FlowScope] Dashboard: http://127.0.0.1:$PORT/"
  echo "[FlowScope] Runtime:   $runtime"
  echo '[FlowScope] Type exit at the mininet> prompt to shut down both.'
  wait "$sudo_pid" || true
)

watch_runtime &
watcher_pid=$!
echo "[FlowScope] Starting Mininet. Python: $CONDA_PY"
echo '[FlowScope] No separate dashboard terminal or manual runtime path needed.'

# Keep sys.stdin attached to the current terminal: Mininet CLI stays usable.
# Force unbuffered Python output so the background watcher gets the runtime.
sudo env PYTHONUNBUFFERED=1 "$CONDA_PY" -u -m topology.lab 2>&1 | tee "$LAUNCH_LOG"
