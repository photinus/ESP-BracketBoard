"""HTTP clients for the matside server (localhost:6099) and the app server.

Plain ``urllib`` in an executor, like ``HttpTransport``: strict total timeouts, response-size caps,
no redirects, http/https only. The matside client never uses a proxy (it is localhost); the app
server client honours the environment's proxy settings except for loopback hosts.

``device_key`` is a credential: it is never logged and never put in an exception message or repr
(error messages here are built from fixed text, status codes and exception class names only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.error
import urllib.request
from http.client import HTTPException
from typing import Callable
from urllib.parse import urlencode, urlsplit

from .model import BoutInfo, ControllerTimer, EventInfo
from .parse import MAX_TEXT, UsabParseError, parse_get_event, parse_get_timer

log = logging.getLogger("bracketboard.usab.client")

DEFAULT_MATSIDE_PORT = 6099
MATSIDE_MAX_BYTES = MAX_TEXT  # a getTimer/getEvent reply is a few hundred bytes
APP_MAX_BYTES = 1 << 20  # bout reply includes the match format
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


class UsabClientError(Exception):
    """A request failed. ``kind``: unreachable | timeout | http | too_large | parse | config."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(f"{kind}: {message}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


def _is_loopback(host: str | None) -> bool:
    return (host or "").lower() in _LOOPBACK


def _fetch(url: str, *, data: bytes | None, headers: dict | None, timeout: float, max_bytes: int, use_proxy: bool) -> bytes:
    """Blocking GET/POST with a hard total deadline and a size cap. Raises :class:`UsabClientError`."""
    try:
        parts = urlsplit(url)
        scheme_ok = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        scheme_ok = False
    if not scheme_ok:
        raise UsabClientError("config", "URL must be http(s) with a host")
    handlers: list = [_NoRedirect()]
    if not use_proxy or _is_loopback(parts.hostname):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET", headers=headers or {})
    deadline = time.monotonic() + timeout
    try:
        with opener.open(req, timeout=timeout) as resp:
            clen = resp.headers.get("Content-Length")
            if clen is not None and clen.isdigit() and int(clen) > max_bytes:
                raise UsabClientError("too_large", f"response exceeds {max_bytes} bytes")
            chunks: list[bytes] = []
            total = 0
            while True:
                # read1: return after one recv, so the total deadline below is checked while data trickles in
                chunk = resp.read1(min(8192, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UsabClientError("too_large", f"response exceeds {max_bytes} bytes")
                chunks.append(chunk)
                if time.monotonic() > deadline:
                    raise UsabClientError("timeout", "response too slow")
            return b"".join(chunks)
    except UsabClientError:
        raise
    except urllib.error.HTTPError as e:
        raise UsabClientError("http", f"HTTP {e.code}") from None
    except (TimeoutError, OSError) as e:  # URLError is an OSError; socket.timeout is a TimeoutError
        reason = getattr(e, "reason", e)
        kind = "timeout" if isinstance(e, TimeoutError) or isinstance(reason, TimeoutError) else "unreachable"
        raise UsabClientError(kind, type(reason).__name__) from None
    except (HTTPException, ValueError) as e:
        raise UsabClientError("http", f"bad HTTP exchange ({type(e).__name__})") from None


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


class MatsideClient:
    """Client for the matside server's ``/usab?function=...`` endpoints."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_MATSIDE_PORT,
        timeout: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = MATSIDE_MAX_BYTES,
    ):
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6 literal
        self.host = host
        self.port = port
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._clock = clock

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/usab"

    async def _get(self, function: str) -> str:
        url = f"{self.base_url}?function={function}"
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None, lambda: _fetch(url, data=None, headers=None, timeout=self.timeout, max_bytes=self.max_bytes, use_proxy=False)
        )
        return _decode(raw)

    async def alive(self) -> bool:
        """True if the matside server answers ``alive`` with HTTP 200 (the body is not inspected)."""
        try:
            await self._get("alive")
        except UsabClientError:
            return False
        return True

    async def get_timer(self) -> tuple[ControllerTimer, float]:
        """``getTimer`` -> ``(timer, rx_mono)``; ``rx_mono`` is the local monotonic time of receipt."""
        text = await self._get("getTimer")
        rx = self._clock()
        try:
            return parse_get_timer(text), rx
        except UsabParseError as e:
            raise UsabClientError("parse", str(e)) from None

    async def get_event(self) -> EventInfo:
        text = await self._get("getEvent")
        try:
            return parse_get_event(text)
        except UsabParseError as e:
            raise UsabClientError("parse", str(e)) from None


def _s(v) -> str:
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, str):
        return v[:64]
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        return str(int(v)) if v == int(v) else str(v)
    if isinstance(v, int):
        return str(v)
    return ""


def _reject_constant(name: str):
    raise ValueError(name)


class AppServerClient:
    """Client for the app server's ``POST {server}/api/live_scoring/bout`` (weight / bout number)."""

    def __init__(self, timeout: float = 2.0, *, max_bytes: int = APP_MAX_BYTES):
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def fetch_bout(self, event: EventInfo, bout_id: str) -> BoutInfo:
        server = (event.server or "").rstrip("/")
        if not server or not event.event_id or not event.device_key or not bout_id:
            raise UsabClientError("config", "event info incomplete (no event selected yet?)")
        url = server + "/api/live_scoring/bout"
        body = urlencode({"event_id": event.event_id, "device_key": event.device_key, "bout_id": bout_id}).encode("ascii", "replace")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None, lambda: _fetch(url, data=body, headers=headers, timeout=self.timeout, max_bytes=self.max_bytes, use_proxy=True)
        )
        return self._parse(_decode(raw))

    @staticmethod
    def _parse(text: str) -> BoutInfo:
        try:
            obj = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise UsabClientError("parse", "app server reply is not JSON") from None
        if not isinstance(obj, dict):
            raise UsabClientError("parse", "app server reply is not an object")
        status = obj.get("status")
        if status is not None and str(status).strip().lower() not in ("success", "ok"):
            raise UsabClientError("http", "app server did not report success")
        bout = obj.get("bout")
        if not isinstance(bout, dict):
            raise UsabClientError("parse", "app server reply has no 'bout' object")
        return BoutInfo(
            weight_name=_s(bout.get("weight_name")),
            bout_number=_s(bout.get("bout_number")),
            match_id=_s(bout.get("match_id")),
        )
