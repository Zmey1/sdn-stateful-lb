"""Ryu OpenFlow 1.3 controller application for the stateful load balancer.

The controller never sees data packets (no Packet-In). It:
  1. on switch connect, maps host ports and reconciles the SELECT group:
     a fresh switch gets the pipeline installed; an existing group is
     adopted as-is, so a controller restart never disturbs live connections;
  2. every cycle, polls group counters, runs the policy step and sends a
     GroupMod when the weights should change, then reads the group back
     so the switch stays the source of truth.

Run:  sudo $CONDA_PREFIX/bin/python -m controller.lb_app --runtime /tmp/sdn-lb-...
"""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import sys
import time
import traceback

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

from .config import BACKENDS
from .openflow import (flow_mods, group_counters, group_mod, ports_from_desc,
                       weights_from_group_desc)
from .policy_loop import PolicyLoop
from .runtime import atomic_json, read_dict

RUNTIME_ENV = 'SDN_LB_RUNTIME'
INTERVAL = 2.0
CONTROLLER_PORT = 6653


class LoadBalancer(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.runtime = Path(os.environ[RUNTIME_ENV])
        self.runtime.mkdir(parents=True, exist_ok=True)
        # Prevent two control loops from fighting over SELECT weights.
        self._lock = (self.runtime / 'controller.lock').open('w')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another controller is already managing this runtime')
        (self.runtime / 'controller.pid').write_text(str(os.getpid()))
        self.datapath = None
        self.ports: dict[str, int] = {}
        self.loop: PolicyLoop | None = None
        self.installed = {b.name: 0 for b in BACKENDS}
        self.counters = None
        hub.spawn(self._control_loop)

    # Switch connection and reconciliation ---------------------------------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features(self, ev):
        datapath = ev.msg.datapath
        print(f'Switch {datapath.id:#x} connected', flush=True)
        self.datapath = datapath
        datapath.send_msg(datapath.ofproto_parser.OFPPortDescStatsRequest(datapath, 0))

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc(self, ev):
        datapath = ev.msg.datapath
        try:
            self.ports = ports_from_desc(ev.msg.body)
        except ValueError as exc:
            print(f'Cannot map switch ports: {exc}', flush=True)
            return
        print(f'Actual OpenFlow ports: {self.ports}', flush=True)
        self._request_group_desc(datapath)

    def _request_group_desc(self, datapath):
        datapath.send_msg(datapath.ofproto_parser.OFPGroupDescStatsRequest(datapath, 0))

    @set_ev_cls(ofp_event.EventOFPGroupDescStatsReply, MAIN_DISPATCHER)
    def _group_desc(self, ev):
        datapath = ev.msg.datapath
        try:
            weights = weights_from_group_desc(ev.msg.body)
        except ValueError as exc:
            print(f'Refusing to manage unexpected group: {exc}', flush=True)
            return
        if weights is None:
            # Fresh switch: install the pipeline, new connections fail-closed
            # until the policy admits healthy backends. The group goes first:
            # OVS rejects a flow whose group does not exist (BAD_OUT_GROUP).
            ofproto = datapath.ofproto
            datapath.send_msg(group_mod(datapath, {}, ofproto.OFPGC_ADD))
            for message in flow_mods(datapath, self.ports):
                datapath.send_msg(message)
            weights = {b.name: 0 for b in BACKENDS}
            print('Installed pipeline on fresh switch', flush=True)
        self.installed = weights
        if self.loop is None:
            print(f'Controller PID {os.getpid()}, reconciled installed weights={weights}',
                  flush=True)
            self.loop = PolicyLoop(self.runtime, weights)

    @set_ev_cls(ofp_event.EventOFPGroupStatsReply, MAIN_DISPATCHER)
    def _group_stats(self, ev):
        self.counters = group_counters(ev.msg.body)

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def _error(self, ev):
        msg = ev.msg
        print(f'OpenFlow error type={msg.type} code={msg.code}; re-reading group',
              flush=True)
        self._request_group_desc(msg.datapath)

    @set_ev_cls(ofp_event.EventOFPStateChange, DEAD_DISPATCHER)
    def _disconnected(self, ev):
        if ev.datapath is self.datapath:
            print('Switch disconnected; it keeps forwarding with the last weights',
                  flush=True)
            self.datapath = None

    # Control loop ----------------------------------------------------------

    def _control_loop(self):
        # hub.spawn logs an uncaught exception once and ends the thread, which
        # would leave a live process with a frozen status.json. Keep cycling
        # and publish the error instead.
        last_error = ''
        while True:
            hub.sleep(INTERVAL)
            if self.loop is None:
                continue
            try:
                self._control_step()
                last_error = ''
            except Exception:
                error = traceback.format_exc()
                if error != last_error:
                    print(f'Control step failed:\n{error}', flush=True)
                last_error = error
                self._publish_error(error.strip().splitlines()[-1])

    def _publish_error(self, message: str):
        try:
            status = read_dict(self.runtime / 'status.json')
            status.update({'timestamp': time.time(), 'controller_pid': os.getpid(),
                           'loop_error': message})
            atomic_json(self.runtime / 'status.json', status)
        except OSError:
            pass

    def _control_step(self):
        datapath = self.datapath
        if datapath is not None:
            datapath.send_msg(datapath.ofproto_parser.OFPGroupStatsRequest(
                datapath, 0, datapath.ofproto.OFPG_ALL))
        desired = self.loop.step(self.installed, self.counters,
                                 connected=datapath is not None)
        if desired is not None and datapath is not None:
            # Group MOD rewrites buckets only: flows and conntrack entries
            # are untouched. Read the group back to confirm.
            datapath.send_msg(group_mod(datapath, desired))
            self.installed = dict(desired)
            self._request_group_desc(datapath)


def main():
    p = argparse.ArgumentParser(description='Run the Ryu load-balancer controller')
    p.add_argument('--runtime', required=True, type=Path)
    p.add_argument('--port', type=int, default=CONTROLLER_PORT)
    args = p.parse_args()
    os.environ[RUNTIME_ENV] = str(args.runtime.resolve())
    from ryu.cmd import manager
    sys.argv = ['ryu-manager', '--ofp-listen-host', '127.0.0.1',
                '--ofp-tcp-listen-port', str(args.port), 'controller.lb_app']
    manager.main()


if __name__ == '__main__':
    main()
