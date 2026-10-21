"""Generate OpenFlow 1.3 + OVS-extension rules for stateful VIP DNAT.

New TCP SYN -> conntrack lookup -> SELECT group -> commit NAT -> route.
Committed connections -> conntrack NAT -> route, never rehash.
Return traffic -> conntrack reverse NAT -> route to client.

No packet needs a controller Packet-In, and a controller restart does not
flush the switch flow table or conntrack state.
"""
from .config import BACKENDS, CLIENTS, PROBE, VIP, VPORT, GATEWAY_MAC, CT_ZONE, GROUP_ID


def flow_lines(ofports: dict[str, int]) -> list[str]:
    """Requires interface name -> actual OVS port number mapping."""
    required = {x.name for x in (*CLIENTS, *BACKENDS, PROBE)}
    if set(ofports) != required or len(set(ofports.values())) != len(ofports):
        raise ValueError('Need seven unique, exactly named OVS port numbers')
    if any(not isinstance(p, int) or p <= 0 for p in ofports.values()):
        raise ValueError('OVS port numbers must be positive integers')

    flows: list[str] = []
    # table 0: only allow client TCP sessions to service VIP, and backend replies.
    for c in CLIENTS:
        flows.append(f'table=0,priority=200,in_port={ofports[c.name]},tcp,'
                     f'nw_dst={VIP},tp_dst={VPORT},'
                     f'actions=ct(table=10,zone={CT_ZONE},nat)')
    for b in BACKENDS:
        flows.append(f'table=0,priority=200,in_port={ofports[b.name]},tcp,'
                     f'nw_src={b.ip},actions=ct(table=10,zone={CT_ZONE},nat)')
    flows.append('table=0,priority=0,actions=drop')

    # Management health checks: only p1 may reach an individual backend
    # directly. They bypass VIP DNAT and use a separate return rule. These
    # priority-300 matches override the general backend ct lookup, otherwise
    # the HTTP health response would be classified as an unsolicited packet.
    # Client h1/h2/h3 cannot use these bypass rules.
    for b in BACKENDS:
        flows.append(f'table=0,priority=300,in_port={ofports[PROBE.name]},tcp,'
                     f'nw_src={PROBE.ip},nw_dst={b.ip},tp_dst={b.port},'
                     'actions=resubmit(,30)')
        flows.append(f'table=0,priority=300,in_port={ofports[b.name]},tcp,'
                     f'nw_src={b.ip},nw_dst={PROBE.ip},tp_src={b.port},'
                     'actions=resubmit(,30)')

    # table 10: committed ct_mark is authoritative even for retransmitted SYNs
    # still carrying the +new ct_state flag. Not-yet-committed TCP SYNs get
    # exactly one backend selection. Reject unsolicited non-SYN traffic.
    flows.extend([
        'table=10,priority=300,ct_state=+trk+inv,actions=drop',
        'table=10,priority=250,ct_state=+trk,ct_mark=0x1/0x1,actions=resubmit(,30)',
        f'table=10,priority=200,tcp,ct_state=+trk+new,ct_mark=0x0/0x1,'
        f'nw_dst={VIP},tp_dst={VPORT},tcp_flags=+syn-ack,'
        f'actions=group:{GROUP_ID}',
        'table=10,priority=0,actions=drop',
    ])

    # table 30: the packet has already been DNATed (or reverse-NATed).
    # Use explicit, predictable router MAC rewriting instead of 'NORMAL'.
    for b in BACKENDS:
        flows.append(f'table=30,priority=200,ip,nw_dst={b.ip},'
                     f'actions=dec_ttl,set_field:{GATEWAY_MAC}->eth_src,'
                     f'set_field:{b.mac}->eth_dst,output:{ofports[b.name]}')
    for c in (*CLIENTS, PROBE):
        flows.append(f'table=30,priority=200,ip,nw_dst={c.ip},'
                     f'actions=dec_ttl,set_field:{GATEWAY_MAC}->eth_src,'
                     f'set_field:{c.mac}->eth_dst,output:{ofports[c.name]}')
    flows.append('table=30,priority=0,actions=drop')
    return flows


def group_text(weights: dict[str, int]) -> str:
    """New connections only; removing a bucket NEVER flushes conntrack.

    At least one bucket is required for an OpenFlow group. With no healthy
    backends, use a drop bucket so existing marked connections can continue
    through table 10/30 while new connections are rejected.
    """
    unknown = set(weights) - {b.name for b in BACKENDS}
    if unknown:
        raise ValueError(f'Unknown backend(s): {sorted(unknown)}')
    for name, weight in weights.items():
        if not isinstance(weight, int) or weight < 0 or weight > 65535:
            raise ValueError(f'Invalid weight for {name}: {weight}')
    buckets = []
    for b in BACKENDS:
        w = weights.get(b.name, 0)
        if w:
            # OVS-specific ct action in SELECT bucket, as in OVN's ct_lb_mark.
            # Table 30 is recirculated after commit (group actions restore
            # packet/metadata after executing the bucket).
            ct = (f'ct(commit,zone={CT_ZONE},'
                  f'nat(dst={b.ip}:{b.port}),table=30,'
                  f'exec(set_field:0x1->ct_mark))')
            buckets.append(f'bucket=weight={w},actions={ct}')
    if not buckets:
        buckets.append('bucket=weight=1,actions=drop')
    return f'group_id={GROUP_ID},type=select,' + ','.join(buckets)


def validate_distribution(weights: dict[str, int]) -> dict[str, int]:
    """Check a weight dictionary with the same rules as a group update."""
    group_text(weights)
    return {b.name: weights.get(b.name, 0) for b in BACKENDS}
