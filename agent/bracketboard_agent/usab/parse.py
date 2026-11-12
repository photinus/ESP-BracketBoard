"""Parsers for the controller / matside-server texts. NEVER eval anything.

``getTimer`` is *not* JSON (docs/usab-controller-protocol.md section 4): keys and strings are
single-quoted, strings are not escaped, scores/ids/colors are quoted strings and ``message`` is free
text that may contain apostrophes. The server's own consumer (the overlay) ``eval``s it; we must
not. So we extract each field by key with regular expressions:

* the ``message`` value is located first and cut out of the text (it ends at ``', 'system_millis'``,
  the *last* such marker, so hostile message text cannot spoof other keys or end itself early);
* every other field is looked up in what remains;
* real JSON (double quotes) is accepted too, in case the server is changed;
* input is bounded (:data:`MAX_TEXT` characters) and error messages never contain the input.
"""
from __future__ import annotations

import json
import math
import re
from urllib.parse import unquote

from .model import ControllerTimer, EventInfo

MAX_TEXT = 64 * 1024

# Per-field length caps (truncation, not an error): hostile or buggy servers cannot make us hold huge strings.
_CAPS = {
    "name": 128, "bout_id": 64, "event_id": 64, "w1_color": 32, "w2_color": 32, "w1_score": 24,
    "w2_score": 24, "tie_winner": 16, "t1_score": 24, "t2_score": 24, "w1_position": 16,
    "wrestler_indicator": 16, "message": 512, "device_key": 512, "server": 512,
}

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:NaN|Infinity|inf)"
_NUM_RE = re.compile(_NUM, re.IGNORECASE)
_SQ = r"'((?:[^']|'(?!\s*[,}]))*)'"
_DQ = r'"((?:[^"]|"(?!\s*[,}]))*)"'


class UsabParseError(ValueError):
    """The text is not a usable controller response. The message never includes the input."""


def _key(name: str) -> str:
    return r"(?<![A-Za-z0-9_])['\"]" + re.escape(name) + r"['\"]\s*:\s*"


_NUM_VALUE_RES: dict[str, re.Pattern] = {}
_STR_VALUE_RES: dict[str, re.Pattern] = {}


def _num_re(name: str) -> re.Pattern:
    r = _NUM_VALUE_RES.get(name)
    if r is None:
        r = _NUM_VALUE_RES[name] = re.compile(_key(name) + r"['\"]?(" + _NUM + r")['\"]?", re.IGNORECASE)
    return r


def _str_re(name: str) -> re.Pattern:
    r = _STR_VALUE_RES.get(name)
    if r is None:
        r = _STR_VALUE_RES[name] = re.compile(
            _key(name) + "(?:" + _SQ + "|" + _DQ + r"|(null|undefined)|(" + _NUM + "))", re.IGNORECASE
        )
    return r


_MSG_START_RE = re.compile(_key("message") + "'")
_MSG_END_RE = re.compile(r"'\s*,\s*'system_millis'\s*:")
_MSG_FALLBACK_END_RE = re.compile(r"'\s*\}\s*$")


