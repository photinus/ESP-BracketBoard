"""Data model of what the USABracketing controller / matside server reports.

Types follow docs/usab-controller-protocol.md section 4: ``current_time``, ``running``,
``updated_millis``, ``riding_time``, ``shot_clock`` and ``system_millis`` are numbers on the wire;
everything else is a (quoted) string, including scores and colors.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ControllerTimer:
    """One ``getTimer`` sample (the controller's *current* timer plus match info).

    The defaults describe the matside server's documented default state, before any state push:
    ``Period 1``, empty ids and colors.
    """

    name: str = "Period 1"  # the CURRENT timer's name (a period, Break, Blood, Injury...), not the period
    current_time: float = 0.0  # seconds remaining on that timer when the controller sampled it
    running: int = 0  # 1 / 0
    updated_millis: int = 0  # CONTROLLER machine epoch ms of the sample
    bout_id: str = ""
    event_id: str = ""
    w1_color: str = ""  # hex without '#', or 'black' / ''
    w2_color: str = ""
    w1_score: str = "0"
    w2_score: str = "0"
    tie_winner: str = ""
    t1_score: str = "0"
    t2_score: str = "0"
    riding_time: float = 0.0  # signed seconds: positive = wrestler 1 ahead
    w1_position: str = "neutral"  # top | bottom | neutral
    shot_clock: float = 0.0  # seconds remaining, 0 = none
    wrestler_indicator: str = ""  # '1' / '2' / '0' / '' : whose shot clock
    message: str = ""  # "<counter>.<text>", free text, may contain apostrophes
    system_millis: int = 0  # SERVER epoch ms when it answered

    @property
    def is_running(self) -> bool:
        return self.running == 1


@dataclass
class EventInfo:
    """``getEvent`` reply. ``device_key`` is a credential: it is excluded from repr and never logged."""

    event_id: str = ""
    device_key: str = field(default="", repr=False)
    server: str = ""  # app server base URL (http/https)

    def __str__(self) -> str:  # never leak the key through str() either
        return f"EventInfo(event_id={self.event_id!r}, server={self.server!r})"


@dataclass
class BoutInfo:
    """What we take from the app server's ``/api/live_scoring/bout`` reply."""

    weight_name: str = ""
    bout_number: str = ""
    match_id: str = ""

    @property
    def display_bout(self) -> str:
        """``bout_number``, or ``"m" + match_id`` if the number is empty (the controller's own rule)."""
        if self.bout_number:
            return self.bout_number
        return f"m{self.match_id}" if self.match_id else ""
