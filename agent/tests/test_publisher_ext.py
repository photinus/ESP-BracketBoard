"""Publisher cadence for the optional timed extension fields (riding_ms, shot_clock_ms) and schema handling."""
import asyncio
from dataclasses import replace

import pytest

from bracketboard_agent.adapter import SourceAdapter
from bracketboard_agent.publisher import Publisher
from bracketboard_agent.schema import BoutState, SchemaError, to_message, validate_state_message
from bracketboard_agent.transport import Transport


class Tr(Transport):
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)
        return "applied"


class Static(SourceAdapter):
    def __init__(self, st):
        self.state = st

    async def read_state(self):
        return replace(self.state)


def base(**kw):
    d = dict(clock_ms_remaining=60_000, clock_state="running", riding_ms=10_000, riding_dir=1,
             shot_clock_ms=20_000, shot_clock_side="left", clock_label="Period 2", blink="none")
    d.update(kw)
    return BoutState(**d)


@pytest.fixture
def rig(clock):
    ad = Static(base())
    tr = Tr()
    pub = Publisher(ad, tr, clock=clock, wall_ms=lambda: 1, max_hz=0)
    assert asyncio.run(pub.tick()) is True
    return ad, tr, pub, clock


def tick(pub):
    return asyncio.run(pub.tick())


def test_linear_extrapolation_of_riding_and_shot_clock_sends_nothing(rig):
    ad, tr, pub, clock = rig
    clock.advance(0.5)
    ad.state = base(clock_ms_remaining=59_500, riding_ms=10_500, shot_clock_ms=19_500)
    assert tick(pub) is False
    assert len(tr.sent) == 1


def test_riding_deviation_sends(rig):
    ad, tr, pub, clock = rig
    clock.advance(0.5)
    ad.state = base(clock_ms_remaining=59_500, riding_ms=11_500, shot_clock_ms=19_500)  # manual edit of +1 s
    assert tick(pub) is True and pub.last_reason == "clock"


def test_riding_direction_sign_is_used(rig):
    ad, tr, pub, clock = rig
    ad.state = base(riding_dir=-1, riding_ms=-10_000)
    assert tick(pub) is True and pub.last_reason == "change"
    clock.advance(0.5)
    ad.state = base(clock_ms_remaining=59_500, riding_dir=-1, riding_ms=-10_500, shot_clock_ms=19_500)
    assert tick(pub) is False


def test_shot_clock_deviation_sends(rig):
    ad, tr, pub, clock = rig
    clock.advance(0.5)
    ad.state = base(clock_ms_remaining=59_500, riding_ms=10_500, shot_clock_ms=25_000)
    assert tick(pub) is True


def test_field_appearing_or_disappearing_sends(rig):
    ad, tr, pub, clock = rig
    ad.state = base(riding_ms=None, riding_dir=None)
    assert tick(pub) is True
    clock.advance(0.3)
    ad.state = base(riding_ms=None, riding_dir=None, shot_clock_ms=None, shot_clock_side=None)
    assert tick(pub) is True


def test_stopped_clock_does_not_extrapolate_extensions(clock):
    ad = Static(base(clock_state="stopped"))
    tr = Tr()
    pub = Publisher(ad, tr, clock=clock, wall_ms=lambda: 1, max_hz=0)
    asyncio.run(pub.tick())
    clock.advance(0.5)
    assert tick(pub) is False  # unchanged values on a stopped clock
    ad.state = base(clock_state="stopped", riding_ms=10_500)
    assert tick(pub) is True


def test_non_timed_extension_change_sends(rig):
    ad, tr, pub, clock = rig
    ad.state = base(blink="left")
    assert tick(pub) is True and pub.last_reason == "change"
    ad.state = base(blink="left", clock_label="Break")
    clock.advance(0.1)
    assert tick(pub) is True


# --- schema ----------------------------------------------------------------------------------------
def msg(**extra):
    m = to_message(BoutState(), "sess", 1, 0)
    m.update(extra)
    return m


def test_to_message_omits_none_extensions_and_includes_set_ones():
    assert set(to_message(BoutState(), "s", 1, 0)) == {
        "type", "v", "session", "seq", "sender_ts", "left_score", "right_score", "left_warnings", "right_warnings",
        "period", "clock_ms_remaining", "clock_state", "screen", "weight", "bout", "colors_swapped"}
    m = to_message(base(), "s", 1, 0)
    assert m["clock_label"] == "Period 2" and m["riding_ms"] == 10_000 and m["shot_clock_side"] == "left"
    validate_state_message(m)
    assert len(str(m).encode()) < 1024


@pytest.mark.parametrize("extra", [
    {"clock_label": ""}, {"clock_label": "x" * 12}, {"blink": "none"}, {"blink": "left"}, {"blink": "right"},
    {"riding_ms": -3600000}, {"riding_ms": 3600000}, {"riding_ms": 0}, {"riding_dir": -1}, {"riding_dir": 0},
    {"riding_dir": 1}, {"shot_clock_ms": 0}, {"shot_clock_ms": 3600000}, {"shot_clock_side": "right"},
    {"riding_ms": 5.0}, {"mystery_field": [1, 2]},
])
def test_valid_extensions(extra):
    validate_state_message(msg(**extra))


@pytest.mark.parametrize("extra,code,field", [
    ({"clock_label": 5}, "bad_type", "clock_label"),
    ({"clock_label": None}, "bad_type", "clock_label"),
    ({"clock_label": "x" * 13}, "out_of_range", "clock_label"),
    ({"clock_label": "café"}, "out_of_range", "clock_label"),
    ({"clock_label": "a\nb"}, "out_of_range", "clock_label"),
    ({"blink": 1}, "bad_type", "blink"),
    ({"blink": "both"}, "out_of_range", "blink"),
    ({"blink": "LEFT"}, "out_of_range", "blink"),
    ({"riding_ms": "5"}, "bad_type", "riding_ms"),
    ({"riding_ms": 1.5}, "bad_type", "riding_ms"),
    ({"riding_ms": True}, "bad_type", "riding_ms"),
    ({"riding_ms": 3600001}, "out_of_range", "riding_ms"),
    ({"riding_ms": -3600001}, "out_of_range", "riding_ms"),
    ({"riding_dir": 2}, "out_of_range", "riding_dir"),
    ({"riding_dir": -2}, "out_of_range", "riding_dir"),
    ({"riding_dir": "1"}, "bad_type", "riding_dir"),
    ({"shot_clock_ms": -1}, "out_of_range", "shot_clock_ms"),
    ({"shot_clock_ms": 3600001}, "out_of_range", "shot_clock_ms"),
    ({"shot_clock_ms": None}, "bad_type", "shot_clock_ms"),
    ({"shot_clock_side": "up"}, "out_of_range", "shot_clock_side"),
    ({"shot_clock_side": 0}, "bad_type", "shot_clock_side"),
])
def test_invalid_extensions(extra, code, field):
    with pytest.raises(SchemaError) as ei:
        validate_state_message(msg(**extra))
    assert (ei.value.code, ei.value.field) == (code, field)


def test_required_field_errors_take_precedence_over_extension_errors():
    m = msg(riding_dir=9)
    del m["bout"]
    with pytest.raises(SchemaError) as ei:
        validate_state_message(m)
    assert (ei.value.code, ei.value.field) == ("missing_field", "bout")
