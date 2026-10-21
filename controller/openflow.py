"""OpenFlow 1.3 + OVS (Nicira) extension messages for stateful VIP DNAT.

New TCP SYN -> conntrack lookup -> SELECT group -> commit NAT -> route.
Committed connections -> conntrack NAT -> route, never rehash.
Return traffic -> conntrack reverse NAT -> route to client.

No packet ever needs a controller Packet-In, and a controller restart does
not flush the switch flow table or conntrack state.
"""
from __future__ import annotations

from ryu.ofproto import nicira_ext
from ryu.ofproto import ofproto_v1_3 as ofp
from ryu.ofproto import ofproto_v1_3_parser as parser

from .config import (ALL, BACKENDS, BRIDGE, CLIENTS, CT_ZONE, GATEWAY_MAC,
                     GROUP_ID, PROBE, VIP, VPORT)

ETH_IP = 0x0800
IP_TCP = 6
CT_NEW, CT_INVALID, CT_TRACKED = 0x01, 0x10, 0x20
TCP_SYN, TCP_ACK = 0x002, 0x010
NX_IN_PORT = 0xfff8           # Nicira actions use 16-bit (OF1.0) port numbers
NAT_DST = 1 << 1              # NX_NAT_F_DST
TABLE_CLASSIFY, TABLE_CONNTRACK, TABLE_ROUTE = 0, 10, 30


class _NATAction(parser.NXActionNAT):
    """Ryu 4.34 declares 32 bytes for a NAT with address+port range but
    serializes 28; pad so actions nested inside ct(...) stay aligned."""

    def serialize_body(self):
        data = super().serialize_body()
        header = ofp.OFP_ACTION_EXPERIMENTER_HEADER_SIZE + 2
        return data + bytes(-(header + len(data)) % 8)


def _ct(table: int, commit=False, actions=()) -> parser.NXActionCT:
    return parser.NXActionCT(
        flags=nicira_ext.NX_CT_F_COMMIT if commit else 0, zone_src='',
        zone_ofs_nbits=CT_ZONE, recirc_table=table, alg=0, actions=list(actions))


def _flow(datapath, table: int, priority: int, match: dict, actions=()):
    instructions = ([parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, list(actions))]
                    if actions else [])  # no instructions = drop
    return parser.OFPFlowMod(datapath, table_id=table, priority=priority,
                             match=parser.OFPMatch(**match),
                             instructions=instructions)


def _route(port: int, mac: str) -> list:
    """Act as the IPv4 gateway: explicit MAC rewrite instead of NORMAL."""
    return [parser.OFPActionDecNwTtl(),
            parser.OFPActionSetField(eth_src=GATEWAY_MAC),
            parser.OFPActionSetField(eth_dst=mac),
            parser.OFPActionOutput(port)]


def flow_mods(datapath, ports: dict[str, int]) -> list:
    """All pipeline flows; needs interface name -> actual OVS port number."""
    required = {x.name for x in ALL}
    if set(ports) != required or len(set(ports.values())) != len(ports):
        raise ValueError('Need seven unique, exactly named OVS port numbers')
    if any(not isinstance(p, int) or p <= 0 for p in ports.values()):
        raise ValueError('OVS port numbers must be positive integers')
    tcp = {'eth_type': ETH_IP, 'ip_proto': IP_TCP}
    track = _ct(TABLE_CONNTRACK, actions=[parser.NXActionNAT(flags=0)])
    resubmit_route = parser.NXActionResubmitTable(in_port=NX_IN_PORT, table_id=TABLE_ROUTE)
    flows = []

    # Table 0: only client TCP sessions to the VIP, and backend replies.
    for c in CLIENTS:
        flows.append(_flow(datapath, TABLE_CLASSIFY, 200,
                           {'in_port': ports[c.name], **tcp, 'ipv4_dst': VIP,
                            'tcp_dst': VPORT}, [track]))
    for b in BACKENDS:
        flows.append(_flow(datapath, TABLE_CLASSIFY, 200,
                           {'in_port': ports[b.name], **tcp, 'ipv4_src': b.ip}, [track]))
    # Health checks: only p1 may reach an individual backend directly. These
    # priority-300 rules bypass VIP DNAT; otherwise the probe's reply would be
    # classified as unsolicited backend traffic. Clients cannot use them.
    for b in BACKENDS:
        flows.append(_flow(datapath, TABLE_CLASSIFY, 300,
                           {'in_port': ports[PROBE.name], **tcp, 'ipv4_src': PROBE.ip,
                            'ipv4_dst': b.ip, 'tcp_dst': b.port}, [resubmit_route]))
        flows.append(_flow(datapath, TABLE_CLASSIFY, 300,
                           {'in_port': ports[b.name], **tcp, 'ipv4_src': b.ip,
                            'ipv4_dst': PROBE.ip, 'tcp_src': b.port}, [resubmit_route]))
    flows.append(_flow(datapath, TABLE_CLASSIFY, 0, {}))

    # Table 10: a committed ct_mark is authoritative even for retransmitted
    # SYNs still flagged +new. Uncommitted SYNs get exactly one backend
    # selection. Unsolicited non-SYN traffic is dropped.
    flows += [
        _flow(datapath, TABLE_CONNTRACK, 300,
              {'ct_state': (CT_TRACKED | CT_INVALID, CT_TRACKED | CT_INVALID)}),
        _flow(datapath, TABLE_CONNTRACK, 250,
              {'ct_state': (CT_TRACKED, CT_TRACKED), 'ct_mark': (1, 1)}, [resubmit_route]),
        _flow(datapath, TABLE_CONNTRACK, 200,
              {**tcp, 'ct_state': (CT_TRACKED | CT_NEW, CT_TRACKED | CT_NEW),
               'ct_mark': (0, 1), 'ipv4_dst': VIP, 'tcp_dst': VPORT,
               'tcp_flags': (TCP_SYN, TCP_SYN | TCP_ACK)},
              [parser.OFPActionGroup(GROUP_ID)]),
        _flow(datapath, TABLE_CONNTRACK, 0, {}),
    ]

    # Table 30: packets are already DNATed (or reverse-NATed); route them.
    for x in ALL:
        flows.append(_flow(datapath, TABLE_ROUTE, 200,
                           {'eth_type': ETH_IP, 'ipv4_dst': x.ip}, _route(ports[x.name], x.mac)))
    flows.append(_flow(datapath, TABLE_ROUTE, 0, {}))
    return flows


