"""Map a controller timer sample onto our snapshot (docs/usab-controller-protocol.md section 6)."""
from __future__ import annotations

import colorsys
import math
import re
from dataclasses import dataclass, field

from ..schema import BoutState
from .clock import MAX_CLOCK_MS, age_ms, remaining_ms
from .model import BoutInfo, ControllerTimer


@dataclass
class MappingConfig:
    swap_sides: bool = False  # flip the left/right rule (left = red wrestler)
    controller_offset_ms: float = 0  # see clock.py: subtracted from the controller-side age
    period_overrides: dict = field(default_factory=dict)  # timer name (case-insensitive) -> period 1..9


# --- helpers ---------------------------------------------------------------------------------------
_NAMED = {"red": "red", "green": "green", "blue": "blue"}


def classify_color(hex_color: str) -> str:
    """Classify a controller color by hue: ``'red'``, ``'green'``, ``'blue'`` or ``'other'``.

    Accepts 3/6-digit hex with or without ``#`` in any case, plus the bare names ``red``/``green``/
    ``blue``. Black, white, grays (low saturation / value), empty and garbage are ``'other'``.
    """
    s = (hex_color or "").strip().lstrip("#").lower() if isinstance(hex_color, str) else ""
    if s in _NAMED:
        return _NAMED[s]
    if len(s) == 3 and re.fullmatch(r"[0-9a-f]{3}", s):
        s = "".join(c * 2 for c in s)
    if not re.fullmatch(r"[0-9a-f]{6}", s):
        return "other"
    r, g, b = (int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    h, sat, val = colorsys.rgb_to_hsv(r, g, b)
    if val < 0.25 or sat < 0.30:
        return "other"  # black, white, grays
    deg = h * 360.0
    if deg < 20 or deg >= 340:
        return "red"
    if 75 <= deg <= 165:
        return "green"
    if 190 <= deg <= 265:
        return "blue"
    return "other"


def printable(s, max_len: int) -> str:
    """Keep only printable ASCII (0x20-0x7E), trim, truncate to ``max_len``."""
    if not isinstance(s, str):
        return ""
    return "".join(c for c in s if " " <= c <= "~").strip()[:max_len].rstrip()


_INT_TEXT_RE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]*)?|[+-]?\.[0-9]+")


def lenient_score(v, lo: int = 0, hi: int = 99) -> int:
    """Score from controller text: non-numeric/NaN/inf -> 0, clamped to ``lo..hi``."""
    if isinstance(v, bool):
        n = int(v)
    elif isinstance(v, int):
        n = v
    elif isinstance(v, float):
        n = int(v) if math.isfinite(v) else 0
    elif isinstance(v, str):
        s = v.strip()
        if not _INT_TEXT_RE.fullmatch(s):
            n = 0
        elif len(s) > 40:  # absurdly long number: saturate without converting it
            n = -(10**9) if s.startswith("-") else 10**9
        else:
            try:
                n = int(float(s)) if "." in s else int(s)
            except (ValueError, OverflowError):
                n = 0
    else:
        n = 0
    return max(lo, min(hi, n))


def _clamp_round(x: float, lo: int, hi: int) -> int:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return lo if lo <= 0 <= hi else lo
    if math.isnan(x):
        x = 0.0
    return int(round(max(float(lo), min(float(hi), x))))


def _finite(x, default: float = 0.0) -> float:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


