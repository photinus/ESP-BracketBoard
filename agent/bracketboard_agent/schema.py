"""BracketBoard state snapshot v1: dataclass, validation and message building.

The validation here mirrors the firmware parser (``core/src/snapshot.cpp``) and the error
precedence documented in ``docs/protocol-v1.md``:

    too_large > not_json > not_object > wrong_type > unsupported_version > per-field (table order)

Numbers: the C++ side uses cJSON, which stores every JSON number as a double and requires it to
be integral and in range. Python's ``json`` distinguishes ``3`` from ``3.0``. We follow the
"integral JSON number" rule: ``3.0`` is accepted (it is integral), ``1.5``, ``NaN`` and ``inf`` are
``bad_type``, and ``bool`` is never an int. The agent itself only ever emits real ints, so
the ``3.0`` corner is only about checking foreign messages.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024

CLOCK_STATES = ("running", "stopped", "injury", "expired")
SCREENS = ("score", "splash")

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_LABEL_RE = re.compile(r"^[\x20-\x7E]{0,8}$")

# Wire error codes (identical to the C++ parse_error_code strings).
ERROR_CODES = (
    "too_large",
    "not_json",
    "not_object",
    "wrong_type",
    "unsupported_version",
    "missing_field",
    "bad_type",
    "out_of_range",
)


class SchemaError(Exception):
    """A state message violates the schema. ``code`` is the wire string, ``field`` the field or None."""

    def __init__(self, code: str, field: str | None = None, detail: str = ""):
        self.code = code
        self.field = field
        self.detail = detail
        text = code if field is None else f"{code} ({field})"
        if detail:
            text += f": {detail}"
        super().__init__(text)


@dataclass
class BoutState:
    """Current full state of a bout, as an adapter reports it. Maps 1:1 onto the snapshot."""

    left_score: int = 0
    right_score: int = 0
    left_warnings: int = 0
    right_warnings: int = 0
    period: int = 1
    clock_ms_remaining: int = 0
    clock_state: str = "stopped"  # running | stopped | injury | expired
    screen: str = "score"  # score | splash
    weight: str = ""
    bout: str = ""
    colors_swapped: bool = False


# --- field table ---------------------------------------------------------------------------------
# (name, kind, arg). Order is the validation order (protocol table order, `v` first).
_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("v", "int", None),  # range handled separately (unsupported_version)
    ("session", "session", None),
    ("seq", "int", (0, 4294967295)),
    ("sender_ts", "int", (0, 9007199254740991)),
    ("left_score", "int", (0, 99)),
    ("right_score", "int", (0, 99)),
    ("left_warnings", "int", (0, 9)),
    ("right_warnings", "int", (0, 9)),
    ("period", "int", (1, 9)),
    ("clock_ms_remaining", "int", (0, 3600000)),
    ("clock_state", "enum", CLOCK_STATES),
    ("screen", "enum", SCREENS),
    ("weight", "label", None),
    ("bout", "label", None),
    ("colors_swapped", "bool", None),
)


def _as_int(value: Any) -> int | None:
    """Return the integer value of an integral JSON number, else None. bool is not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value == math.floor(value):
            return int(value)
        return None
    return None


def validate_state_message(d: dict) -> None:
    """Validate a parsed state message (dict). Raises :class:`SchemaError`; returns None if valid.

    ``too_large`` and ``not_json`` cannot be detected on a dict; see :func:`parse_and_validate`.
    """
    if not isinstance(d, dict):
        raise SchemaError("not_object")
    if d.get("type") != "state" or not isinstance(d.get("type"), str):
        raise SchemaError("wrong_type", "type")

    v = d.get("v")
    v_int = _as_int(v)
    if "v" in d and v_int is not None and v_int != PROTOCOL_VERSION:
        raise SchemaError("unsupported_version", "v")

    for name, kind, arg in _FIELDS:
        if name not in d:
            raise SchemaError("missing_field", name)
        value = d[name]
        if kind == "int":
            n = _as_int(value)
            if n is None:
                raise SchemaError("bad_type", name)
            if name == "v":
                continue  # == 1 guaranteed above
            lo, hi = arg
            if not lo <= n <= hi:
                raise SchemaError("out_of_range", name)
        elif kind == "session":
            if not isinstance(value, str):
                raise SchemaError("bad_type", name)
            if not _SESSION_RE.match(value):
                raise SchemaError("out_of_range", name)
        elif kind == "enum":
            if not isinstance(value, str):
                raise SchemaError("bad_type", name)
            if value not in arg:
                raise SchemaError("out_of_range", name)
        elif kind == "label":
            if not isinstance(value, str):
                raise SchemaError("bad_type", name)
            if not _LABEL_RE.match(value):
                raise SchemaError("out_of_range", name)
        elif kind == "bool":
            if not isinstance(value, bool):
                raise SchemaError("bad_type", name)


def _reject_constant(name: str):
    raise ValueError(f"non-standard JSON constant {name}")


def parse_and_validate(raw: bytes | str) -> dict:
    """Full wire-level check: size, JSON syntax, then :func:`validate_state_message`.

    Returns the parsed dict. The size limit is in bytes (UTF-8 for ``str`` input).
    """
    data = raw.encode("utf-8", "surrogatepass") if isinstance(raw, str) else bytes(raw)
    if len(data) > MAX_MESSAGE_BYTES:
        raise SchemaError("too_large")
    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise SchemaError("not_json") from None
    validate_state_message(obj)
    return obj


def to_message(state: BoutState, session: str, seq: int, sender_ts: int) -> dict:
    """Build the wire dict for a snapshot. Does not validate (see :func:`validate_state_message`)."""
    return {
        "type": "state",
        "v": PROTOCOL_VERSION,
        "session": session,
        "seq": seq,
        "sender_ts": sender_ts,
        "left_score": state.left_score,
        "right_score": state.right_score,
        "left_warnings": state.left_warnings,
        "right_warnings": state.right_warnings,
        "period": state.period,
        "clock_ms_remaining": state.clock_ms_remaining,
        "clock_state": state.clock_state,
        "screen": state.screen,
        "weight": state.weight,
        "bout": state.bout,
        "colors_swapped": state.colors_swapped,
    }


def dumps(msg: dict) -> str:
    """Compact JSON, as sent on the wire."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=True)
