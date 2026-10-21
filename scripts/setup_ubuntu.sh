#!/usr/bin/env bash
# Install system packages, then expose Mininet's pure-Python package to the
# project's Conda environment so every component runs on one interpreter.
# Usage (from an activated env): sudo -E bash scripts/setup_ubuntu.sh
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo 'Run: sudo -E bash scripts/setup_ubuntu.sh'; exit 1; fi
if [[ -z "${CONDA_PREFIX:-}" ]]; then echo 'Activate the env first: conda activate sdn-ryu'; exit 1; fi
apt-get update
apt-get install -y mininet openvswitch-switch iproute2 curl util-linux
systemctl enable --now openvswitch-switch || true

SITE="$("$CONDA_PREFIX/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
rsync -a --exclude __pycache__ /usr/lib/python3/dist-packages/mininet/ "$SITE/mininet/"
chown -R --reference="$SITE" "$SITE/mininet"
"$CONDA_PREFIX/bin/python" -m pip install -q -r requirements.txt
printf '\nReady. From the project root: ./start_dev.sh\n'
