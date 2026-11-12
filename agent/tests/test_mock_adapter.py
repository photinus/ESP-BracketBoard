import asyncio
import json

import pytest

from bracketboard_agent.adapters.mock import DEFAULT_SCRIPT, MockBoutAdapter
from bracketboard_agent.schema import to_message, validate_state_message


def run(coro):
    return asyncio.run(coro)


def make(clock, script=None, **kw):
    a = MockBoutAdapter(script, clock=clock, **kw)
    run(a.start())
    return a


def state_at(a, clock, t_script):
    """Advance the fake clock to absolute script time t (speed 1) and read."""
    clock.t = a._t0 + t_script / a.speed
    return run(a.read_state())


def write_script(tmp_path, events):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(events))
    return p


def test_default_script_exists_and_is_valid_json():
    ev = json.loads(DEFAULT_SCRIPT.read_text())
    assert {e["do"] for e in ev} >= {
        "start", "stop", "injury", "resume", "score", "warn", "period", "expire",
        "set_clock", "swap", "splash", "weight", "bout",
    }


def test_full_bout_walkthrough(clock):
    a = make(clock)
    s = state_at(a, clock, 0)
    assert (s.weight, s.bout, s.screen, s.clock_state) == ("157", "1203", "splash", "stopped")
    s = state_at(a, clock, 4)
    assert s.screen == "score" and s.period == 1 and s.clock_ms_remaining == 120000
    s = state_at(a, clock, 5)
    assert s.clock_state == "running"
    s = state_at(a, clock, 25)  # 20 s of clock gone, takedown scored at 20
    assert s.clock_ms_remaining == 100000 and s.left_score == 2
    s = state_at(a, clock, 35)
    assert s.right_score == 1
    s = state_at(a, clock, 50)
    assert (s.left_score, s.right_score) == (4, 1)
    s = state_at(a, clock, 61)
    assert s.right_warnings == 1 and s.left_warnings == 0
    s = state_at(a, clock, 80)  # injury at 70: clock frozen at 55 s
    assert s.clock_state == "injury" and s.clock_ms_remaining == 55000
    s = state_at(a, clock, 96)
    assert s.clock_state == "running" and s.clock_ms_remaining == 54000
    s = state_at(a, clock, 151)  # clock ran out by itself at t=150
    assert s.clock_state == "expired" and s.clock_ms_remaining == 0 and s.period == 1
    s = state_at(a, clock, 161)  # period break -> period 2 ready
    assert s.period == 2 and s.clock_state == "stopped" and s.clock_ms_remaining == 120000
    s = state_at(a, clock, 190)
    assert s.clock_state == "running" and s.right_score == 3
    s = state_at(a, clock, 206)  # clock edit to 75 s at t=205, stopped
    assert s.clock_state == "stopped" and s.clock_ms_remaining == 75000
    s = state_at(a, clock, 230)  # running from 210: 20 s gone
    assert s.clock_ms_remaining == 55000 and s.clock_state == "running"
    s = state_at(a, clock, 290)
    assert s.clock_state == "expired" and s.left_warnings == 1
    s = state_at(a, clock, 303)
    assert s.colors_swapped is True and s.period == 3 and s.clock_state == "stopped"
    s = state_at(a, clock, 331)
    assert s.left_score == 7 and s.clock_state == "running"
    s = state_at(a, clock, 401)
    assert s.clock_state == "expired" and s.clock_ms_remaining == 0 and s.right_score == 4
    assert not a.finished
    s = state_at(a, clock, 446)
    assert s.screen == "splash"
    assert a.finished


def test_every_state_of_the_script_is_schema_valid(clock):
    a = make(clock)
    for t in range(0, 460, 1):
        s = state_at(a, clock, t)
        validate_state_message(to_message(s, "sess", 1, 0))


def test_speed_multiplier(clock):
    a = make(clock, speed=10.0)
    clock.advance(2.5)  # = 25 script seconds
    s = run(a.read_state())
    assert s.left_score == 2 and s.clock_ms_remaining == 100000


def test_clock_precision_and_no_event_skipping(clock):
    a = make(clock)
    clock.advance(1000)  # jump far past the end in a single read
    s = run(a.read_state())
    assert a.finished and s.screen == "splash" and s.left_score == 7 and s.right_score == 4
    assert s.clock_state == "expired"


def test_sub_second_clock_samples(clock):
    a = make(clock)
    s = state_at(a, clock, 5.25)
    assert s.clock_ms_remaining == 119750


def test_loop_restarts(clock):
    a = make(clock, loop=True)
    state_at(a, clock, 446)
    assert not a.finished
    clock.advance(a.duration + 1.0)  # past the end plus the loop gap
    s = run(a.read_state())
    assert s.left_score == 0 or s.screen == "splash"  # restarted from the beginning
    clock.advance(30)
    s = run(a.read_state())
    assert s.screen == "score" and s.clock_state == "running"


def test_custom_script_actions(tmp_path, clock):
    p = write_script(tmp_path, [
        {"t": 0, "do": "period", "n": 4, "clock_ms": 30000},
        {"t": 1, "do": "start"},
        {"t": 2, "do": "swap"},
        {"t": 3, "do": "swap"},
        {"t": 4, "do": "score", "side": "left", "points": -5},
        {"t": 5, "do": "warn", "side": "right", "n": 20},
        {"t": 6, "do": "set_clock", "ms": 0},
        {"t": 7, "do": "set_clock", "seconds": 10},
    ])
    a = make(clock, p)
    s = state_at(a, clock, 2.5)
    assert s.period == 4 and s.colors_swapped and s.clock_ms_remaining == 28500
    s = state_at(a, clock, 3.5)
    assert not s.colors_swapped
    s = state_at(a, clock, 4.5)
    assert s.left_score == 0  # clamped
    s = state_at(a, clock, 5.5)
    assert s.right_warnings == 9
    s = state_at(a, clock, 6.5)
    assert s.clock_state == "expired" and s.clock_ms_remaining == 0
    s = state_at(a, clock, 7.5)
    assert s.clock_state == "stopped" and s.clock_ms_remaining == 10000


def test_start_ignored_at_zero_clock(tmp_path, clock):
    p = write_script(tmp_path, [{"t": 0, "do": "expire"}, {"t": 1, "do": "start"}])
    a = make(clock, p)
    assert state_at(a, clock, 2).clock_state == "expired"


def test_bad_script_rejected(tmp_path, clock):
    with pytest.raises(ValueError):
        MockBoutAdapter(write_script(tmp_path, {"not": "a list"}), clock=clock)
    a = make(clock, write_script(tmp_path, [{"t": 0, "do": "teleport"}]))
    with pytest.raises(ValueError):
        run(a.read_state())
    with pytest.raises(ValueError):
        MockBoutAdapter(speed=0)


def test_read_state_returns_copy(clock):
    a = make(clock)
    s1 = run(a.read_state())
    s1.left_score = 50
    assert run(a.read_state()).left_score == 0


def test_wait_change_wakes_for_next_event(clock):
    async def go():
        a = MockBoutAdapter(clock=lambda: __import__("time").monotonic(), speed=1000.0)
        await a.start()
        import time

        t = time.monotonic()
        await a.wait_change(5.0)  # next event is at script t=0 or soon after
        return time.monotonic() - t

    assert run(go()) < 1.0
