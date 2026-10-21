"""Read-only switch inspection through ovs-ofctl, for operators.

The controller itself programs the switch over OpenFlow (controller/lb_app.py).
"""
from __future__ import annotations

import subprocess

from .config import BRIDGE


class OpenFlowError(RuntimeError):
    pass


def ovs(*args: str, bridge: str = BRIDGE) -> str:
    cmd = ['ovs-ofctl', '-O', 'OpenFlow13', *args, bridge]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, check=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise OpenFlowError(f'{cmd!r}: {getattr(exc, "stderr", "") or exc}') from exc
    return result.stdout


def show(bridge: str = BRIDGE) -> str:
    return ('GROUPS\n' + ovs('dump-groups', bridge=bridge)
            + '\nGROUP STATS\n' + ovs('dump-group-stats', bridge=bridge)
            + '\nFLOWS\n' + ovs('dump-flows', bridge=bridge))
