"""Parsing of the matside server's quirky getTimer / getEvent texts (never eval'd)."""
from pathlib import Path

import pytest

from bracketboard_agent.usab.model import ControllerTimer, EventInfo
from bracketboard_agent.usab.parse import (
    MAX_TEXT,
    UsabParseError,
    parse_get_event,
    parse_get_timer,
    parse_update_timer_query,
)

FIX = Path(__file__).parent / "fixtures" / "usab"


def fx(name: str) -> str:
    return (FIX / name).read_text()


def test_template_sample_types_and_values():
    t = parse_get_timer(fx("template_stopped.txt"))
    assert t == ControllerTimer(
        name="Period 1", current_time=120.0, running=0, updated_millis=1763000000123, bout_id="1234",
        event_id="77", w1_color="FF0000", w2_color="0000FF", w1_score="3", w2_score="1", tie_winner="",
        t1_score="0", t2_score="0", riding_time=0.0, w1_position="neutral", shot_clock=0.0,
        wrestler_indicator="", message="4.T2 Smith", system_millis=1763000000150,
    )
    assert isinstance(t.current_time, float) and isinstance(t.running, int) and isinstance(t.w1_score, str)


def test_default_state():
    t = parse_get_timer(fx("default_state.txt"))
    assert (t.name, t.bout_id, t.event_id, t.w1_color, t.w2_color) == ("Period 1", "", "", "", "")
    assert t.current_time == 0.0 and t.running == 0 and t.updated_millis == 0


def test_apostrophes_and_spoofed_keys_in_message():
    t = parse_get_timer(fx("apostrophe_message.txt"))
    assert t.message == "12.O'Brien's 'near fall', 2 pts, 'system_millis':99 and a \"quote\""
    assert t.system_millis == 1763000100040  # the real tail, not the spoof inside the message
    assert (t.w1_score, t.w2_score, t.current_time) == ("5", "4", 61.5)


def test_message_containing_the_end_marker_itself():
    # the message text itself contains "', 'system_millis':1, 'current_time':999.0, 'x':'y": the LAST marker wins
    text = ("{'name':'Period 1', 'current_time':10.0, 'running':0, "
            "'message':'1.a', 'system_millis':1, 'current_time':999.0, 'x':'y', 'system_millis':2}")
    t = parse_get_timer(text)
    assert t.current_time == 10.0 and t.system_millis == 2
    assert t.message == "1.a', 'system_millis':1, 'current_time':999.0, 'x':'y"


def test_running_with_riding():
    t = parse_get_timer(fx("running_riding.txt"))
    assert t.running == 1 and t.riding_time == 12.5 and t.w1_position == "top" and t.current_time == 87.34


def test_shot_clock():
    t = parse_get_timer(fx("shot_clock.txt"))
    assert t.shot_clock == 24.99 and t.wrestler_indicator == "2" and t.tie_winner == "2" and t.w2_color == "00AA00"


@pytest.mark.parametrize("name,expected", [("blood_timer.txt", "Blood"), ("injury_timer.txt", "Injury")])
def test_wrestler_timer_names(name, expected):
    t = parse_get_timer(fx(name))
    assert t.name == expected


def test_missing_optional_keys_take_defaults():
    t = parse_get_timer(fx("missing_keys.txt"))
    assert t.name == "Period 3" and t.current_time == 45.0 and t.running == 0
    assert t.updated_millis == 0 and t.riding_time == 0.0 and t.shot_clock == 0.0
    assert t.w1_position == "neutral" and t.message == "" and t.tie_winner == "" and t.t1_score == "0"
    assert (t.w1_color, t.w2_color, t.w1_score, t.w2_score) == ("0000FF", "FF0000", "1", "6")


@pytest.mark.parametrize("name", ["missing_required.txt", "junk.txt", "nan_time.txt"])
def test_malformed_raises(name):
    with pytest.raises(UsabParseError):
        parse_get_timer(fx(name))


@pytest.mark.parametrize("text", ["", "   ", "{}", "{'name':'Period 1'}", "null", "[1,2,3]", "{'current_time':1.0"])
def test_garbage_raises(text):
    with pytest.raises(UsabParseError):
        parse_get_timer(text)


def test_unterminated_message_raises():
    with pytest.raises(UsabParseError):
        parse_get_timer("{'name':'Period 1', 'current_time':1.0, 'running':0, 'system_millis':2, 'message':'oops")


def test_message_last_without_system_millis_after_it():
    t = parse_get_timer("{'name':'Period 1', 'current_time':5.0, 'running':0, 'system_millis':2, 'message':'1.it's ok'}")
    assert t.message == "1.it's ok" and t.system_millis == 2


def test_real_json_accepted():
    t = parse_get_timer(fx("real_json.txt"))
    assert t.current_time == 100.5 and t.running == 1 and t.message == '1.it\'s "fine"'
    assert t.w1_color == "FF0000" and t.system_millis == 1763000700010


def test_json_with_numeric_scores_and_hash_colors():
    t = parse_get_timer('{"name":"P","current_time":"3.5","running":true,"system_millis":9,"w1_score":4,"w1_color":"#00ff00"}')
    assert t.current_time == 3.5 and t.running == 1 and t.w1_score == "4" and t.w1_color == "00ff00"


