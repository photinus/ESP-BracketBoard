"""Source adapter interface: where the agent gets the current bout state from."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from .schema import BoutState


class SourceAdapter(ABC):
    """Reads the live bout state from some source of truth (USABracketing, a mock script, ...)."""

    async def start(self) -> None:
        """Open connections / start background tasks. Called once before the first read_state()."""

    async def stop(self) -> None:
        """Release resources. Called once when the publisher shuts down."""

    @abstractmethod
    async def read_state(self) -> BoutState | None:
        """Return the CURRENT full state with the clock sampled *now*.

        Return ``None`` when the source is temporarily unavailable; the publisher then sends
        nothing (the sign goes stale after its timeout, which is the correct signal).
        """

    async def wait_change(self, timeout: float) -> None:
        """Return when the source may have changed, or after ``timeout`` seconds.

        Push-style adapters override this to wake early; the default simply sleeps.
        """
        await asyncio.sleep(timeout)
