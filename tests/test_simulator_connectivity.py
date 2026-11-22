import random

from fleet_pipeline.simulator.connectivity import Connectivity


def always_online():
    return Connectivity(random.Random(1), outage_probability=0.0)


def test_online_device_sends_each_packet_immediately():
    link = always_online()
    assert [link.push(i) for i in range(3)] == [[0], [1], [2]]


def test_offline_device_buffers_and_then_flushes_in_order():
    link = Connectivity(random.Random(1), outage_probability=1.0, outage_steps=(3, 3))
    assert link.push("a") == []  # outage starts
    assert not link.online
    link.outage_probability = 0.0  # no new outage once this one ends
    assert link.push("b") == []
    assert link.push("c") == []
    assert link.push("d") == ["a", "b", "c", "d"]
    assert link.online


def test_no_packet_is_lost_or_duplicated():
    link = Connectivity(random.Random(7), outage_probability=0.2, outage_steps=(1, 5))
    sent = [packet for i in range(500) for packet in link.push(i)]
    link.outage_probability = 0.0
    while not link.online:
        sent.extend(link.push(None))
    sent.extend(link.push(None))
    assert sent == list(range(500))


def test_ticks_without_a_packet_are_fine():
    link = always_online()
    assert link.push(None) == []
    assert link.push("x") == ["x"]
