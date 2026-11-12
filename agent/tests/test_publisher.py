import asyncio
import re
from dataclasses import replace

import pytest

from bracketboard_agent.adapter import SourceAdapter
from bracketboard_agent.adapters.mock import MockBoutAdapter
from bracketboard_agent.publisher import Publisher
from bracketboard_agent.schema import BoutState, validate_state_message
from bracketboard_agent.transport import Transport, TransportAuthError, TransportError


def run(coro):
    return asyncio.run(coro)


class FakeTransport(Transport):
    def __init__(self):
        self.sent: list[dict] = []
        self.fail: Exception | None = None
        self.ack = "applied"
        self.started = self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send(self, msg):
        if self.fail:
            raise self.fail
        self.sent.append(msg)
        return self.ack


class StaticAdapter(SourceAdapter):
    def __init__(self, state: BoutState | None):
        self.state = state

    async def read_state(self):
        return None if self.state is None else replace(self.state)


@pytest.fixture
def rig(clock):
    ad = StaticAdapter(BoutState(left_score=1, period=1, clock_ms_remaining=120000, clock_state="running"))
    tr = FakeTransport()
    pub = Publisher(ad, tr, clock=clock, wall_ms=lambda: 1_700_000_000_000)
    return ad, tr, pub, clock


def tick(pub):
    return run(pub.tick())


def test_first_tick_sends_seq_1_with_valid_message(rig):
    ad, tr, pub, clock = rig
    assert tick(pub) is True
    m = tr.sent[0]
    assert m["seq"] == 1 and m["sender_ts"] == 1_700_000_000_000
    assert re.fullmatch(r"[0-9a-f]{12}", m["session"])
    validate_state_message(m)
    assert pub.last_reason == "first"


def test_no_send_on_plain_clock_ticks_then_heartbeat_each_second(rig):
    ad, tr, pub, clock = rig
    tick(pub)
    for _ in range(9):  # 0.9 s of exact clock ticks at 10 Hz
        clock.advance(0.1)
        ad.state.clock_ms_remaining = 120000 - round((clock.t - 1000.0) * 1000)
        assert tick(pub) is False
    clock.advance(0.1)
    ad.state.clock_ms_remaining = 119000
    assert tick(pub) is True and pub.last_reason == "heartbeat"
    assert [m["seq"] for m in tr.sent] == [1, 2]  # fresh seq each heartbeat
    assert tr.sent[1]["clock_ms_remaining"] == 119000
    # and the next one a full heartbeat later
    clock.advance(0.99)
    ad.state.clock_ms_remaining = 118010
    assert tick(pub) is False
    clock.advance(0.02)
    ad.state.clock_ms_remaining = 117990
    assert tick(pub) is True and tr.sent[-1]["seq"] == 3


def test_stopped_clock_heartbeat_only(rig):
    ad, tr, pub, clock = rig
    ad.state.clock_state = "stopped"
    tick(pub)
    clock.advance(0.5)
    assert tick(pub) is False
    clock.advance(0.5)
    assert tick(pub) is True and pub.last_reason == "heartbeat"


@pytest.mark.parametrize(
    "field,value",
    [
        ("left_score", 2), ("right_score", 1), ("left_warnings", 1), ("right_warnings", 2),
        ("period", 2), ("clock_state", "stopped"), ("clock_state", "injury"), ("screen", "splash"),
        ("weight", "157"), ("bout", "9"), ("colors_swapped", True),
    ],
)
def test_immediate_send_on_non_clock_change(rig, field, value):
    ad, tr, pub, clock = rig
    tick(pub)
    clock.advance(0.06)  # just past the 20 Hz rate limit; well before the heartbeat
    setattr(ad.state, field, value)
    ad.state.clock_ms_remaining = 120000 - 60
    assert tick(pub) is True
    assert pub.last_reason == "change"
    assert tr.sent[-1][field] == value


def test_clock_edit_triggers_immediate_send(rig):
    ad, tr, pub, clock = rig
    tick(pub)
    clock.advance(0.2)
    ad.state.clock_ms_remaining = 120000 - 200 + 140  # 140 ms off: tolerated
    assert tick(pub) is False
    clock.advance(0.1)
    ad.state.clock_ms_remaining = 120000 - 300 + 151  # > 150 ms off: a clock edit
    assert tick(pub) is True and pub.last_reason == "clock"
    assert len(tr.sent) == 2


def test_clock_edit_while_stopped_and_extrapolation_clamped_at_zero(rig):
    ad, tr, pub, clock = rig
    ad.state.clock_state = "stopped"
    tick(pub)
    clock.advance(0.1)
    ad.state.clock_ms_remaining = 120000 - 200  # stopped clock must not move
    assert tick(pub) is True and pub.last_reason == "clock"

    # running clock near zero: extrapolation floors at 0, no spurious send when it sits at 0
    ad.state.clock_state = "running"
    ad.state.clock_ms_remaining = 50
    clock.advance(0.06)
    tick(pub)  # state change -> sent
    clock.advance(0.2)
    ad.state.clock_ms_remaining = 0
    assert tick(pub) is False


