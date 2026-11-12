"""Controller timer -> BoutState: every row of docs/usab-controller-protocol.md section 6, plus edge cases."""
import math

import pytest

from bracketboard_agent.schema import to_message, validate_state_message
from bracketboard_agent.usab.mapping import (
    MappingConfig,
    StateMapper,
    classify_color,
    lenient_score,
    parse_period,
    printable,
)
from bracketboard_agent.usab.model import BoutInfo, ControllerTimer

BOUT = BoutInfo(weight_name="157", bout_number="1203", match_id="9001")


def T(**kw) -> ControllerTimer:
    base = dict(
        name="Period 1", current_time=120.0, running=0, updated_millis=1_000_000, system_millis=1_000_000,
        bout_id="1234", event_id="77", w1_color="FF0000", w2_color="0000FF", w1_score="3", w2_score="1",
    )
    base.update(kw)
    return ControllerTimer(**base)


def M(timer, *, bout=BOUT, now=0.0, rx=0.0, mapper=None, **cfg):
    m = mapper or StateMapper(MappingConfig(**cfg))
    st = m.map(timer, rx, now, bout)
    validate_state_message(to_message(st, "sess", 1, 0))  # whatever we produce must be schema-valid
    return st


# --- color classification --------------------------------------------------------------------------
@pytest.mark.parametrize("c,expected", [
    ("FF0000", "red"), ("ff0000", "red"), ("#FF0000", "red"), ("f00", "red"), ("#F00", "red"), ("D00000", "red"),
    ("FF1A1A", "red"), ("E6194B", "red"), ("red", "red"), ("RED", "red"),
    ("00FF00", "green"), ("00AA00", "green"), ("0A7B2E", "green"), ("0f0", "green"), ("green", "green"),
    ("0000FF", "blue"), ("1E3A8A", "blue"), ("00f", "blue"), ("3366CC", "blue"), ("blue", "blue"),
    ("black", "other"), ("000000", "other"), ("", "other"), ("  ", "other"), ("FFFFFF", "other"), ("808080", "other"),
    ("FFFF00", "other"), ("FFA500", "other"), ("800080", "other"), ("00FFFF", "other"), ("zzzzzz", "other"),
    ("12345", "other"), ("1234567", "other"), ("#", "other"), ("null", "other"), ("0f", "other"),
])
def test_classify_color(c, expected):
    assert classify_color(c) == expected


def test_classify_color_non_string():
    assert classify_color(None) == "other"  # type: ignore[arg-type]
    assert classify_color(123) == "other"  # type: ignore[arg-type]


# --- left / right ----------------------------------------------------------------------------------
def test_red_w1_left():
    st = M(T())
    assert (st.left_score, st.right_score) == (3, 1) and st.colors_swapped is False


def test_red_w2_goes_left():
    st = M(T(w1_color="0000FF", w2_color="FF0000"))
    assert (st.left_score, st.right_score) == (1, 3)


def test_green_vs_red_red_left():
    st = M(T(w1_color="00AA00", w2_color="FF0000", w1_score="6", w2_score="2"))
    assert (st.left_score, st.right_score) == (2, 6)


@pytest.mark.parametrize("c1,c2", [("0000FF", "00AA00"), ("black", "black"), ("", ""), ("FFFF00", "800080")])
def test_no_red_left_is_w1(c1, c2):
    st = M(T(w1_color=c1, w2_color=c2))
    assert (st.left_score, st.right_score) == (3, 1)


def test_both_red_left_is_w1():
    assert (M(T(w1_color="FF0000", w2_color="CC0000")).left_score) == 3


def test_swap_sides_flips():
    st = M(T(), swap_sides=True)
    assert (st.left_score, st.right_score) == (1, 3) and st.colors_swapped is False
    st = M(T(w1_color="0000FF", w2_color="FF0000"), swap_sides=True)
    assert (st.left_score, st.right_score) == (3, 1)
    st = M(T(w1_color="", w2_color=""), swap_sides=True)
    assert (st.left_score, st.right_score) == (1, 3)


