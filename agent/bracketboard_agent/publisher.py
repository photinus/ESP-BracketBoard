"""Publisher: turns adapter state into a paced stream of snapshots for a Transport.

Cadence (docs/protocol-v1.md section 5):

* send immediately when any non-clock field changes;
* plain clock ticks do not trigger a send (the sign extrapolates a running clock);
* send immediately when the clock deviates more than 150 ms from the linear extrapolation of the
  last sent value (a clock edit / correction), or when the clock state changes;
* otherwise send a fresh snapshot (new seq) every ``heartbeat_s`` as liveness heartbeat;
* never faster than ``max_hz``;
* send immediately after the transport (re)connects.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import fields, replace
from typing import Callable

from .adapter import SourceAdapter
from .schema import BoutState, SchemaError, to_message, validate_state_message
from .transport import Transport, TransportAuthError, TransportError

log = logging.getLogger("bracketboard.publisher")

CLOCK_DEVIATION_MS = 150
SEQ_MASK = 0xFFFFFFFF
AUTH_RETRY_S = 5.0

_NON_CLOCK_FIELDS = tuple(f.name for f in fields(BoutState) if f.name != "clock_ms_remaining")


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


class Publisher:
    def __init__(
        self,
        adapter: SourceAdapter,
        transport: Transport,
        heartbeat_s: float = 1.0,
        poll_hz: float = 10,
        max_hz: float = 20,
        clock: Callable[[], float] = time.monotonic,
        *,
        wall_ms: Callable[[], int] = _wall_ms,
        session: str | None = None,
        first_seq: int = 1,
    ):
        self.adapter = adapter
        self.transport = transport
        self.heartbeat_s = heartbeat_s
        self.poll_hz = poll_hz
        self.max_hz = max_hz
        self._clock = clock
        self._wall_ms = wall_ms
        self.session = session or secrets.token_hex(6)  # new session per Publisher (agent start)
        self._seq = first_seq & SEQ_MASK
        self._last_state: BoutState | None = None
        self._last_sent_t: float | None = None
        self._last_attempt_t: float | None = None
        self._retry_at: float = 0.0
        self._force = True  # first snapshot is always sent
        self._stopping = False
        self._wake: asyncio.Event | None = None
        self._last_fail_log = -1e9
        self.sent_count = 0
        self.last_ack: str | None = None
        self.last_reason: str | None = None  # why the last snapshot was sent (diagnostics/tests)
        transport.on_connected = self.on_connected

    # -- control ---------------------------------------------------------------------------------
    @property
    def min_interval(self) -> float:
        return 1.0 / self.max_hz if self.max_hz > 0 else 0.0

    @property
    def seq(self) -> int:
        """The seq the next snapshot will carry."""
        return self._seq

    def on_connected(self) -> None:
        """Transport (re)connected: the sign may have lost state, push a snapshot right away."""
        self._force = True
        self._retry_at = 0.0
        if self._wake is not None:
            self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        if self._wake is not None:
            self._wake.set()

    # -- decision --------------------------------------------------------------------------------
    def _clock_deviates(self, state: BoutState, now: float) -> bool:
        last, t = self._last_state, self._last_sent_t
        assert last is not None and t is not None
        expected = last.clock_ms_remaining
        if last.clock_state == "running":
            expected = max(0.0, expected - (now - t) * 1000.0)
        return abs(state.clock_ms_remaining - expected) > CLOCK_DEVIATION_MS

    def _decide(self, state: BoutState, now: float) -> str | None:
        """Return the reason to send now, or None."""
        if now < self._retry_at:
            return None  # backing off after a failed send
        if self._force:
            return "connect" if self._last_state is not None else "first"
        if self._last_attempt_t is not None and now - self._last_attempt_t < self.min_interval - 1e-9:
            return None  # rate limit; the change is re-detected on a later poll
        last = self._last_state
        if last is None or self._last_sent_t is None:
            return "first"
        if any(getattr(state, n) != getattr(last, n) for n in _NON_CLOCK_FIELDS):
            return "change"
        if self._clock_deviates(state, now):
            return "clock"
        if now - self._last_sent_t >= self.heartbeat_s:
            return "heartbeat"
        return None

    # -- one iteration ---------------------------------------------------------------------------
    async def tick(self) -> bool:
        """Read the adapter once and send if the cadence rules say so. Returns True if sent."""
        now = self._clock()
        state = await self.adapter.read_state()
        if state is None:
            return False
        now = self._clock()  # clock may have advanced while reading
        reason = self._decide(state, now)
        if reason is None:
            return False
        state = replace(state)  # snapshot copy; adapters may reuse their object
        msg = to_message(state, self.session, self._seq, self._wall_ms())
        try:
            validate_state_message(msg)
        except SchemaError as e:
            self._log_failure("adapter produced an invalid state, not sending: %s", e)
            self._last_attempt_t = now
            self._retry_at = now + self.heartbeat_s
            return False

        self._seq = (self._seq + 1) & SEQ_MASK
        self._last_attempt_t = now
        was_force, self._force = self._force, False
        try:
            ack = await self.transport.send(msg)
        except TransportAuthError as e:
            self._force = was_force
            self._retry_at = now + AUTH_RETRY_S
            self._log_failure("sign rejected the token: %s", e)
            return False
        except TransportError as e:
            self._force = was_force
            self._retry_at = now + min(self.heartbeat_s, 1.0)
            self._log_failure("send failed: %s", e)
            return False

        self._last_state = state
        self._last_sent_t = now
        self.sent_count += 1
        self.last_reason = reason
        self.last_ack = ack
        if ack == "invalid":
            log.error("sign rejected snapshot seq=%s as invalid", msg["seq"])
        elif ack in ("stale", "duplicate"):
            log.warning("sign answered %s for seq=%s (another agent on the sign?)", ack, msg["seq"])
        return True

    def _log_failure(self, fmt: str, *args) -> None:
        now = time.monotonic()
        if now - self._last_fail_log >= 5.0:
            self._last_fail_log = now
            log.warning(fmt, *args)
        else:
            log.debug(fmt, *args)

    # -- main loop -------------------------------------------------------------------------------
    def _next_timeout(self) -> float:
        poll = 1.0 / self.poll_hz if self.poll_hz > 0 else 0.1
        if self._last_sent_t is None:
            return poll
        remaining = self.heartbeat_s - (self._clock() - self._last_sent_t)
        return max(0.005, min(poll, remaining))

    async def _sleep(self, timeout: float) -> None:
        """Wait for adapter change, wake-up (reconnect/stop) or timeout, whichever is first."""
        assert self._wake is not None
        if self._wake.is_set():
            self._wake.clear()
            return
        a = asyncio.ensure_future(self.adapter.wait_change(timeout))
        w = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({a, w}, timeout=timeout + 0.5, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (a, w):
                if not t.done():
                    t.cancel()
            await asyncio.gather(a, w, return_exceptions=True)
        self._wake.clear()

    async def run(self) -> None:
        """Run until :meth:`stop` is called (or the task is cancelled)."""
        self._wake = asyncio.Event()
        self._stopping = False
        await self.adapter.start()
        try:
            await self.transport.start()
            while not self._stopping:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("publisher tick failed")
                    await asyncio.sleep(0.5)
                if self._stopping:
                    break
                await self._sleep(self._next_timeout())
        finally:
            try:
                await self.transport.stop()
            finally:
                await self.adapter.stop()