_PERIOD_RE = re.compile(r"period\s*(\d+)", re.IGNORECASE)
_SV_RE = re.compile(r"(?:sudden\s*victory|sv)\b", re.IGNORECASE)
_UTB_RE = re.compile(r"utb\b", re.IGNORECASE)
_TB_RE = re.compile(r"(?:tie\s*-?\s*breaker|tb)\b", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")
_OWNER_RE = re.compile(r"(?:\bw(?:restler)?\s*|[#(]\s*)([12])\b", re.IGNORECASE)


def parse_period(name: str) -> int | None:
    """Period number (1..9) for a period timer name, or ``None`` for non-period timers (Break, Blood...).

    ``Period N`` -> N; ``Sudden Victory``/``SV`` -> 4; ``Tie Breaker N``/``TB ... N`` -> 4+N (5 without
    a number); ``UTB ...`` -> 7 (after TB1=5, TB2=6). Clamped 1..9.
    """
    n = (name or "").strip()
    m = _PERIOD_RE.match(n)
    if m:
        return max(1, min(9, int(m.group(1)[:3])))
    if _SV_RE.match(n):
        return 4
    if _UTB_RE.match(n):
        return 7
    if _TB_RE.match(n):
        d = _DIGITS_RE.search(n)
        return max(1, min(9, 4 + (int(d.group(0)[:3]) if d else 1)))
    return None


class StateMapper:
    """Stateful mapper from controller samples to :class:`BoutState`.

    Stateful because the period is not reported: while a non-period timer (``Break``, wrestler
    timers) is current, the last known period is kept (reset to 1 when the bout changes).
    """

    def __init__(self, config: MappingConfig | None = None):
        self.config = config or MappingConfig()
        self._last_period = 1
        self._last_bout_id: str | None = None

    # -- side rule -------------------------------------------------------------------------------
    def w1_is_left(self, timer: ControllerTimer) -> bool:
        """Left = the red wrestler (else wrestler 1); ``swap_sides`` flips."""
        c1, c2 = classify_color(timer.w1_color), classify_color(timer.w2_color)
        w1_left = not (c1 != "red" and c2 == "red")
        return (not w1_left) if self.config.swap_sides else w1_left

    # -- period ----------------------------------------------------------------------------------
    def _period(self, name: str) -> int:
        ov = self.config.period_overrides
        p: int | None = None
        if ov:
            key = (name or "").strip().lower()
            for k, v in ov.items():
                if str(k).strip().lower() == key:
                    p = max(1, min(9, int(v)))
                    break
        if p is None:
            p = parse_period(name)
        if p is not None:
            self._last_period = p
        return self._last_period

    # -- main ------------------------------------------------------------------------------------
    def map(self, timer: ControllerTimer, rx_mono: float, now_mono: float, bout: BoutInfo | None = None) -> BoutState:
        cfg = self.config
        bout_id = (timer.bout_id or "").strip()
        if bout_id != self._last_bout_id:
            self._last_bout_id = bout_id
            self._last_period = 1
        w1_left = self.w1_is_left(timer)

        rem = remaining_ms(timer, rx_mono, now_mono, cfg.controller_offset_ms)
        if rem == 0:
            clock_state = "expired"
        elif timer.is_running:
            clock_state = "running"
        else:
            clock_state = "stopped"

        s1, s2 = lenient_score(timer.w1_score), lenient_score(timer.w2_score)
        left_score, right_score = (s1, s2) if w1_left else (s2, s1)

        weight = bout_s = ""
        if bout_id and bout is not None:
            weight = printable(bout.weight_name, 8)
            bout_s = printable(bout.display_bout, 8)

        st = BoutState(
            left_score=left_score,
            right_score=right_score,
            left_warnings=0,  # TODO: not in getTimer; needs the app server's bout.scores (spec section 8, item 5)
            right_warnings=0,
            period=self._period(timer.name),
            clock_ms_remaining=rem,
            clock_state=clock_state,
            screen="splash" if not bout_id else "score",
            weight=weight,
            bout=bout_s,
            colors_swapped=False,
        )
        self._extensions(st, timer, rx_mono, now_mono, w1_left)
        return st

    # -- optional extension fields ---------------------------------------------------------------
    def _extensions(self, st: BoutState, t: ControllerTimer, rx: float, now: float, w1_left: bool) -> None:
        offset = self.config.controller_offset_ms
        running = t.is_running
        age = age_ms(t, rx, now, offset) if running else 0.0
        side_of = {1: "left" if w1_left else "right", 2: "right" if w1_left else "left"}

        st.clock_label = printable(t.name, 12)

        # blink: the owning wrestler of a wrestler timer, only when the name says so (inferred)
        st.blink = "none"
        if parse_period(t.name) is None and (t.name or "").strip().lower() != "break":
            m = _OWNER_RE.search(t.name or "")
            if m:
                st.blink = side_of[int(m.group(1))]

        # riding time: signed, positive = left ahead; rate sign from w1_position
        pos = (t.w1_position or "").strip().lower()
        rate_w1 = 1 if pos == "top" else -1 if pos == "bottom" else 0
        riding = _finite(t.riding_time)
        if riding != 0.0 or rate_w1 != 0:
            ms = riding * 1000.0
            if running:
                # riding stops when the clock it belongs to reaches 0
                ms += rate_w1 * min(age, max(0.0, _finite(t.current_time) * 1000.0))
            ms_i = _clamp_round(ms, -MAX_CLOCK_MS, MAX_CLOCK_MS)
            st.riding_ms = ms_i if w1_left else -ms_i
            st.riding_dir = rate_w1 if w1_left else -rate_w1

        # shot clock
        ind = (t.wrestler_indicator or "").strip()
        owner = int(ind) if ind in ("1", "2") else 0
        shot = _finite(t.shot_clock)
        if shot > 0 or owner:
            st.shot_clock_ms = _clamp_round(shot * 1000.0 - age, 0, MAX_CLOCK_MS)
            st.shot_clock_side = side_of[owner] if owner else "none"