def test_warnings_are_zero():
    st = M(T())
    assert (st.left_warnings, st.right_warnings) == (0, 0)


# --- scores, leniently -----------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("3", 3), (" 7 ", 7), ("0", 0), ("12.0", 12), ("3.9", 3), ("+5", 5), ("-4", 0), ("-0", 0), ("99", 99), ("100", 99),
    ("1e3", 0), ("abc", 0), ("", 0), ("NaN", 0), ("inf", 0), ("Infinity", 0), ("9" * 400, 99), ("-" + "9" * 400, 0),
    ("3 4", 0), ("0x10", 0), (None, 0), ("٣", 0),
])
def test_lenient_score(raw, expected):
    assert lenient_score(raw) == expected


def test_lenient_score_typed_inputs():
    assert lenient_score(5) == 5 and lenient_score(250) == 99 and lenient_score(-3) == 0
    assert lenient_score(7.9) == 7 and lenient_score(math.nan) == 0 and lenient_score(math.inf) == 0
    assert lenient_score(True) == 1


def test_scores_hostile_text_in_timer():
    st = M(T(w1_score="\x00\x1b[31m9", w2_score="9" * 500))
    assert (st.left_score, st.right_score) == (0, 99)


# --- period ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("Period 1", 1), ("Period 2", 2), ("Period 3", 3), ("period 3", 3), ("PERIOD 2", 2), ("Period2", 2),
    ("Period 0", 1), ("Period 12", 9), ("Period 99999999999999999999", 9),
    ("Sudden Victory", 4), ("sudden victory", 4), ("SV", 4), ("SV-1", 4), ("sv 1", 4),
    ("Tie Breaker 1", 5), ("Tie Breaker 2", 6), ("Tie Breaker", 5), ("TB Choice 1", 5), ("TB Choice 2", 6), ("TB 3", 7),
    ("Tie Breaker 40", 9), ("UTB Choice", 7), ("UTB", 7), ("utb 1", 7),
    ("Break", None), ("Blood", None), ("Injury", None), ("Recovery", None), ("Head/Neck", None), ("", None),
    ("Overtime", None), ("Svelte", None), ("Practice Period 2", None),
])
def test_parse_period(name, expected):
    assert parse_period(name) == expected


def test_period_in_state_and_holds_across_non_period_timers():
    m = StateMapper()
    assert M(T(name="Period 2"), mapper=m).period == 2
    assert M(T(name="Break"), mapper=m).period == 2
    assert M(T(name="Blood"), mapper=m).period == 2
    assert M(T(name="Injury"), mapper=m).period == 2
    assert M(T(name="Head/Neck"), mapper=m).period == 2
    assert M(T(name="Period 3"), mapper=m).period == 3
    assert M(T(name="Break"), mapper=m).period == 3
    assert M(T(name="Sudden Victory"), mapper=m).period == 4
    assert M(T(name="Recovery"), mapper=m).period == 4
    assert M(T(name="Tie Breaker 2"), mapper=m).period == 6


def test_period_defaults_to_one_for_leading_non_period_timer():
    assert M(T(name="Break")).period == 1


def test_period_resets_when_bout_changes():
    m = StateMapper()
    assert M(T(name="Period 3", bout_id="1"), mapper=m).period == 3
    assert M(T(name="Break", bout_id="2"), mapper=m).period == 1
    assert M(T(name="Period 2", bout_id="2"), mapper=m).period == 2
    assert M(T(name="Break", bout_id="2"), mapper=m).period == 2


def test_period_overrides():
    cfg = MappingConfig(period_overrides={"Overtime": 4, "tb choice 1": 7, "Period 3": 8})
    m = StateMapper(cfg)
    assert M(T(name="Overtime"), mapper=m).period == 4
    assert M(T(name="Break"), mapper=m).period == 4  # override also updates the remembered period
    assert M(T(name="TB Choice 1"), mapper=m).period == 7
    assert M(T(name="Period 3"), mapper=m).period == 8
    assert M(T(name="Period 2"), mapper=m).period == 2  # unmatched names fall back to parsing