def test_rate_limit_max_hz(rig):
    ad, tr, pub, clock = rig
    tick(pub)
    clock.advance(0.01)
    ad.state.left_score = 2
    assert tick(pub) is False  # < 50 ms since last send
    clock.advance(0.03)
    assert tick(pub) is False
    clock.advance(0.02)  # 60 ms total
    assert tick(pub) is True and tr.sent[-1]["left_score"] == 2
    # a burst of changes never exceeds max_hz
    sent = len(tr.sent)
    for i in range(100):  # 100 changes over 0.5 s
        clock.advance(0.005)
        ad.state.left_score = 3 + i % 50
        tick(pub)
    assert len(tr.sent) - sent <= 11


def test_max_hz_configurable(clock):
    ad = StaticAdapter(BoutState(clock_state="stopped"))
    tr = FakeTransport()
    pub = Publisher(ad, tr, clock=clock, max_hz=2)
    tick(pub)
    clock.advance(0.3)
    ad.state.left_score = 1
    assert tick(pub) is False
    clock.advance(0.3)
    assert tick(pub) is True


def test_seq_masks_at_2_pow_32(clock):
    ad = StaticAdapter(BoutState(clock_state="stopped"))
    tr = FakeTransport()
    pub = Publisher(ad, tr, clock=clock, first_seq=0xFFFFFFFE)
    for _ in range(3):
        tick(pub)
        clock.advance(1.0)
    assert [m["seq"] for m in tr.sent] == [0xFFFFFFFE, 0xFFFFFFFF, 0]
    for m in tr.sent:
        validate_state_message(m)


def test_new_session_per_publisher(clock):
    sessions = set()
    for _ in range(20):
        sessions.add(Publisher(StaticAdapter(None), FakeTransport(), clock=clock).session)
    assert len(sessions) == 20
    a = Publisher(StaticAdapter(BoutState()), FakeTransport(), clock=clock)
    assert a.seq == 1


def test_immediate_send_on_connect_even_inside_rate_limit(rig):
    ad, tr, pub, clock = rig
    tick(pub)
    clock.advance(0.001)
    assert tick(pub) is False
    tr.on_connected()  # publisher registered itself
    assert tick(pub) is True and pub.last_reason == "connect"
    assert tick(pub) is False


def test_unavailable_source_sends_nothing(rig):
    ad, tr, pub, clock = rig
    ad.state = None
    for _ in range(5):
        clock.advance(1.0)
        assert tick(pub) is False
    assert tr.sent == []
    ad.state = BoutState()
    assert tick(pub) is True


def test_invalid_adapter_state_not_sent(rig):
    ad, tr, pub, clock = rig
    ad.state.left_score = 100
    assert tick(pub) is False and tr.sent == []
    ad.state.left_score = 5
    clock.advance(1.5)
    assert tick(pub) is True


def test_transport_error_retries_and_keeps_seq_moving(rig):
    ad, tr, pub, clock = rig
    tr.fail = TransportError("down")
    assert tick(pub) is False
    clock.advance(0.5)
    assert tick(pub) is False  # inside backoff
    tr.fail = None
    clock.advance(1.0)
    assert tick(pub) is True
    assert tr.sent[0]["seq"] == 2  # seq of the failed attempt is never reused


def test_auth_error_backs_off(rig):
    ad, tr, pub, clock = rig
    tr.fail = TransportAuthError("401")
    assert tick(pub) is False
    tr.fail = None
    clock.advance(2.0)
    assert tick(pub) is False
    clock.advance(3.5)
    assert tick(pub) is True


def test_ack_is_recorded(rig):
    ad, tr, pub, clock = rig
    tr.ack = "invalid"
    tick(pub)
    assert pub.last_ack == "invalid"


def test_run_lifecycle_and_full_mock_bout():
    """End to end with the real clock: a 440 s script at speed 4000 plays in ~0.1 s."""
    ad = MockBoutAdapter(speed=4000.0)
    tr = FakeTransport()
    pub = Publisher(ad, tr, heartbeat_s=0.02, poll_hz=500, max_hz=500)

    async def go():
        task = asyncio.ensure_future(pub.run())
        for _ in range(500):
            await asyncio.sleep(0.01)
            if ad.finished and len(tr.sent) > 5:
                break
        await asyncio.sleep(0.05)
        pub.stop()
        await asyncio.wait_for(task, 2)

    run(go())
    assert tr.started and tr.stopped
    seqs = [m["seq"] for m in tr.sent]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert len({m["session"] for m in tr.sent}) == 1
    for m in tr.sent:
        validate_state_message(m)
    last = tr.sent[-1]
    assert last["screen"] == "splash" and last["left_score"] == 7 and last["right_score"] == 4
    assert {m["clock_state"] for m in tr.sent} == {"running", "stopped", "injury", "expired"}


def test_run_wakes_on_connect(clock):
    ad = StaticAdapter(BoutState(clock_state="stopped"))
    tr = FakeTransport()
    pub = Publisher(ad, tr, heartbeat_s=60, poll_hz=0.1)  # would sleep 10 s otherwise

    async def go():
        task = asyncio.ensure_future(pub.run())
        await asyncio.sleep(0.1)
        assert len(tr.sent) == 1
        tr.on_connected()
        await asyncio.sleep(0.1)
        n = len(tr.sent)
        pub.stop()
        await asyncio.wait_for(task, 2)
        return n

    assert run(go()) == 2