def validate_weights(weights: dict[str, int]) -> None:
    unknown = set(weights) - {b.name for b in BACKENDS}
    if unknown:
        raise ValueError(f'Unknown backend(s): {sorted(unknown)}')
    for name, weight in weights.items():
        if not isinstance(weight, int) or weight < 0 or weight > 65535:
            raise ValueError(f'Invalid weight for {name}: {weight}')


def group_mod(datapath, weights: dict[str, int], command=ofp.OFPGC_MODIFY):
    """New connections only; changing buckets NEVER flushes conntrack.

    Each bucket commits the connection with DNAT to its backend and marks it
    (like OVN's ct_lb_mark), then routes it via table 30. A group needs at
    least one bucket: with no live backend, a drop bucket rejects new
    connections while marked connections continue through tables 10/30.
    """
    validate_weights(weights)
    buckets = []
    for b in BACKENDS:
        weight = weights.get(b.name, 0)
        if weight:
            nat = _NATAction(flags=NAT_DST, range_ipv4_min=b.ip, range_ipv4_max=b.ip,
                             range_proto_min=b.port, range_proto_max=b.port)
            commit = _ct(TABLE_ROUTE, commit=True,
                         actions=[nat, parser.OFPActionSetField(ct_mark=1)])
            buckets.append(parser.OFPBucket(weight=weight, watch_port=ofp.OFPP_ANY,
                                            watch_group=ofp.OFPG_ANY, actions=[commit]))
    if not buckets:
        buckets.append(parser.OFPBucket(weight=1, watch_port=ofp.OFPP_ANY,
                                        watch_group=ofp.OFPG_ANY, actions=[]))
    return parser.OFPGroupMod(datapath, command, ofp.OFPGT_SELECT, GROUP_ID, buckets)


def weights_from_group_desc(groups) -> dict[str, int] | None:
    """Read installed weights from a group-description reply.

    None means the group is absent (a fresh switch). Raise rather than guess
    when an unknown group structure is installed.
    """
    group = next((g for g in groups if g.group_id == GROUP_ID), None)
    if group is None:
        return None
    if group.type != ofp.OFPGT_SELECT:
        raise ValueError('Expected SELECT group; refusing reconciliation')
    if not group.buckets:
        raise ValueError('Group has no buckets')
    weights = {b.name: 0 for b in BACKENDS}
    for bucket in group.buckets:
        if not bucket.actions:
            if len(group.buckets) != 1:
                raise ValueError('Drop bucket mixed with live backends')
            continue
        commit = bucket.actions[0]
        nat = next((a for a in getattr(commit, 'actions', [])
                    if isinstance(a, parser.NXActionNAT)), None)
        if (len(bucket.actions) != 1 or not isinstance(commit, parser.NXActionCT)
                or not commit.flags & nicira_ext.NX_CT_F_COMMIT or nat is None):
            raise ValueError(f'Unexpected selection bucket: {bucket}')
        backend = next((b for b in BACKENDS if b.ip == nat.range_ipv4_min
                        and b.port == nat.range_proto_min), None)
        if backend is None or weights[backend.name]:
            raise ValueError(f'Unknown/duplicate backend bucket: {nat.range_ipv4_min}')
        weights[backend.name] = bucket.weight
    return weights


def group_counters(stats) -> dict | None:
    """Packet counters from a group-stats reply. Only TCP SYNs reach the
    group, so bucket counts are new connections per live bucket."""
    group = next((g for g in stats if g.group_id == GROUP_ID), None)
    if group is None:
        return None
    return {'packet_count': group.packet_count,
            'bucket_counts': [b.packet_count for b in group.bucket_stats]}


def ports_from_desc(port_descs) -> dict[str, int]:
    """Map lab hosts to OVS port numbers. Mininet names the switch side of
    the i-th link `<bridge>-eth<i>`, in the order the topology adds them."""
    by_name = {(p.name.decode() if isinstance(p.name, bytes) else p.name): p.port_no
               for p in port_descs}
    ports = {}
    for index, x in enumerate(ALL, start=1):
        interface = f'{BRIDGE}-eth{index}'
        if interface not in by_name:
            raise ValueError(f'Switch port {interface} for {x.name} not found')
        ports[x.name] = by_name[interface]
    return ports