# --- clock and clock_state -------------------------------------------------------------------------
def test_stopped_clock():
    st = M(T(current_time=87.34))
    assert (st.clock_ms_remaining, st.clock_state) == (87_340, "stopped")


def test_running_clock_extrapolates_and_state_is_running():
    t = T(current_time=100.0, running=1, system_millis=1_000_200)
    st = M(t, rx=50.0, now=52.0)
    assert (st.clock_ms_remaining, st.clock_state) == (97_800, "running")


def test_running_clock_reaching_zero_is_expired():
    st = M(T(current_time=5.0, running=1), rx=0.0, now=5.0)
    assert (st.clock_ms_remaining, st.clock_state) == (0, "expired")
    st = M(T(current_time=5.0, running=1), rx=0.0, now=60.0)
    assert (st.clock_ms_remaining, st.clock_state) == (0, "expired")
    st = M(T(current_time=5.0, running=1), rx=0.0, now=4.9)
    assert st.clock_state == "running" and st.clock_ms_remaining == 100


def test_stopped_at_zero_is_expired():
    st = M(T(current_time=0.0, running=0))
    assert (st.clock_ms_remaining, st.clock_state) == (0, "expired")


def test_injury_state_is_never_used():
    for name in ("Blood", "Injury", "Recovery", "Head/Neck"):
        for running in (0, 1):
            st = M(T(name=name, current_time=60.0, running=running))
            assert st.clock_state in ("running", "stopped")
            assert st.clock_label == name


def test_wrestler_timer_clock_is_the_current_timers_clock():
    st = M(T(name="Blood", current_time=300.0, running=1), rx=0.0, now=10.0)
    assert st.clock_ms_remaining == 290_000 and st.clock_state == "running"


def test_offset_config_applies():
    t = T(current_time=100.0, running=1, system_millis=1_000_500)
    assert M(t, controller_offset_ms=0).clock_ms_remaining == 99_500
    assert M(t, controller_offset_ms=200).clock_ms_remaining == 99_700


def test_hostile_times():
    assert M(T(current_time=math.nan)).clock_ms_remaining == 0
    assert M(T(current_time=math.inf)).clock_ms_remaining == 0
    assert M(T(current_time=-12.0)).clock_ms_remaining == 0
    assert M(T(current_time=1e300)).clock_ms_remaining == 3_600_000
    assert M(T(current_time=1e300)).clock_state == "stopped"


# --- screen, weight, bout --------------------------------------------------------------------------
def test_splash_when_no_bout():
    st = M(T(bout_id=""))
    assert st.screen == "splash" and st.weight == "" and st.bout == ""
    st = M(T(bout_id="   "))
    assert st.screen == "splash"


def test_score_screen_and_bout_info():
    st = M(T())
    assert (st.screen, st.weight, st.bout) == ("score", "157", "1203")


def test_bout_info_missing_is_empty_strings():
    st = M(T(), bout=None)
    assert (st.screen, st.weight, st.bout) == ("score", "", "")


def test_bout_number_falls_back_to_m_plus_match_id():
    st = M(T(), bout=BoutInfo("125", "", "9001"))
    assert st.bout == "m9001"
    st = M(T(), bout=BoutInfo("125", "", ""))
    assert st.bout == ""


def test_weight_and_bout_truncated_and_cleaned():
    st = M(T(), bout=BoutInfo("HWT-Heavyweight", "1234567890", ""))
    assert (st.weight, st.bout) == ("HWT-Heav", "12345678")
    st = M(T(), bout=BoutInfo("1\x005\x1b7\n\t", "é中12\x7f3", ""))
    assert (st.weight, st.bout) == ("157", "123")
    st = M(T(), bout=BoutInfo("  125  ", "ab cd", ""))
    assert (st.weight, st.bout) == ("125", "ab cd")
    st = M(T(), bout=BoutInfo("éé", "\x00", ""))
    assert (st.weight, st.bout) == ("", "")


