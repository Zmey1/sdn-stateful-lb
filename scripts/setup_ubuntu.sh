#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo 'Run: sudo bash scripts/setup_ubuntu.sh'; exit 1; fi
apt-get update
apt-get install -y mininet openvswitch-switch iproute2 curl python3 python3-pip python3-pytest
systemctl enable --now openvswitch-switch || true
printf '\nReady. Run from project root: sudo python3 -m topology.lab --self-test\n'
