import pytest
from controller.config import ALL
from controller.pipeline import flow_lines, group_text

PORTS = {x.name: i + 1 for i, x in enumerate(ALL)}


def test_pipeline_no_packet_in_or_normal():
    flows = '\n'.join(flow_lines(PORTS))
    assert 'CONTROLLER' not in flows
    assert 'NORMAL' not in flows
    assert 'ct(table=10,zone=10,nat)' in flows
    assert 'ct_mark=0x1/0x1' in flows
    assert 'tcp_flags=+syn-ack' in flows
    assert 'group:100' in flows
    assert 'table=30' in flows
    assert f'in_port={PORTS["p1"]}' in flows
    assert 'nw_src=10.0.1.50,nw_dst=10.0.2.11,tp_dst=8080' in flows
    assert 'nw_dst=10.0.1.50,tp_src=8080' in flows
    assert '10.0.2.11' in flows and '10.0.1.11' in flows


def test_group_buckets_commit_stateful_nat():
    group = group_text({'b1': 60, 'b2': 30, 'b3': 10})
    assert group.count('ct(commit,') == 3
    assert group.count('exec(set_field:0x1->ct_mark)') == 3
    assert group.count('table=30') == 3
    assert 'nat(dst=10.0.2.11:8080)' in group


def test_zero_weights_remove_backend_for_new_flows_only():
    group = group_text({'b1': 100, 'b2': 0, 'b3': 0})
    assert group.count('bucket=') == 1
    assert '10.0.2.11' in group and '10.0.2.12' not in group


def test_no_alive_backends_drop_new_connections():
    group = group_text({})
    assert 'bucket=weight=1,actions=drop' in group
    assert 'ct(' not in group


def test_reject_bad_ports():
    with pytest.raises(ValueError): flow_lines({'b1': 1})
    with pytest.raises(ValueError): flow_lines({x.name: 1 for x in ALL})


def test_reject_invalid_weights():
    with pytest.raises(ValueError): group_text({'b4': 100})
    with pytest.raises(ValueError): group_text({'b1': -1})
    with pytest.raises(ValueError): group_text({'b1': 70000})


def test_parse_group_stats_bucket_counters():
    from controller.ofctl import parse_group_stats
    text = ('OFPST_GROUP reply (OF1.3) (xid=0x2):\n'
            ' group_id=100,duration=12.3s,ref_count=3,packet_count=30,byte_count=1980,'
            'bucket0:packet_count=10,byte_count=660,bucket1:packet_count=20,byte_count=1320\n')
    assert parse_group_stats(text) == {'packet_count': 30, 'bucket_counts': [10, 20]}
    assert parse_group_stats('group_id=7,packet_count=1') == {'packet_count': None,
                                                             'bucket_counts': []}
