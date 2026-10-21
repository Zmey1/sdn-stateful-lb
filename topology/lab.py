"""One-bridge Mininet lab. Run as root: sudo python3 -m topology.lab.

No controller participates in packet forwarding. Controller-style OpenFlow
messages are pushed by ovs-ofctl; OVS performs selection, tracking and NAT.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from controller.config import (ALL, BACKENDS, CLIENTS, PROBE, BRIDGE, GATEWAY_MAC,
                               CLIENT_GW, BACKEND_GW, VIP)
from controller.ofctl import install, ports_from_switch, update_weights

ROOT = Path(__file__).resolve().parent.parent


def wait_for(path: Path, timeout: float, process=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists(): return
        if process is not None and process.poll() is not None:
            raise RuntimeError(f'Experiment ended unexpectedly, code={process.returncode}')
        time.sleep(.1)
    raise TimeoutError(f'Timed out waiting for {path}')


def main():
    if os.geteuid():
        raise SystemExit('Run as root: sudo python3 -m topology.lab')
    # Mininet is imported only here, so pure Python unit tests run elsewhere.
    try:
        from mininet.net import Mininet
        from mininet.topo import Topo
        from mininet.node import OVSSwitch
        from mininet.cli import CLI
        from mininet.log import setLogLevel
    except ImportError as exc:
        raise SystemExit('Mininet missing. See README: apt install mininet') from exc

    p = argparse.ArgumentParser()
    p.add_argument('--self-test', action='store_true', help='automated persistent-session test')
    p.add_argument('--adaptive', action='store_true', help='start adaptive policy loop')
    p.add_argument('--connections', type=int, default=30)
    args = p.parse_args()
    if args.adaptive and args.self_test:
        p.error('Use --self-test separately from --adaptive for deterministic test')

    class LBTopo(Topo):
        def build(self):
            s = self.addSwitch(BRIDGE, protocols='OpenFlow13', failMode='secure')
            for e in ALL:
                subnet = '24'
                h = self.addHost(e.name, ip=e.ip + '/' + subnet, mac=e.mac)
                self.addLink(h, s)

    setLogLevel('info')
    runtime = Path(tempfile.mkdtemp(prefix='sdn-lb-'))
    net = Mininet(topo=LBTopo(), switch=OVSSwitch, controller=None,
                  autoSetMacs=False, autoStaticArp=False)
    procs = []
    try:
        net.start()
        # Static ARP is intentional: this OVS bridge is a strict
        # OpenFlow-programmed IPv4 gateway, with no NORMAL/ARP responder.
        for e in (*CLIENTS, PROBE):
            h = net.get(e.name)
            dev = h.defaultIntf().name
            h.cmd(f'ip neigh replace {CLIENT_GW} lladdr {GATEWAY_MAC} dev {dev} nud permanent')
            h.cmd(f'ip route replace default via {CLIENT_GW} dev {dev}')
        for e in BACKENDS:
            h = net.get(e.name)
            dev = h.defaultIntf().name
            h.cmd(f'ip neigh replace {BACKEND_GW} lladdr {GATEWAY_MAC} dev {dev} nud permanent')
            h.cmd(f'ip route replace default via {BACKEND_GW} dev {dev}')

        # Allows lab-only restarts of failed servers in the same Mininet
        # network namespace, even when the server worker itself has exited.
        (runtime / 'host_pids.json').write_text(json.dumps(
            {e.name: net.get(e.name).pid for e in ALL}))
        ports = ports_from_switch(BRIDGE)
        print('Actual OpenFlow ports:', ports)
        # Bootstrap fail closed before starting any server; group weights will
        # be made live immediately after all three healthy servers start.
        install(ports, BRIDGE)
        update_weights({}, BRIDGE)

        env = os.environ.copy()
        env['PYTHONPATH'] = str(ROOT)
        for e in BACKENDS:
            p_backend = net.get(e.name).popen(
                [sys.executable, '-u', '-m', 'backend.server', '--name', e.name,
                 '--runtime', str(runtime)], env=env,
                stdout=(runtime / f'{e.name}.log').open('w'),
                stderr=subprocess.STDOUT)
            procs.append(p_backend)
        for e in BACKENDS:
            wait_for(runtime / f'{e.name}.json', 12, procs[BACKENDS.index(e)])

        if args.adaptive:
            # Probe worker is independent of the policy/controller process.
            # p1 can address each backend directly; production client h1/h2/h3
            # cannot bypass the VIP or the SELECT group.
            p_probe = net.get(PROBE.name).popen(
                [sys.executable, '-u', '-m', 'controller.health_probe',
                 '--runtime', str(runtime)], env=env,
                stdout=(runtime / 'probe.log').open('w'),
                stderr=subprocess.STDOUT)
            procs.append(p_probe)
            wait_for(runtime / 'health.json', 12, p_probe)
            # Start the actual policy daemon, which gradually enables healthy
            # members. No Packet-In or per-connection controller processing.
            p_controller = subprocess.Popen(
                [sys.executable, '-u', '-m', 'controller.policy_loop',
                 '--runtime', str(runtime)], env=env,
                stdout=(runtime / 'controller.log').open('w'),
                stderr=subprocess.STDOUT)
            procs.append(p_controller)
            print('Adaptive policy loop started')
        else:
            update_weights({'b1': 100, 'b2': 100, 'b3': 100}, BRIDGE)

        print(f'VIP: http://{VIP}:8080/ | Logs/metrics: {runtime}')
        if args.self_test:
            ready, go, result = (runtime / 'ready.json', runtime / 'go.flag',
                                  runtime / 'result.json')
            cmd = [sys.executable, '-u', '-m', 'experiments.session_check',
                   '--connections', str(args.connections), '--ready', str(ready),
                   '--go', str(go), '--result', str(result)]
            worker = net.get('h1').popen(cmd, env=env,
                stdout=(runtime / 'experiment.log').open('w'),
                stderr=subprocess.STDOUT)
            procs.append(worker)
            wait_for(ready, 30, worker)
            print('Live TCP sessions before weight change:', ready.read_text())
            update_weights({'b1': 1, 'b2': 150, 'b3': 1}, BRIDGE)
            go.touch()
            worker.wait(timeout=30)
            if result.exists():
                print('PERSISTENCE RESULT:', result.read_text())
            if worker.returncode:
                raise RuntimeError(f'Persistence test failed: see {runtime / "experiment.log"}')
            print('PASS: session identity and source ports stable across group MOD')
        else:
            print('Try: h1 curl -s http://10.0.3.100:8080/work')
            print('In another terminal, run:')
            print('sudo python3 -m controller.cli --runtime ' + str(runtime) + ' mode equal')
            print('Type exit to shut down this Mininet lab.')
            CLI(net)
    finally:
        # CLI-started replacement backend workers are recorded separately.
        registry = runtime / 'restarted_pids.json'
        try:
            for pid in json.loads(registry.read_text()).values():
                try:
                    os.kill(int(pid), 15)
                except (ProcessLookupError, PermissionError, ValueError, TypeError):
                    pass
        except (OSError, ValueError):
            pass
        for process in reversed(procs):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: process.kill()
        net.stop()
        print('Lab finished. Diagnostics preserved at', runtime)


if __name__ == '__main__':
    main()
