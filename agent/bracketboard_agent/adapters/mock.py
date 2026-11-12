"""Mock bout source: replays a JSON script with its own authoritative clock.

Script format (``[{"t": seconds, "do": action, ...args}]``, times are script seconds from start):

====================  ==========================================================================
``start``             clock_state -> running (ignored when the clock is at 0)
``stop``              clock_state -> stopped
``injury``            clock_state -> injury (clock held while the injury stoppage is on)
``resume``            clock_state -> running (same as ``start``)
``score``             ``side`` left|right, ``points`` (default 1, may be negative); clamped 0..99
``warn``              ``side``, ``n`` (default 1); clamped 0..9
``period``            ``n`` period number, optional ``clock_s`` / ``clock_ms`` (default: keep),
                      clock_state -> stopped (period break)
``expire``            clock -> 0, clock_state -> expired
``set_clock``         ``seconds`` or ``ms``; keeps running/stopped state (expired -> stopped if > 0)
``swap``              ``value`` true|false (default: toggle) colors_swapped
``splash``            ``on`` true|false (default true): screen splash|score
``weight`` / ``bout`` ``value`` string (0-8 printable ASCII)
====================  ==========================================================================

A running clock that reaches 0 turns into ``expired`` by itself. ``speed`` scales *script* time
against the injected clock (speed=4: four script seconds per real second); the game clock
runs in script time, so at speed != 1 the clock the sign sees runs fast. Use speed > 1 only for tests
and quick demos.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from ..adapter import SourceAdapter
from ..schema import BoutState

DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sample_bout.json"
DEFAULT_PERIOD_MS = 120_000


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


class MockBoutAdapter(SourceAdapter):
    def __init__(
        self,
        script_path: str | Path | None = None,
        speed: float = 1.0,
        loop: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.script_path = Path(script_path) if script_path else DEFAULT_SCRIPT
        self.speed = float(speed)
        self.loop = loop
        self._clock = clock
        with open(self.script_path, encoding="utf-8") as f:
            events = json.load(f)
        if not isinstance(events, list) or not all(isinstance(e, dict) and "t" in e and "do" in e for e in events):
            raise ValueError(f"{self.script_path}: script must be a list of {{'t':..,'do':..}} objects")
        self.events = sorted(events, key=lambda e: e["t"])  # stable: same-t events keep file order
        self.duration = self.events[-1]["t"] if self.events else 0.0
        self._t0: float | None = None
        self._reset()

    # -- authoritative state ---------------------------------------------------------------------
    def _reset(self) -> None:
        self._s = BoutState(clock_ms_remaining=DEFAULT_PERIOD_MS)
        self._idx = 0
        self._game_t = 0.0  # script time up to which the game clock has been advanced
        self._finished = False

    @property
    def finished(self) -> bool:
        """True once every scripted event has been applied (and not looping)."""
        self._advance()
        return self._finished

    async def start(self) -> None:
        self._t0 = self._clock()
        self._reset()

    async def stop(self) -> None:
        pass

    def script_time(self) -> float:
        if self._t0 is None:
            self._t0 = self._clock()
        return (self._clock() - self._t0) * self.speed

    def _advance_clock_to(self, t: float) -> None:
        """Run the game clock forward to script time ``t`` (auto-expiring at 0)."""
        dt_ms = (t - self._game_t) * 1000.0
        self._game_t = max(self._game_t, t)
        s = self._s
        if dt_ms <= 0 or s.clock_state != "running":
            return
        remaining = s.clock_ms_remaining - dt_ms
        if remaining <= 0:
            s.clock_ms_remaining = 0
            s.clock_state = "expired"
        else:
            s.clock_ms_remaining = int(round(remaining))

    def _advance(self) -> None:
        now = self.script_time()
        if self.loop and self.events and now >= self.duration + 1.0 and self._idx >= len(self.events):
            # restart the script; keep the phase so loops are seamless
            self._t0 += (self.duration + 1.0) / self.speed
            self._reset()
            now = self.script_time()
        while self._idx < len(self.events) and self.events[self._idx]["t"] <= now:
            ev = self.events[self._idx]
            self._advance_clock_to(float(ev["t"]))
            self._apply(ev)
            self._idx += 1
        self._advance_clock_to(now)
        if self._idx >= len(self.events):
            self._finished = True

    def _side(self, ev: dict) -> str:
        side = ev.get("side")
        if side not in ("left", "right"):
            raise ValueError(f"event {ev}: side must be 'left' or 'right'")
        return side

    def _apply(self, ev: dict) -> None:
        s = self._s
        do = ev["do"]
        if do in ("start", "resume"):
            if s.clock_ms_remaining > 0:
                s.clock_state = "running"
        elif do == "stop":
            s.clock_state = "stopped"
        elif do == "injury":
            s.clock_state = "injury"
        elif do == "score":
            attr = f"{self._side(ev)}_score"
            setattr(s, attr, _clamp(getattr(s, attr) + int(ev.get("points", 1)), 0, 99))
        elif do == "warn":
            attr = f"{self._side(ev)}_warnings"
            setattr(s, attr, _clamp(getattr(s, attr) + int(ev.get("n", 1)), 0, 9))
        elif do == "period":
            s.period = _clamp(int(ev["n"]), 1, 9)
            if "clock_ms" in ev:
                s.clock_ms_remaining = _clamp(int(ev["clock_ms"]), 0, 3_600_000)
            elif "clock_s" in ev:
                s.clock_ms_remaining = _clamp(int(round(float(ev["clock_s"]) * 1000)), 0, 3_600_000)
            s.clock_state = "stopped"
        elif do == "expire":
            s.clock_ms_remaining = 0
            s.clock_state = "expired"
        elif do == "set_clock":
            ms = ev["ms"] if "ms" in ev else float(ev["seconds"]) * 1000
            s.clock_ms_remaining = _clamp(int(round(ms)), 0, 3_600_000)
            if s.clock_ms_remaining == 0:
                s.clock_state = "expired"
            elif s.clock_state == "expired":
                s.clock_state = "stopped"
        elif do == "swap":
            s.colors_swapped = bool(ev["value"]) if "value" in ev else not s.colors_swapped
        elif do == "splash":
            s.screen = "splash" if ev.get("on", True) else "score"
        elif do == "weight":
            s.weight = str(ev["value"])
        elif do == "bout":
            s.bout = str(ev["value"])
        else:
            raise ValueError(f"unknown mock action {do!r}")

    # -- SourceAdapter ---------------------------------------------------------------------------
    async def read_state(self) -> BoutState | None:
        self._advance()
        return replace(self._s)

    async def wait_change(self, timeout: float) -> None:
        """Sleep until the next scripted event (in real time) or ``timeout``, whichever is first."""
        delay = timeout
        if self._idx < len(self.events):
            until = (self.events[self._idx]["t"] - self.script_time()) / self.speed
            delay = min(delay, max(0.0, until))
        await asyncio.sleep(max(0.0, delay))
