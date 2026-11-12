import json
from pathlib import Path

import pytest

from bracketboard_agent.schema import (
    BoutState,
    SchemaError,
    parse_and_validate,
    to_message,
    validate_state_message,
)

FIXTURES = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
VALID = sorted((FIXTURES / "valid").glob("*.json"))
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))


def base() -> dict:
    return json.loads((FIXTURES / "valid" / "typical.json").read_text())


def code_of(d) -> tuple[str, str | None]:
    with pytest.raises(SchemaError) as ei:
        validate_state_message(d)
    return ei.value.code, ei.value.field


def test_fixture_dirs_not_empty():
    assert len(VALID) >= 7 and len(INVALID) >= 50


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_valid_fixtures_accepted(path):
    obj = parse_and_validate(path.read_bytes())
    validate_state_message(obj)


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.name)
def test_invalid_fixtures_rejected_with_expected_code(path):
    expected = path.name.split("__", 1)[0]
    with pytest.raises(SchemaError) as ei:
        parse_and_validate(path.read_bytes())
    assert ei.value.code == expected, (path.name, ei.value)
    # str input behaves the same as bytes where the text is valid UTF-8
    with pytest.raises(SchemaError) as ei2:
        parse_and_validate(path.read_text(encoding="utf-8"))
    assert ei2.value.code == expected


def test_to_message_roundtrip_is_valid():
    st = BoutState(3, 1, 0, 1, 2, 87340, "running", "score", "157", "1203", True)
    msg = to_message(st, "abc", 7, 1763000000123)
    validate_state_message(msg)
    assert msg["type"] == "state" and msg["v"] == 1 and msg["seq"] == 7
    assert parse_and_validate(json.dumps(msg)) == msg


def test_error_field_names():
    d = base()
    del d["left_score"]
    assert code_of(d) == ("missing_field", "left_score")
    d = base()
    d["period"] = 10
    assert code_of(d) == ("out_of_range", "period")


def test_precedence_not_object_before_everything():
    with pytest.raises(SchemaError) as ei:
        validate_state_message([1])  # type: ignore[arg-type]
    assert ei.value.code == "not_object"


def test_precedence_too_large_before_not_json():
    with pytest.raises(SchemaError) as ei:
        parse_and_validate(b"x" * 1025)
    assert ei.value.code == "too_large"
    with pytest.raises(SchemaError) as ei:
        parse_and_validate("x" * 1024)
    assert ei.value.code == "not_json"


def test_too_large_counts_bytes_not_chars():
    d = base()
    d["pad"] = "é" * 600  # 600 chars, 1200 bytes in UTF-8
    with pytest.raises(SchemaError) as ei:
        parse_and_validate(json.dumps(d, ensure_ascii=False))
    assert ei.value.code == "too_large"


def test_exactly_1024_bytes_ok():
    d = base()
    raw = json.dumps(d, separators=(",", ":"))
    pad = 1024 - len(raw) - len(',"pad":""')
    d["pad"] = "x" * pad
    raw = json.dumps(d, separators=(",", ":"))
    assert len(raw.encode()) == 1024
    parse_and_validate(raw)


def test_precedence_wrong_type_before_unsupported_version_and_fields():
    d = base()
    d["type"] = "hello"
    d["v"] = 2
    del d["seq"]
    assert code_of(d)[0] == "wrong_type"
    d = base()
    del d["type"]
    assert code_of(d)[0] == "wrong_type"
    d = base()
    d["type"] = 5
    assert code_of(d)[0] == "wrong_type"


def test_precedence_unsupported_version_before_field_errors():
    d = base()
    d["v"] = 2
    d["left_score"] = "x"
    del d["session"]
    assert code_of(d) == ("unsupported_version", "v")


def test_version_missing_and_non_int():
    d = base()
    del d["v"]
    assert code_of(d) == ("missing_field", "v")
    d = base()
    d["v"] = "1"
    assert code_of(d) == ("bad_type", "v")
    d["v"] = True  # bool is not an int
    assert code_of(d) == ("bad_type", "v")
    d["v"] = 1.5
    assert code_of(d) == ("bad_type", "v")


def test_per_field_errors_in_table_order():
    d = base()
    d["weight"] = 5  # later in table
    d["seq"] = -1  # earlier in table
    assert code_of(d) == ("out_of_range", "seq")
    d = base()
    d["bout"] = "123456789"
    d["screen"] = "menu"
    assert code_of(d) == ("out_of_range", "screen")
    d = base()
    del d["colors_swapped"]
    d["period"] = 0
    assert code_of(d) == ("out_of_range", "period")


def test_bool_is_not_int():
    for f in ("seq", "left_score", "period", "clock_ms_remaining", "sender_ts"):
        d = base()
        d[f] = True
        assert code_of(d) == ("bad_type", f)


def test_non_integral_numbers_bad_type():
    for val in (1.5, float("nan"), float("inf")):
        d = base()
        d["left_score"] = val
        assert code_of(d) == ("bad_type", "left_score")


def test_integral_float_accepted_like_cjson():
    # Documented behaviour: 3.0 is an integral JSON number, so it is accepted (cJSON cannot tell
    # 3 from 3.0). The agent never emits floats; this only concerns foreign messages.
    d = base()
    d["left_score"] = 3.0
    validate_state_message(d)
    d["left_score"] = 100.0
    assert code_of(d) == ("out_of_range", "left_score")


def test_non_standard_json_constants_are_not_json():
    raw = json.dumps(base()).replace('"left_score": 3', '"left_score": NaN')
    with pytest.raises(SchemaError) as ei:
        parse_and_validate(raw)
    assert ei.value.code == "not_json"


def test_invalid_utf8_is_not_json():
    with pytest.raises(SchemaError) as ei:
        parse_and_validate(b'{"type":"state","x":"\xff"}')
    assert ei.value.code == "not_json"


def test_duplicate_keys_last_wins():
    raw = json.dumps(base()).replace('"left_score": 3', '"left_score": 500, "left_score": 4')
    assert parse_and_validate(raw)["left_score"] == 4


def test_boundaries():
    d = base()
    for f, hi in (("left_score", 99), ("left_warnings", 9), ("period", 9), ("clock_ms_remaining", 3600000)):
        d[f] = hi
        validate_state_message(d)
        d[f] = hi + 1
        assert code_of(d) == ("out_of_range", f)
        d[f] = hi
    d["period"] = 0
    assert code_of(d) == ("out_of_range", "period")


def test_session_and_label_rules():
    d = base()
    for bad in ("", "x" * 17, "a b", "a.b"):
        d["session"] = bad
        assert code_of(d) == ("out_of_range", "session")
    d["session"] = "A_b-9"
    validate_state_message(d)
    d["weight"] = "\x7f"
    assert code_of(d) == ("out_of_range", "weight")
    d["weight"] = " ~ "
    validate_state_message(d)
    d["weight"] = "café"
    assert code_of(d) == ("out_of_range", "weight")


def test_enum_wrong_json_type_vs_value():
    d = base()
    d["clock_state"] = 1
    assert code_of(d) == ("bad_type", "clock_state")
    d["clock_state"] = "paused"
    assert code_of(d) == ("out_of_range", "clock_state")


def test_unknown_fields_ignored():
    d = base()
    d.update({"left_name": "Smith", "aux": {"a": 1}, "anything": [1, 2]})
    validate_state_message(d)
