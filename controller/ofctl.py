"""OpenFlow rule programming through ovs-ofctl (not controller Packet-Ins).

The data plane remains fully self-contained. `ovs-ofctl` is an OpenFlow
control-plane client and can be replaced by OS-Ken message transport later.
"""
from __future__ import annotations
import subprocess
import re
from .config import ALL, BRIDGE, BACKENDS, GROUP_ID
from .pipeline import group_text, flow_lines


class OpenFlowError(RuntimeError):
    pass


def ovs(*args: str, bridge: str = BRIDGE, input_text: str | None = None) -> str:
    cmd = ['ovs-ofctl', '-O', 'OpenFlow13', *args]
    try:
        result = subprocess.run(cmd, input=input_text, text=True,
                                capture_output=True, check=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        stderr = getattr(exc, 'stderr', '')
        raise OpenFlowError(f'{cmd!r}: {stderr or exc}') from exc
    return result.stdout


def install(ports: dict[str, int], bridge: str = BRIDGE) -> None:
    # Explicitly NOT using NORMAL or standalone fail-mode fallback.
    try:
        subprocess.run(['ovs-vsctl', 'set-fail-mode', bridge, 'secure'],
                       check=True, capture_output=True, text=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise OpenFlowError(f'set-fail-mode: {getattr(exc, "stderr", "") or exc}') from exc
    # Clean only this lab bridge, never global conntrack state. The group
    # starts fail-closed; the caller enables backends once they are serving.
    ovs('del-flows', bridge)
    ovs('del-groups', bridge)
    ovs('add-group', bridge, group_text({}))
    ovs('add-flows', bridge, '-', input_text='\n'.join(flow_lines(ports)) + '\n')
    check_installed(bridge)


def update_weights(weights: dict[str, int], bridge: str = BRIDGE) -> None:
    # Group MOD overwrites buckets without deleting references from flows or
    # deleting conntrack mappings. Existing CT state is untouched.
    ovs('mod-group', bridge, group_text(weights))
    check_installed(bridge, expected=weights)


def parse_installed_weights(output: str) -> dict[str, int]:
    """Interpret `ovs-ofctl dump-groups`, including the fail-closed bucket.

    Fail rather than guess if an unknown group structure was installed.
    """
    group = next((line for line in output.splitlines()
                  if re.search(rf'\bgroup_id={GROUP_ID}(?:,|\s|$)', line)), None)
    if group is None:
        raise OpenFlowError(f'SELECT group {GROUP_ID} is missing')
    if 'type=select' not in group:
        raise OpenFlowError('Expected SELECT group; refusing reconciliation')
    bucket_chunks = group.split('bucket=')[1:]
    if not bucket_chunks:
        raise OpenFlowError('Group has no buckets')
    weights = {b.name: 0 for b in BACKENDS}
    seen = set()
    for chunk in bucket_chunks:
        if chunk.strip().startswith('actions=drop'):
            if len(bucket_chunks) != 1:
                raise OpenFlowError('Drop bucket mixed with live backends')
            continue
        weight_match = re.search(r'\bweight[:=](\d+)', chunk)
        nat_match = re.search(r'nat\(dst=([0-9.]+):(\d+)\)', chunk)
        if not weight_match or not nat_match or 'ct(commit,' not in chunk:
            raise OpenFlowError(f'Unexpected selection bucket: {chunk}')
        backend = next((b for b in BACKENDS
                        if b.ip == nat_match.group(1) and b.port == int(nat_match.group(2))), None)
        if backend is None or backend.name in seen:
            raise OpenFlowError(f'Unknown/duplicate backend bucket: {chunk}')
        seen.add(backend.name)
        weights[backend.name] = int(weight_match.group(1))
    return weights


def read_installed_weights(bridge: str = BRIDGE) -> dict[str, int]:
    return parse_installed_weights(ovs('dump-groups', bridge))


def check_installed(bridge: str = BRIDGE, expected: dict[str, int] | None = None) -> None:
    installed = read_installed_weights(bridge)
    if expected is not None:
        wanted = {b.name: expected.get(b.name, 0) for b in BACKENDS}
        if installed != wanted:
            raise OpenFlowError(f'Group readback mismatch: wanted={wanted}, actual={installed}')


def parse_group_stats(output: str) -> dict:
    """Packet counters of the SELECT group from `ovs-ofctl dump-group-stats`.

    Only TCP SYNs reach the group, so bucket packet counts are the number of
    new connections the switch actually sent to each live bucket.
    """
    line = next((line for line in output.splitlines()
                 if re.search(rf'\bgroup_id={GROUP_ID}\b', line)), None)
    if line is None:
        return {'packet_count': None, 'bucket_counts': []}
    total = re.search(r'(?<!:)packet_count=(\d+)', line)
    return {'packet_count': int(total.group(1)) if total else None,
            'bucket_counts': [int(n) for n in
                              re.findall(r'bucket\d+:packet_count=(\d+)', line)]}


def read_group_stats(bridge: str = BRIDGE) -> dict:
    return parse_group_stats(ovs('dump-group-stats', bridge))


def show(bridge: str = BRIDGE) -> str:
    return ('GROUPS\n' + ovs('dump-groups', bridge)
            + '\nFLOWS\n' + ovs('dump-flows', bridge))


def ports_from_switch(bridge: str = BRIDGE) -> dict[str, int]:
    """Read actual Mininet OVS ports; do not assume stable allocation."""
    ports = {}
    for index, x in enumerate(ALL, start=1):
        interface = f'{bridge}-eth{index}'
        proc = subprocess.run(['ovs-vsctl', 'get', 'Interface', interface,
                               'ofport'], text=True, capture_output=True, check=True)
        ports[x.name] = int(proc.stdout.strip())
    return ports
