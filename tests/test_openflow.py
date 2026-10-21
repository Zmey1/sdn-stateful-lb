"""OpenFlow message tests. Messages are serialized by Ryu and decoded by
`ovs-ofctl ofp-print`, so they check exactly what OVS would receive."""
import shutil
import subprocess

import pytest
from ryu.ofproto import ofproto_v1_3 as ofp
from ryu.ofproto import ofproto_v1_3_parser as parser

from controller.config import ALL
from controller.openflow import (flow_mods, group_counters, group_mod,
                                 ports_from_desc, weights_from_group_desc)

PORTS = {x.name: i + 1 for i, x in enumerate(ALL)}


class FakeDatapath:
    ofproto = ofp
    ofproto_parser = parser
    id = 1


def wire(message) -> bytes:
    message.serialize()
    return bytes(message.buf)


def decode(message) -> str:
    if not shutil.which('ovs-ofctl'):
        pytest.skip('ovs-ofctl not installed')
    out = subprocess.run(['ovs-ofctl', 'ofp-print', wire(message).hex()],
                         capture_output=True, text=True, check=True).stdout
    assert 'decode error' not in out, out
    return out


def test_pipeline_flows_decode_as_ovs_rules():
    flows = '\n'.join(decode(m) for m in flow_mods(FakeDatapath(), PORTS))
    assert 'CONTROLLER' not in flows and 'NORMAL' not in flows
    assert 'actions=ct(table=10,zone=10,nat)' in flows
    assert 'ct_state=+new+trk,ct_mark=0/0x1,tcp,nw_dst=10.0.3.100,tp_dst=8080,' \
           'tcp_flags=+syn-ack' in flows
    assert 'actions=group:100' in flows
    assert 'ct_state=+trk,ct_mark=0x1/0x1' in flows
    assert 'ct_state=+inv+trk' in flows
    assert f'in_port={PORTS["p1"]},nw_src=10.0.1.50,nw_dst=10.0.2.11,tp_dst=8080' in flows
    assert 'nw_src=10.0.2.11,nw_dst=10.0.1.50,tp_src=8080' in flows
    assert 'resubmit(,30)' in flows
    assert 'actions=dec_ttl,set_field:02:00:00:00:00:01->eth_src,' \
           'set_field:02:00:00:00:02:11->eth_dst,output:4' in flows
    assert flows.count('actions=drop') == 4  # default in tables 0/10/30 + invalid


def test_group_buckets_commit_stateful_nat():
    text = decode(group_mod(FakeDatapath(), {'b1': 60, 'b2': 30, 'b3': 10}))
    assert 'MOD group_id=100,type=select' in text
    assert text.count('ct(commit,table=30,zone=10,nat(dst=') == 3
    assert 'bucket=weight:60,actions=ct(commit,table=30,zone=10,' \
           'nat(dst=10.0.2.11:8080),exec(set_field:0x1->ct_mark))' in text


def test_zero_weights_remove_backend_for_new_flows_only():
    text = decode(group_mod(FakeDatapath(), {'b1': 100, 'b2': 0, 'b3': 0}))
    assert text.count('bucket=') == 1
    assert '10.0.2.11' in text and '10.0.2.12' not in text


def test_no_alive_backends_drop_new_connections():
    text = decode(group_mod(FakeDatapath(), {}, ofp.OFPGC_ADD))
    assert 'ADD group_id=100,type=select,bucket=actions=drop' in text


def _parsed_group(weights):
    """Round-trip: parse our own buckets back the way a desc reply would be."""
    buf = wire(group_mod(FakeDatapath(), weights))
    offset, buckets = ofp.OFP_GROUP_MOD_SIZE, []
    while offset < len(buf):
        bucket = parser.OFPBucket.parser(buf, offset)
        buckets.append(bucket)
        offset += bucket.len
    return parser.OFPGroupDescStats(type_=ofp.OFPGT_SELECT, group_id=100, buckets=buckets)


def test_reconcile_weights_from_installed_group():
    assert weights_from_group_desc([_parsed_group({'b1': 42, 'b3': 129})]) == {
        'b1': 42, 'b2': 0, 'b3': 129}
    assert weights_from_group_desc([_parsed_group({})]) == {'b1': 0, 'b2': 0, 'b3': 0}
    assert weights_from_group_desc([]) is None


def test_reconcile_refuses_unknown_group_structure():
    foreign = parser.OFPGroupDescStats(
        type_=ofp.OFPGT_SELECT, group_id=100,
        buckets=[parser.OFPBucket(weight=5, watch_port=ofp.OFPP_ANY, watch_group=ofp.OFPG_ANY,
                                  actions=[parser.OFPActionOutput(1)])])
    with pytest.raises(ValueError):
        weights_from_group_desc([foreign])
    indirect = parser.OFPGroupDescStats(type_=ofp.OFPGT_INDIRECT, group_id=100, buckets=[])
    with pytest.raises(ValueError):
        weights_from_group_desc([indirect])


def test_group_counters():
    stats = parser.OFPGroupStats(group_id=100, packet_count=30, bucket_stats=[
        parser.OFPBucketCounter(10, 660), parser.OFPBucketCounter(20, 1320)])
    assert group_counters([stats]) == {'packet_count': 30, 'bucket_counts': [10, 20]}
    assert group_counters([]) is None


def test_ports_from_port_desc():
    descs = [parser.OFPPort(port_no=i + 10, hw_addr='00:00:00:00:00:01', config=0, state=0,
                            curr=0, advertised=0, supported=0, peer=0, curr_speed=0,
                            max_speed=0, name=f's1-eth{i + 1}'.encode())
             for i in range(len(ALL))]
    assert ports_from_desc(descs) == {x.name: i + 10 for i, x in enumerate(ALL)}
    with pytest.raises(ValueError):
        ports_from_desc(descs[:-1])


def test_reject_bad_ports_and_weights():
    with pytest.raises(ValueError): flow_mods(FakeDatapath(), {'b1': 1})
    with pytest.raises(ValueError): flow_mods(FakeDatapath(), {x.name: 1 for x in ALL})
    with pytest.raises(ValueError): group_mod(FakeDatapath(), {'b4': 100})
    with pytest.raises(ValueError): group_mod(FakeDatapath(), {'b1': -1})
    with pytest.raises(ValueError): group_mod(FakeDatapath(), {'b1': 70000})
