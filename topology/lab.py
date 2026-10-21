"""One-bridge Mininet lab. Run as root: sudo $CONDA_PREFIX/bin/python -m topology.lab

The Ryu controller (controller/lb_app.py) programs the switch over OpenFlow
1.3 but never handles data packets: OVS performs selection, tracking and NAT.
"""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from controller.benchmark_modes import STATIC_CAPACITY
from controller.config import (ALL, BACKENDS, PROBE, BRIDGE, GATEWAY_MAC,
                               CLIENT_GW, BACKEND_GW, VIP, VPORT)
from controller.lb_app import CONTROLLER_PORT
from controller.runtime import atomic_json, read_dict

ROOT = Path(__file__).resolve().parent.parent


def wait_until(check, timeout: float, what: str, process=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        if process is not None and process.poll() is not None:
            raise RuntimeError(f'Process ended unexpectedly while waiting for {what}, '
                               f'code={process.returncode}')
        time.sleep(.2)
    raise TimeoutError(f'Timed out waiting for {what}')


def port_listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def refuse_if_lab_running() -> None:
    """A second lab must not start beside a live one. Mininet deletes
    same-named veths before creating its own (removing the live lab's switch
    ports), and Ryu listens with SO_REUSEPORT, so a second controller would
    silently share port 6653 and could take over the switch."""
    bridge = subprocess.run(['ovs-vsctl', 'br-exists', BRIDGE]).returncode == 0
    if bridge or port_listening(CONTROLLER_PORT):
        raise SystemExit(f'Another lab is running (OVS bridge {BRIDGE} or port '
                         f'{CONTROLLER_PORT} in use). Type exit at its mininet> '
                         'prompt first. If it crashed: sudo mn -c')


def installed_weights(runtime: Path) -> dict:
    status = read_dict(runtime / 'status.json')
    if time.time() - status.get('timestamp', 0) >= 8:
        return {}
    return status.get('installed_weights') or {}


def self_test(net, runtime: Path, env: dict, connections: int, controller) -> None:
    """Open TCP sessions, change SELECT weights through the controller, and
    verify every session keeps its backend and source port."""
    equal = {b.name: 100 for b in BACKENDS}
    atomic_json(runtime / 'benchmark_mode.json', {'mode': 'equal'})
    wait_until(lambda: installed_weights(runtime) == equal, 90,
               'all backends admitted with equal weights', controller)
    ready, go, result = runtime / 'ready.json', runtime / 'go.flag', runtime / 'result.json'
    cmd = [sys.executable, '-u', '-m', 'experiments.session_check',
           '--connections', str(connections), '--ready', str(ready),
           '--go', str(go), '--result', str(result)]
    with (runtime / 'experiment.log').open('w') as log:
        worker = net.get('h1').popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_until(ready.exists, 30, 'test sessions', worker)
        print('Live TCP sessions before weight change:', ready.read_text())
        atomic_json(runtime / 'benchmark_mode.json', {'mode': 'capacity'})
        wait_until(lambda: installed_weights(runtime) == STATIC_CAPACITY, 20,
                   'controller to install new weights', controller)
        go.touch()
        worker.wait(timeout=30)
    finally:
        if worker.poll() is None:
            worker.kill()
        atomic_json(runtime / 'benchmark_mode.json', {'mode': 'adaptive'})
    if result.exists():
        print('PERSISTENCE RESULT:', result.read_text())
    if worker.returncode:
        raise RuntimeError(f'Persistence test failed: see {runtime / "experiment.log"}')
    print('PASS: session identity and source ports stable across group MOD')


def main():
    if os.geteuid():
        raise SystemExit('Run as root: sudo "$CONDA_PREFIX/bin/python" -m topology.lab')
    # Mininet is imported only here, so pure Python unit tests run elsewhere.
    try:
        from mininet.net import Mininet
        from mininet.topo import Topo
        from mininet.node import OVSSwitch, RemoteController
        from mininet.cli import CLI
        from mininet.log import setLogLevel
    except ImportError as exc:
        raise SystemExit('Mininet missing. Run: sudo -E bash scripts/setup_ubuntu.sh') from exc

    p = argparse.ArgumentParser()
    p.add_argument('--self-test', action='store_true',
                   help='automated session-persistence test, then exit')
    p.add_argument('--connections', type=int, default=30)
    args = p.parse_args()
    refuse_if_lab_running()

    class LBTopo(Topo):
        def build(self):
            s = self.addSwitch(BRIDGE, protocols='OpenFlow13', failMode='secure')
            for e in ALL:
                h = self.addHost(e.name, ip=f'{e.ip}/24', mac=e.mac)
                self.addLink(h, s)

    setLogLevel('info')
    runtime = Path(tempfile.mkdtemp(prefix='sdn-lb-'))
    env = os.environ.copy()
    env['PYTHONPATH'] = str(ROOT)
    procs = []

    def start(cmd, log_name, host=None):
        log = (runtime / log_name).open('w')
        if host is not None:
            process = host.popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        else:
            # Own session, no terminal: otherwise the process shares the
            # Mininet CLI's foreground job and terminal job control can
            # stop it (state T), freezing the controller.
            process = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        log.close()
        procs.append(process)
        return process

    net = None
    try:
        # The controller listens before the switch exists; the switch connects
        # to it during net.start() and receives the pipeline over OpenFlow.
        controller = start([sys.executable, '-u', '-m', 'controller.lb_app',
                            '--runtime', str(runtime), '--port', str(CONTROLLER_PORT)],
                           'controller.log')
        wait_until(lambda: port_listening(CONTROLLER_PORT), 15,
                   'Ryu controller to listen', controller)
        net = Mininet(topo=LBTopo(), switch=OVSSwitch,
                      controller=RemoteController('c0', ip='127.0.0.1', port=CONTROLLER_PORT),
                      autoSetMacs=False, autoStaticArp=False)
        net.start()
        # Static ARP is intentional: this OVS bridge is a strict
        # OpenFlow-programmed IPv4 gateway, with no NORMAL/ARP responder.
        for e in ALL:
            h = net.get(e.name)
            dev = h.defaultIntf().name
            gateway = BACKEND_GW if e in BACKENDS else CLIENT_GW
            h.cmd(f'ip neigh replace {gateway} lladdr {GATEWAY_MAC} dev {dev} nud permanent')
            h.cmd(f'ip route replace default via {gateway} dev {dev}')

        # Allows lab-only restarts of failed servers in the same Mininet
        # network namespace, even when the server worker itself has exited.
        atomic_json(runtime / 'host_pids.json', {e.name: net.get(e.name).pid for e in ALL})

        backends = [start([sys.executable, '-u', '-m', 'backend.server', '--name', e.name,
                           '--runtime', str(runtime)], f'{e.name}.log', net.get(e.name))
                    for e in BACKENDS]
        for e, process in zip(BACKENDS, backends):
            wait_until((runtime / f'{e.name}.json').exists, 12, f'{e.name} telemetry', process)

        # The probe runs in its own namespace, independent of the controller.
        # p1 can address each backend directly; clients h1/h2/h3 cannot
        # bypass the VIP or the SELECT group.
        probe = start([sys.executable, '-u', '-m', 'controller.health_probe',
                       '--runtime', str(runtime)], 'probe.log', net.get(PROBE.name))
        wait_until((runtime / 'health.json').exists, 12, 'health probe', probe)
        wait_until(lambda: read_dict(runtime / 'status.json').get('switch_connected'), 20,
                   'switch to connect to the Ryu controller', controller)

        print(f'VIP: http://{VIP}:{VPORT}/ | Logs/metrics: {runtime}')
        if args.self_test:
            self_test(net, runtime, env, args.connections, controller)
        else:
            print(f'Try: h1 curl -s http://{VIP}:{VPORT}/work')
            print(f'Status: sudo {sys.executable} -m controller.cli status --runtime {runtime}')
            print('Type exit to shut down this Mininet lab.')
            CLI(net)
    finally:
        # CLI-started replacement backend workers are recorded separately.
        for pid in read_dict(runtime / 'restarted_pids.json').values():
            try:
                os.kill(int(pid), 15)
            except (ProcessLookupError, PermissionError, ValueError, TypeError):
                pass
        for process in reversed(procs):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        if net is not None:
            net.stop()
        print('Lab finished. Diagnostics preserved at', runtime)


if __name__ == '__main__':
    main()