def test_printable_helper():
    assert printable("a\x00b☃c", 8) == "abc"
    assert printable("abcdefghij", 3) == "abc"
    assert printable("ab   cd", 3) == "ab"  # no trailing space after truncation
    assert printable(None, 3) == ""  # type: ignore[arg-type]


# --- extension fields ------------------------------------------------------------------------------
def test_clock_label_truncated_and_ascii():
    assert M(T(name="Period 2")).clock_label == "Period 2"
    assert M(T(name="Sudden Victory")).clock_label == "Sudden Victor"[:12]
    assert M(T(name="Café\x00 Break\x1b")).clock_label == "Caf Break"
    assert len(M(T(name="x" * 200)).clock_label) == 12
    assert M(T(name="")).clock_label == ""


def test_blink_none_by_default_and_for_unlabelled_wrestler_timers():
    for name in ("Period 1", "Break", "Blood", "Injury", "Recovery", "Head/Neck", "Sudden Victory"):
        assert M(T(name=name)).blink == "none"


@pytest.mark.parametrize("name,w1c,w2c,expected", [
    ("Blood W1", "FF0000", "0000FF", "left"),
    ("Blood W2", "FF0000", "0000FF", "right"),
    ("Injury (1)", "FF0000", "0000FF", "left"),
    ("Recovery #2", "FF0000", "0000FF", "right"),
    ("Blood W1", "0000FF", "FF0000", "right"),  # w1 is on the right
    ("Blood wrestler 2", "0000FF", "FF0000", "left"),
])
def test_blink_inferred_only_when_name_carries_owner(name, w1c, w2c, expected):
    assert M(T(name=name, w1_color=w1c, w2_color=w2c)).blink == expected


def test_blink_not_inferred_from_period_names():
    assert M(T(name="Period 2")).blink == "none"
    assert M(T(name="Tie Breaker 1")).blink == "none"


def test_riding_absent_without_riding():
    st = M(T())
    assert st.riding_ms is None and st.riding_dir is None


def test_riding_signed_and_direction_w1_left():
    st = M(T(riding_time=12.0, w1_position="top"))
    assert (st.riding_ms, st.riding_dir) == (12_000, 1)
    st = M(T(riding_time=-7.5, w1_position="bottom"))
    assert (st.riding_ms, st.riding_dir) == (-7_500, -1)
    st = M(T(riding_time=3.0, w1_position="neutral"))
    assert (st.riding_ms, st.riding_dir) == (3_000, 0)
    st = M(T(riding_time=0.0, w1_position="TOP"))
    assert (st.riding_ms, st.riding_dir) == (0, 1)


def test_riding_flips_when_w1_is_right_or_sides_swapped():
    w1_right = dict(w1_color="0000FF", w2_color="FF0000")
    st = M(T(riding_time=12.0, w1_position="top", **w1_right))
    assert (st.riding_ms, st.riding_dir) == (-12_000, -1)  # w1 ahead and on the right -> left is behind
    st = M(T(riding_time=-4.0, w1_position="bottom", **w1_right))
    assert (st.riding_ms, st.riding_dir) == (4_000, 1)  # w2 (left) on top and accumulating
    st = M(T(riding_time=12.0, w1_position="top"), swap_sides=True)
    assert (st.riding_ms, st.riding_dir) == (-12_000, -1)
    st = M(T(riding_time=12.0, w1_position="top", **w1_right), swap_sides=True)
    assert (st.riding_ms, st.riding_dir) == (12_000, 1)


