"""USABracketing source adapter.

Reads the controller's state from the matside server (``GET http://127.0.0.1:6099/usab?function=getTimer``,
exactly what the project's own video overlays poll) and maps it onto our snapshot. The model, the
parsing and the mapping rules are in :mod:`bracketboard_agent.usab` and documented in
``docs/usab-controller-protocol.md``; this module is only the glue:

* ``read_state()`` polls ``getTimer`` on every call (cheap, localhost) and keeps the receive time so
  the clock of a running timer can be extrapolated (the controller sends nothing while it runs);
* when the bout changes, weight/bout number are fetched in the background (``getEvent`` then the app
  server's ``/api/live_scoring/bout``), cached per bout, retried with backoff, and never block or fail
  state delivery;
* ``read_state()`` returns ``None`` (never raises) while the matside server is unreachable or answers
  garbage; the sign then goes stale on its own. The outage is logged once, the recovery once.

The agent never writes to the controller. ``device_key`` (from ``getEvent``) is never logged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Callable
from urllib.parse import urlsplit

from ..adapter import SourceAdapter
from ..schema import BoutState
from ..usab.client import DEFAULT_MATSIDE_PORT, AppServerClient, MatsideClient, UsabClientError
from ..usab.mapping import MappingConfig, StateMapper
from ..usab.model import BoutInfo, EventInfo

log = logging.getLogger("bracketboard.usab")

LOOKUP_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
_BOUT_CACHE_SIZE = 32


def _safe_server(url: str) -> str:
    """Host part of an app server URL for logging (no credentials, path or query)."""
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


class USABracketingAdapter(SourceAdapter):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_MATSIDE_PORT,
        *,
        offset_ms: float = 0,
        swap_sides: bool = False,
        bout_lookup: bool = True,
        poll_interval: float = 0.1,
        timeout: float = 0.5,
        period_overrides: dict | None = None,
        client: MatsideClient | None = None,
        app_client: AppServerClient | None = None,
        lookup_backoff: tuple[float, ...] = LOOKUP_BACKOFF_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self.client = client or MatsideClient(host, port, timeout=timeout, clock=clock)
        self.app = app_client or AppServerClient()
        self.bout_lookup = bout_lookup
        self.poll_interval = poll_interval
        self.lookup_backoff = lookup_backoff
        self.mapper = StateMapper(
            MappingConfig(swap_sides=swap_sides, controller_offset_ms=offset_ms, period_overrides=dict(period_overrides or {}))
        )
        self._bouts: OrderedDict[tuple[str, str], BoutInfo] = OrderedDict()
        self._lookup_key: tuple[str, str] | None = None
        self._lookup_task: asyncio.Task | None = None
        self._event: EventInfo | None = None
        self._event_for: str | None = None
        self._outage: str | None = None  # kind of the failure we already logged, None = healthy
        self.lookup_failures = 0

    # -- lifecycle -------------------------------------------------------------------------------
    async def start(self) -> None:
        try:
            ok = await self.client.alive()
        except Exception:  # never let a probe problem stop the agent
            ok = False
        if ok:
            log.info("matside server reachable at %s", self.client.base_url)
        else:
            self._outage = "unreachable"
            log.warning(
                "matside server not reachable at %s (is the USABracketing controller/matside server running "
                "on this machine?); will keep retrying, the sign shows stale until it answers",
                self.client.base_url,
            )

    async def stop(self) -> None:
        self._cancel_lookup()

    # -- SourceAdapter ---------------------------------------------------------------------------
    async def read_state(self) -> BoutState | None:
        try:
            timer, rx = await self.client.get_timer()
        except UsabClientError as e:
            self._note_failure(e)
            return None
        except Exception as e:  # defensive: an adapter must not raise
            self._note_failure(UsabClientError("unreachable", type(e).__name__))
            return None
        if self._outage is not None:
            log.info("matside server is answering again")
            self._outage = None

        bout_id = timer.bout_id.strip()
        key = (timer.event_id.strip(), bout_id)
        bout = None
        if bout_id and self.bout_lookup:
            bout = self._bouts.get(key)
            if bout is not None:
                self._bouts.move_to_end(key)
            self._ensure_lookup(key, bout is not None)
        elif self._lookup_key is not None:
            self._cancel_lookup()
        return self.mapper.map(timer, rx, self._clock(), bout)

    async def wait_change(self, timeout: float) -> None:
        await asyncio.sleep(max(0.0, min(timeout, self.poll_interval)))

    # -- outage logging --------------------------------------------------------------------------
    def _note_failure(self, e: UsabClientError) -> None:
        if self._outage == e.kind:
            log.debug("matside poll still failing: %s", e)
            return
        first = self._outage is None
        self._outage = e.kind
        if e.kind == "parse":
            log.warning("matside server answered something we cannot parse (%s); no state until it does", e)
        elif first:
            log.warning("lost the matside server (%s); no state until it answers again", e)
        else:
            log.warning("matside server still failing, now: %s", e)

    # -- background bout lookup ------------------------------------------------------------------
    def _cancel_lookup(self) -> None:
        task, self._lookup_task = self._lookup_task, None
        self._lookup_key = None
        if task is not None and not task.done():
            task.cancel()

    def _ensure_lookup(self, key: tuple[str, str], have: bool) -> None:
        if have:
            if self._lookup_key == key:
                self._cancel_lookup()
            return
        if self._lookup_key == key and self._lookup_task is not None and not self._lookup_task.done():
            return
        self._cancel_lookup()
        self._lookup_key = key
        self._lookup_task = asyncio.ensure_future(self._lookup(key))

    async def _lookup(self, key: tuple[str, str]) -> None:
        event_id, bout_id = key
        attempt = 0
        while True:
            try:
                if self._event is None or self._event_for != event_id:
                    self._event = await self.client.get_event()
                    self._event_for = event_id
                info = await self.app.fetch_bout(self._event, bout_id)
            except asyncio.CancelledError:
                raise
            except UsabClientError as e:
                self._event = None  # the key may have changed: ask the controller again next time
                self.lookup_failures += 1
                delay = self.lookup_backoff[min(attempt, len(self.lookup_backoff) - 1)]
                (log.warning if attempt == 0 else log.debug)(
                    "bout lookup for bout %s failed (%s); retrying in %.0f s", bout_id, e, delay
                )
                attempt += 1
                await asyncio.sleep(delay)
                continue
            except Exception as e:  # never let the background task die silently
                self._event = None
                self.lookup_failures += 1
                delay = self.lookup_backoff[min(attempt, len(self.lookup_backoff) - 1)]
                log.warning("bout lookup failed unexpectedly (%s); retrying in %.0f s", type(e).__name__, delay)
                attempt += 1
                await asyncio.sleep(delay)
                continue
            self._bouts[key] = info
            while len(self._bouts) > _BOUT_CACHE_SIZE:
                self._bouts.popitem(last=False)
            log.info("bout %s: weight %r, bout number %r (app server %s)", bout_id, info.weight_name, info.display_bout,
                     _safe_server(self._event.server if self._event else ""))
            return


UsabAdapter = USABracketingAdapter