# --- scalar coercion -------------------------------------------------------------------------------
def _to_float(v) -> float | None:
    """Float from a number / numeric string. Non-finite -> ``None`` (caller decides what that means)."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        s = v.strip()
        if not _NUM_RE.fullmatch(s):
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(f):
        return None
    return f


def _to_int(v) -> int | None:
    f = _to_float(v)
    if f is None or abs(f) > 2**62:
        return None
    return int(round(f))


def _clean_str(name: str, v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        s = "1" if v else "0"
    elif isinstance(v, (int, float)):
        s = _fmt_number(v)
    elif isinstance(v, str):
        s = v
    else:
        s = ""
    cap = _CAPS.get(name, 128)
    return s[:cap]


def _fmt_number(v) -> str:
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
    return str(v)


def _norm_indicator(s: str) -> str:
    s = s.strip()
    return "" if s.lower() in ("null", "undefined", "nan") else s


def _norm_color(s: str) -> str:
    return s.strip().lstrip("#")


# --- getTimer --------------------------------------------------------------------------------------
def _check_size(text) -> str:
    if not isinstance(text, str):
        raise UsabParseError("response is not text")
    if len(text) > MAX_TEXT:
        raise UsabParseError(f"response too large ({len(text)} chars, limit {MAX_TEXT})")
    return text


def _build_timer(get_num, get_str) -> ControllerTimer:
    """Common builder: ``get_num(key) -> raw or None``, ``get_str(key) -> str or None`` (None = key absent)."""
    cur = _to_float(get_num("current_time"))
    if cur is None:
        raise UsabParseError("missing or non-numeric 'current_time'")
    running = _to_int(get_num("running"))
    if running is None:
        raise UsabParseError("missing or non-numeric 'running'")
    sysm = _to_int(get_num("system_millis"))
    if sysm is None:
        raise UsabParseError("missing or non-numeric 'system_millis'")

    t = ControllerTimer(current_time=cur, running=running, system_millis=sysm)
    um = _to_int(get_num("updated_millis"))
    t.updated_millis = um if um is not None else 0
    rt = _to_float(get_num("riding_time"))
    t.riding_time = rt if rt is not None else 0.0
    sc = _to_float(get_num("shot_clock"))
    t.shot_clock = sc if sc is not None else 0.0

    for name in ("name", "bout_id", "event_id", "w1_color", "w2_color", "w1_score", "w2_score", "tie_winner",
                 "t1_score", "t2_score", "w1_position", "wrestler_indicator", "message"):
        v = get_str(name)
        if v is None:
            continue  # absent key: keep the documented default
        v = _clean_str(name, v)
        if name in ("w1_color", "w2_color"):
            v = _norm_color(v)
        elif name == "wrestler_indicator":
            v = _norm_indicator(v)
        setattr(t, name, v)
    return t


def _reject_constant(name: str):
    raise ValueError(name)


def _try_json(text: str) -> dict | None:
    if not text.lstrip().startswith("{"):
        return None
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _split_message(text: str) -> tuple[str, str | None]:
    """Return ``(text_without_message_value, message_or_None)``. Raises on an unterminated message."""
    m = _MSG_START_RE.search(text)
    if m is None:
        return text, None
    start = m.end()
    end = None
    last = None
    for e in _MSG_END_RE.finditer(text, start):
        last = e
    if last is not None:
        end = last.start()
    else:
        f = _MSG_FALLBACK_END_RE.search(text, start)
        if f is not None:
            end = f.start()
    if end is None:
        raise UsabParseError("unterminated 'message' value")
    message = text[start:end]
    return text[: m.start()] + "'message':''" + text[end + 1:], message


def parse_get_timer(text: str) -> ControllerTimer:
    """Parse a ``getTimer`` response (single-quoted pseudo-JSON, or real JSON). See the module docstring."""
    text = _check_size(text)
    obj = _try_json(text)
    if obj is not None:
        def jnum(k):
            return obj.get(k)

        def jstr(k):
            return obj[k] if k in obj else None

        return _build_timer(jnum, jstr)

    rest, message = _split_message(text)

    def xnum(k):
        m = _num_re(k).search(rest)
        return m.group(1) if m else None

    def xstr(k):
        if k == "message":
            return message
        m = _str_re(k).search(rest)
        if m is None:
            return None
        sq, dq, nul, num = m.groups()
        if sq is not None:
            return sq
        if dq is not None:
            return dq
        if nul is not None:
            return ""
        return num

    return _build_timer(xnum, xstr)


# --- updateTimer (controller -> server) query ------------------------------------------------------
_MESSAGE_PARAM_RE = re.compile(r"(?:^|&)message=(.*)\Z", re.DOTALL)


def parse_update_timer_query(qs: str, system_millis: int | None = None) -> ControllerTimer:
    """Parse the controller's ``updateTimer`` query string (``time`` -> ``current_time``).

    The controller does not URL-encode its values and ``message`` (always last) is free text, so
    ``message`` takes everything after ``message=``. ``system_millis`` is the server's stamp (default:
    ``updated_millis``). Used by tests and the fake matside server.
    """
    qs = _check_size(qs)
    if "?" in qs:
        qs = qs.split("?", 1)[1]
    message = ""
    m = _MESSAGE_PARAM_RE.search(qs)
    if m is not None:
        message = unquote(m.group(1))
        qs = qs[: m.start()]
    params: dict[str, str] = {}
    for part in qs.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        params.setdefault(unquote(k), unquote(v))

    t_raw = params.get("time")
    cur = _to_float(t_raw)
    if cur is None:
        raise UsabParseError("missing or non-numeric 'time'")
    running = _to_int(params.get("running", "0"))
    if running is None:
        raise UsabParseError("non-numeric 'running'")
    t = ControllerTimer(current_time=cur, running=running)
    for name in ("name", "bout_id", "event_id", "w1_color", "w2_color", "w1_score", "w2_score", "tie_winner",
                 "t1_score", "t2_score", "w1_position", "wrestler_indicator"):
        if name in params:
            v = _clean_str(name, params[name])
            if name in ("w1_color", "w2_color"):
                v = _norm_color(v)
            elif name == "wrestler_indicator":
                v = _norm_indicator(v)
            setattr(t, name, v)
    t.message = _clean_str("message", message)
    rt = _to_float(params.get("riding_time"))
    t.riding_time = rt if rt is not None else 0.0
    sc = _to_float(params.get("shot_clock"))
    t.shot_clock = sc if sc is not None else 0.0
    um = _to_int(params.get("updated_millis"))
    t.updated_millis = um if um is not None else 0
    sm = system_millis if system_millis is not None else t.updated_millis
    t.system_millis = int(sm)
    return t


# --- getEvent --------------------------------------------------------------------------------------
def parse_get_event(text: str) -> EventInfo:
    """Parse a ``getEvent`` response. ``device_key`` is a credential: it appears in no error text."""
    text = _check_size(text)
    obj = _try_json(text)
    vals: dict[str, str | None] = {}
    if obj is not None:
        for k in ("event_id", "device_key", "server"):
            vals[k] = _clean_str(k, obj[k]) if k in obj else None
    else:
        for k in ("event_id", "device_key", "server"):
            m = _str_re(k).search(text)
            if m is None:
                vals[k] = None
                continue
            sq, dq, nul, num = m.groups()
            vals[k] = _clean_str(k, sq if sq is not None else dq if dq is not None else "" if nul is not None else num)
    if all(v is None for v in vals.values()):
        raise UsabParseError("not a getEvent response")
    server = (vals["server"] or "").strip().replace("[f]", "/")
    return EventInfo(event_id=(vals["event_id"] or "").strip(), device_key=vals["device_key"] or "", server=server)