def test_riding_extrapolates_while_running_in_the_right_direction():
    t = T(riding_time=10.0, w1_position="top", running=1, system_millis=1_000_500)
    st = M(t, rx=0.0, now=2.0)  # 500 ms at response + 2 s local = 2.5 s
    assert st.riding_ms == 12_500 and st.riding_dir == 1
    t = T(riding_time=10.0, w1_position="bottom", running=1, system_millis=1_000_500)
    assert M(t, rx=0.0, now=2.0).riding_ms == 7_500
    t = T(riding_time=10.0, w1_position="top", running=1, w1_color="0000FF", w2_color="FF0000")
    assert M(t, rx=0.0, now=2.0).riding_ms == -12_000  # w1 on the right accumulating -> left falls behind
    t = T(riding_time=10.0, w1_position="top", running=0)
    assert M(t, rx=0.0, now=2.0).riding_ms == 10_000
    t = T(riding_time=10.0, w1_position="neutral", running=1)
    assert M(t, rx=0.0, now=2.0).riding_ms == 10_000


def test_riding_stops_when_the_clock_expires():
    t = T(current_time=3.0, riding_time=10.0, w1_position="top", running=1)
    assert M(t, rx=0.0, now=30.0).riding_ms == 13_000


def test_riding_clamped_and_hostile():
    assert M(T(riding_time=1e9, w1_position="top")).riding_ms == 3_600_000
    assert M(T(riding_time=-1e9, w1_position="top")).riding_ms == -3_600_000
    st = M(T(riding_time=math.nan, w1_position="top"))
    assert st.riding_ms == 0 and st.riding_dir == 1
    assert M(T(riding_time=math.inf, w1_position="nonsense")).riding_ms is None


def test_shot_clock_absent_without_one():
    st = M(T())
    assert st.shot_clock_ms is None and st.shot_clock_side is None
    assert M(T(wrestler_indicator="0")).shot_clock_ms is None


def test_shot_clock_side_follows_wrestler_indicator_and_left_right_rule():
    st = M(T(shot_clock=24.99, wrestler_indicator="1"))
    assert (st.shot_clock_ms, st.shot_clock_side) == (24_990, "left")
    st = M(T(shot_clock=24.99, wrestler_indicator="2"))
    assert st.shot_clock_side == "right"
    st = M(T(shot_clock=10.0, wrestler_indicator="1", w1_color="0000FF", w2_color="FF0000"))
    assert st.shot_clock_side == "right"
    st = M(T(shot_clock=10.0, wrestler_indicator="1"), swap_sides=True)
    assert st.shot_clock_side == "right"
    st = M(T(shot_clock=10.0, wrestler_indicator=""))
    assert (st.shot_clock_ms, st.shot_clock_side) == (10_000, "none")
    st = M(T(shot_clock=0.0, wrestler_indicator="2"))
    assert (st.shot_clock_ms, st.shot_clock_side) == (0, "right")


def test_shot_clock_counts_down_while_running_and_clamps():
    t = T(shot_clock=30.0, wrestler_indicator="1", running=1, system_millis=1_000_250)
    assert M(t, rx=0.0, now=2.0).shot_clock_ms == 27_750
    assert M(t, rx=0.0, now=100.0).shot_clock_ms == 0
    t = T(shot_clock=30.0, wrestler_indicator="1", running=0)
    assert M(t, rx=0.0, now=100.0).shot_clock_ms == 30_000
    assert M(T(shot_clock=1e12, wrestler_indicator="1")).shot_clock_ms == 3_600_000
    assert M(T(shot_clock=math.nan, wrestler_indicator="2")).shot_clock_ms == 0


# --- whole-message hostility -----------------------------------------------------------------------
def test_hostile_strings_everywhere_still_validate():
    t = T(
        name="\x00\x07‮\U0001f600 Period\n9\r\t" * 3, bout_id="\x00" * 5 + "1", w1_color="\x00zz", w2_color="‮",
        w1_score="\ud800", w2_score="\x7f", w1_position="\x00top", wrestler_indicator="¹",
        message="\x00" * 100, current_time=math.nan, riding_time=math.inf, shot_clock=-math.inf,
    )
    st = M(t, bout=BoutInfo("\x00" * 50, "\U0001f600" * 50, "\x1b" * 50))
    assert st.period == 1 and st.screen == "score"
    assert all(" " <= c <= "~" for c in st.clock_label + st.weight + st.bout)
