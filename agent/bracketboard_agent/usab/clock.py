"""Time-sync model (docs/usab-controller-protocol.md section 5). Pure functions, injectable times.

The controller samples a timer at ``updated_millis`` (its clock) and the matside server stamps
``system_millis`` (its clock) when it answers. The controller sends nothing while a clock runs, so
a consumer must extrapolate::

    age_ms(now) = max(0, (system_millis - updated_millis) - controller_offset_ms) + (mono_now - mono_rx)
    remaining   = current_time*1000 - age_ms        if running == 1, else current_time*1000
    remaining   = clamp(remaining, 0, 3_600_000)

``controller_offset_ms`` follows the overlay's ``offset_millis`` convention: it is *subtracted* from
the controller-side age, so a positive value shortens the computed age. Defined as
(server clock - controller clock): ``system_millis - updated_millis`` equals the true age plus that
difference, so it is subtracted. On one machine, the default 0 is right.)
"""
from __future__ import annotations

import math

from .model import ControllerTimer

MAX_CLOCK_MS = 3_600_000


def _finite(x: float, default: float = 0.0) -> float:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def age_ms(timer: ControllerTimer, rx_mono: float, now_mono: float, controller_offset_ms: float = 0) -> float:
    """Milliseconds since the controller sampled ``timer``, as of ``now_mono``.

    ``rx_mono`` is the local monotonic time (seconds) when the sample was received. A controller
    stamp of 0 (or a missing server stamp) means "age unknown": the controller-side part is 0.
    """
    controller_age = 0.0
    if timer.updated_millis > 0 and timer.system_millis > 0:
        controller_age = max(0.0, float(timer.system_millis - timer.updated_millis) - _finite(controller_offset_ms))
    local = max(0.0, (now_mono - rx_mono) * 1000.0)
    return controller_age + local


def remaining_ms(timer: ControllerTimer, rx_mono: float, now_mono: float, controller_offset_ms: float = 0) -> int:
    """Remaining time of the controller's current timer in whole ms, clamped to 0..3_600_000."""
    base = _finite(timer.current_time) * 1000.0
    if timer.running == 1:
        base -= age_ms(timer, rx_mono, now_mono, controller_offset_ms)
    base = max(0.0, min(float(MAX_CLOCK_MS), base))
    return int(round(base))
