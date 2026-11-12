"""remaining_ms / age_ms: spec section 5, with injected times."""
import math

import pytest

from bracketboard_agent.usab.clock import MAX_CLOCK_MS, age_ms, remaining_ms
from bracketboard_agent.usab.model import ControllerTimer


def T(**kw) -> ControllerTimer:
    base = dict(current_time=100.0, running=1, updated_millis=1_000_000, system_millis=1_000_000)
    base.update(kw)
    return ControllerTimer(**base)


def test_stopped_clock_does_not_move():
    t = T(running=0, system_millis=1_005_000)
    assert remaining_ms(t, 10.0, 10.0) == 100_000
    assert remaining_ms(t, 10.0, 500.0) == 100_000


def test_running_local_elapsed_only():
    t = T()
    assert remaining_ms(t, 10.0, 10.0) == 100_000
    assert remaining_ms(t, 10.0, 12.5) == 97_500


def test_running_controller_age_at_response_counts():
    t = T(system_millis=1_000_300)  # sample was 300 ms old when the server answered
    assert remaining_ms(t, 10.0, 10.0) == 99_700
    assert remaining_ms(t, 10.0, 11.0) == 98_700


def test_offset_is_subtracted_from_controller_age_and_floors_at_zero():
    t = T(system_millis=1_000_300)
    assert remaining_ms(t, 10.0, 10.0, controller_offset_ms=100) == 99_800
    assert remaining_ms(t, 10.0, 10.0, controller_offset_ms=300) == 100_000
    assert remaining_ms(t, 10.0, 10.0, controller_offset_ms=5_000) == 100_000  # never negative age
    assert remaining_ms(t, 10.0, 11.0, controller_offset_ms=5_000) == 99_000  # local elapsed still counts


def test_negative_controller_age_is_clamped():
    t = T(system_millis=999_000)  # server clock behind the controller stamp
    assert age_ms(t, 0.0, 0.0) == 0.0


def test_unknown_stamps_mean_zero_controller_age():
    assert age_ms(T(updated_millis=0), 5.0, 5.0) == 0.0
    assert age_ms(T(system_millis=0), 5.0, 5.0) == 0.0
    assert remaining_ms(T(updated_millis=0, system_millis=1_763_000_000_000), 5.0, 6.0) == 99_000


def test_clamp_to_zero_and_upper_bound():
    assert remaining_ms(T(current_time=1.0), 0.0, 5.0) == 0
    assert remaining_ms(T(current_time=1.0, running=0), 0.0, 5.0) == 1000
    assert remaining_ms(T(current_time=1e9, running=0), 0.0, 0.0) == MAX_CLOCK_MS
    assert remaining_ms(T(current_time=1e308), 0.0, 0.0) == MAX_CLOCK_MS  # *1000 overflows to inf
    assert remaining_ms(T(current_time=-5.0, running=0), 0.0, 0.0) == 0


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_current_time_is_zero(bad):
    assert remaining_ms(T(current_time=bad), 0.0, 0.0) == 0
    assert remaining_ms(T(current_time=bad, running=0), 0.0, 0.0) == 0


def test_monotonic_going_backwards_does_not_add_time():
    t = T()
    assert remaining_ms(t, 10.0, 9.0) == 100_000


def test_running_flag_must_be_exactly_one():
    assert remaining_ms(T(running=2), 0.0, 10.0) == 100_000
    assert remaining_ms(T(running=0), 0.0, 10.0) == 100_000


def test_result_is_int_and_rounded():
    r = remaining_ms(T(current_time=10.0004), 0.0, 0.0)
    assert isinstance(r, int) and r == 10_000
    assert remaining_ms(T(current_time=10.0006, running=0), 0.0, 0.0) == 10_001


def test_extreme_ages():
    t = T(system_millis=1_000_000 + 10 * 3_600_000)
    assert remaining_ms(t, 0.0, 0.0) == 0
    assert isinstance(age_ms(T(system_millis=2**62), 0.0, 0.0), float)