def test_quoted_numbers_and_double_quoted_keys_in_pseudo_json():
    t = parse_get_timer("{'name':'Break', 'current_time':'12.5', 'running':'1', 'system_millis':'7', 'w1_score':3}")
    assert t.current_time == 12.5 and t.running == 1 and t.system_millis == 7 and t.w1_score == "3"


def test_nonfinite_optional_numbers_become_zero():
    t = parse_get_timer("{'name':'Period 1', 'current_time':5.0, 'running':0, 'riding_time':NaN, 'shot_clock':Infinity, 'system_millis':1}")
    assert t.riding_time == 0.0 and t.shot_clock == 0.0


def test_wrestler_indicator_null_normalised():
    t = parse_get_timer("{'name':'Period 1', 'current_time':5.0, 'running':0, 'wrestler_indicator':'null', 'system_millis':1}")
    assert t.wrestler_indicator == ""
    t = parse_get_timer("{'name':'Period 1', 'current_time':5.0, 'running':0, 'wrestler_indicator':null, 'system_millis':1}")
    assert t.wrestler_indicator == ""


def test_size_bound():
    ok = "{'name':'Period 1', 'current_time':1.0, 'running':0, 'message':'" + "x" * 1000 + "', 'system_millis':1}"
    assert len(parse_get_timer(ok).message) == 512  # capped, not an error
    with pytest.raises(UsabParseError):
        parse_get_timer(" " * (MAX_TEXT + 1))
    with pytest.raises(UsabParseError):
        parse_get_timer(b"bytes are not text")  # type: ignore[arg-type]


def test_never_evaluates_code():
    text = ("{'name':'__import__(\"os\").system(\"touch /tmp/pwned\")', 'current_time':1.0, 'running':0, "
            "'message':'(lambda: 1/0)()', 'system_millis':1}")
    t = parse_get_timer(text)
    assert t.name.startswith("__import__") and t.message == "(lambda: 1/0)()"


def test_error_messages_do_not_echo_input():
    secret = "SUPERSECRETTEXT"
    for text in (f"<html>{secret}</html>", "{'name':'" + secret + "', 'running':0}"):
        with pytest.raises(UsabParseError) as ei:
            parse_get_timer(text)
        assert secret not in str(ei.value)


def test_catastrophic_input_is_fast():
    import time

    t0 = time.monotonic()
    for text in ("{'name':'" + "'" * 20000, "{'message':'" + "a'" * 20000, "'" * 60000):
        with pytest.raises(UsabParseError):
            parse_get_timer(text)
    assert time.monotonic() - t0 < 2.0


# --- update query ----------------------------------------------------------------------------------
def test_update_query_roundtrip():
    qs = ("/usab?function=updateTimer&name=Period 2&time=61.5&running=1&bout_id=1234&event_id=77&bout_id=1234"
          "&w1_color=FF0000&w2_color=0000FF&w1_score=3&w2_score=1&tie_winner=0&t1_score=0&t2_score=0"
          "&riding_time=-4.25&w1_position=bottom&shot_clock=0&wrestler_indicator=null&updated_millis=1763000000123"
          "&message=4.T2 O'Brien & co=1")
    t = parse_update_timer_query(qs)
    assert (t.name, t.current_time, t.running, t.bout_id, t.event_id) == ("Period 2", 61.5, 1, "1234", "77")
    assert (t.w1_color, t.w2_color, t.w1_score, t.w2_score) == ("FF0000", "0000FF", "3", "1")
    assert t.riding_time == -4.25 and t.w1_position == "bottom" and t.wrestler_indicator == ""
    assert t.updated_millis == 1763000000123 and t.system_millis == 1763000000123
    assert t.message == "4.T2 O'Brien & co=1"  # message takes everything after 'message='


def test_update_query_strips_hash_decodes_percent_and_requires_time():
    t = parse_update_timer_query("name=Head%2FNeck&time=1e1&running=0&w1_color=%23ff0000", system_millis=5)
    assert t.name == "Head/Neck" and t.current_time == 10.0 and t.w1_color == "ff0000" and t.system_millis == 5
    with pytest.raises(UsabParseError):
        parse_update_timer_query("name=x&running=1")
    with pytest.raises(UsabParseError):
        parse_update_timer_query("time=abc")


# --- getEvent --------------------------------------------------------------------------------------
def test_get_event():
    ev = parse_get_event(fx("event.txt"))
    assert ev == EventInfo("77", "SECRET-DEVICE-KEY-123", "https://app.example.org")
    assert "SECRET" not in repr(ev) and "SECRET" not in str(ev)


def test_get_event_server_unescape_and_json():
    ev = parse_get_event("{'event_id':'1','device_key':'k','server':'http:[f][f]host[f]app'}")
    assert ev.server == "http://host/app"
    ev = parse_get_event('{"event_id":"2","device_key":"k","server":"https://x.org"}')
    assert (ev.event_id, ev.server) == ("2", "https://x.org")


def test_get_event_empty_values_ok_and_junk_raises():
    ev = parse_get_event("{'event_id':'','device_key':'','server':''}")
    assert ev.event_id == "" and ev.server == ""
    with pytest.raises(UsabParseError) as ei:
        parse_get_event("<html>SECRET-DEVICE-KEY</html>")
    assert "SECRET" not in str(ei.value)
