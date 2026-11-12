"""Transports from the agent to the sign: WebSocket (primary), HTTP POST (fallback), failover."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Callable

try:  # websockets is a hard dependency in pyproject, but degrade to HTTP-only if it is missing
    import websockets
    from websockets.exceptions import ConnectionClosed, WebSocketException
except ImportError:  # pragma: no cover - exercised only without the dependency
    websockets = None  # type: ignore[assignment]
    ConnectionClosed = WebSocketException = Exception  # type: ignore[assignment,misc]

from .schema import PROTOCOL_VERSION, dumps

log = logging.getLogger("bracketboard.transport")

AGENT_NAME = "bracketboard-agent/0.1.0"
BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 5.0)
AUTH_FAILED_DELAY_S = 30.0


class TransportError(Exception):
    """The snapshot was not delivered (not connected, timeout, refused)."""


class TransportAuthError(TransportError):
    """The sign rejected our token (WS ``auth_failed`` / HTTP 401). Retrying faster will not help."""


def backoff_delay(attempt: int, backoff=BACKOFF_S, rng: random.Random | None = None) -> float:
    """Reconnect delay for the ``attempt``-th consecutive failure (0-based): 0.5/1/2/4/5 s + jitter."""
    base = backoff[min(attempt, len(backoff) - 1)]
    r = rng or random
    return base + r.uniform(0.0, base * 0.25)


class Transport(ABC):
    """Delivers state messages to the sign.

    ``on_connected`` is invoked (no args, on the event loop) every time a link to the sign has just
    been established; the publisher uses it to push a snapshot immediately.
    ``on_ack`` (optional) receives every ack/error frame dict the sign sends back (WS only).
    """

    on_connected: Callable[[], None] | None = None
    on_ack: Callable[[dict], None] | None = None

    async def start(self) -> None:
        """Begin background work (connecting). Idempotent."""

    async def stop(self) -> None:
        """Stop background work and close connections."""

    @property
    def connected(self) -> bool:
        return True

    @abstractmethod
    async def send(self, msg: dict) -> str | None:
        """Send one state message.

        Returns the ack ``result`` string when it is known synchronously (HTTP), else None (WS acks
        arrive asynchronously via ``on_ack``). Raises :class:`TransportError` if not delivered.
        """


def _log_ack(msg: dict) -> None:
    if msg.get("type") == "ack":
        if msg.get("result") == "invalid":
            log.error("sign rejected seq=%s: %s field=%s", msg.get("seq"), msg.get("error"), msg.get("field"))
        elif msg.get("result") in ("duplicate", "stale"):
            log.warning("sign answered %s for seq=%s", msg.get("result"), msg.get("seq"))
        else:
            log.debug("ack %s", msg)


class WsTransport(Transport):
    """Primary transport: ``ws://host/ws``. Token goes in the first (hello) frame."""

    def __init__(
        self,
        url: str,
        token: str,
        agent: str = AGENT_NAME,
        *,
        hello_timeout: float = 2.0,
        connect_timeout: float = 3.0,
        backoff=BACKOFF_S,
        auth_failed_delay: float = AUTH_FAILED_DELAY_S,
        ack_timeout: float = 5.0,
        rng: random.Random | None = None,
    ):
        if websockets is None:
            raise RuntimeError("the 'websockets' package is required for WsTransport")
        self.url = url
        self.token = token
        self.agent = agent
        self.hello_timeout = hello_timeout
        self.connect_timeout = connect_timeout
        self.backoff = backoff
        self.auth_failed_delay = auth_failed_delay
        self.ack_timeout = ack_timeout
        self._rng = rng or random.Random()
        self._task: asyncio.Task | None = None
        self._ws = None
        self._connected = False
        self._last_send = 0.0
        self._last_recv = 0.0
        self.auth_failed = False
        self.hello_ack: dict | None = None
        self.supported_v: list[int] = []
        self.last_ack: dict | None = None
        self.connect_count = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._connected = False

    async def send(self, msg: dict) -> str | None:
        ws = self._ws
        if not self._connected or ws is None:
            if self.auth_failed:
                raise TransportAuthError("sign refused the token (auth_failed)")
            raise TransportError("websocket not connected")
        try:
            await ws.send(dumps(msg))
        except (ConnectionClosed, OSError) as e:
            raise TransportError(f"websocket send failed: {e}") from e
        self._last_send = asyncio.get_running_loop().time()
        return None

    # -- connection loop -------------------------------------------------------------------------
    async def _run(self) -> None:
        attempt = 0
        while True:
            outcome = "error"
            try:
                outcome = await self._session()
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError, WebSocketException) as e:
                log.warning("websocket %s: %s", self.url, e or type(e).__name__)
            except Exception:
                log.exception("unexpected websocket error")
            finally:
                self._connected = False
                self._ws = None

            if outcome == "connected_then_closed":
                attempt = 0
                outcome = "closed"
            if outcome in ("auth_failed", "unsupported_version"):
                delay = self.auth_failed_delay
            else:
                delay = backoff_delay(attempt, self.backoff, self._rng)
                attempt += 1
            log.info("reconnecting to %s in %.1f s (%s)", self.url, delay, outcome)
            await asyncio.sleep(delay)

    async def _session(self) -> str:
        async with websockets.connect(
            self.url, open_timeout=self.connect_timeout, ping_interval=None, max_size=65536
        ) as ws:
            await ws.send(dumps({"type": "hello", "v": PROTOCOL_VERSION, "token": self.token, "agent": self.agent}))
            try:
                raw = await asyncio.wait_for(ws.recv(), self.hello_timeout)
            except asyncio.TimeoutError:
                log.warning("no hello_ack from sign within %.1f s", self.hello_timeout)
                return "hello_ack_timeout"
            except ConnectionClosed:
                log.warning("sign closed the connection during hello")
                return "closed_during_hello"
            msg = self._parse(raw)
            if msg is None:
                log.warning("unparsable reply to hello: %r", raw)
                return "bad_hello_ack"
            if msg.get("type") == "error":
                return self._handle_error(msg)
            if msg.get("type") != "hello_ack":
                log.warning("unexpected reply to hello: %s", msg)
                return "bad_hello_ack"
            supported = msg.get("supported_v") or []
            if PROTOCOL_VERSION not in supported:
                log.error("sign supports protocol versions %s, this agent only speaks v%d", supported, PROTOCOL_VERSION)
                return "unsupported_version"

            self.hello_ack = msg
            self.supported_v = list(supported)
            self.auth_failed = False
            self._ws = ws
            self._connected = True
            self.connect_count += 1
            loop = asyncio.get_running_loop()
            self._last_send = self._last_recv = loop.time()
            log.info(
                "connected to %s (device=%s fw=%s mode=%s session=%s last_seq=%s)",
                self.url, msg.get("device"), msg.get("fw"), msg.get("mode"), msg.get("session"), msg.get("last_seq"),
            )
            if self.on_connected:
                self.on_connected()
            return await self._read_loop(ws)

    async def _read_loop(self, ws) -> str:
        loop = asyncio.get_running_loop()
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), 1.0)
            except asyncio.TimeoutError:
                # Half-open socket watchdog: we keep sending but the sign never acks.
                if self._last_send > self._last_recv and loop.time() - self._last_send > self.ack_timeout:
                    log.warning("no ack from sign for %.0f s, reconnecting", self.ack_timeout)
                    return "connected_then_closed"
                continue
            except ConnectionClosed as e:
                log.info("websocket closed: %s", e)
                return "connected_then_closed"
            self._last_recv = loop.time()
            msg = self._parse(raw)
            if msg is None:
                continue
            kind = msg.get("type")
            if kind == "ack":
                self.last_ack = msg
                _log_ack(msg)
                if self.on_ack:
                    self.on_ack(msg)
            elif kind == "error":
                outcome = self._handle_error(msg)
                if self.on_ack:
                    self.on_ack(msg)
                # the sign closes the socket after an error frame; do not wait for it
                return "connected_then_closed" if outcome == "bad_message" else outcome
            else:
                log.debug("ignoring frame %s", msg)

    @staticmethod
    def _parse(raw) -> dict | None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return msg if isinstance(msg, dict) else None

    def _handle_error(self, msg: dict) -> str:
        code = msg.get("code")
        if code == "auth_failed":
            self.auth_failed = True
            log.error(
                "AUTH FAILED: the sign rejected the token. Check --token / BRACKETBOARD_TOKEN "
                "(printed on the sign's serial console / splash). Retrying in %.0f s.",
                self.auth_failed_delay,
            )
        elif code == "replaced":
            log.warning(
                "REPLACED: another controller authenticated to this sign and took over. "
                "Is a second agent running? Reconnecting with backoff (this will take control back)."
            )
        elif code == "hello_timeout":
            log.warning("sign reported hello_timeout (hello not received within 2 s)")
        elif code == "unsupported_version":
            log.error("sign reported unsupported_version: it does not speak protocol v%d", PROTOCOL_VERSION)
        else:
            log.warning("sign reported error: %s", code)
        return str(code)


class HttpTransport(Transport):
    """Fallback transport: ``POST /state`` with ``Authorization: Bearer <token>``."""

    def __init__(self, url: str, token: str, timeout: float = 1.0):
        base = url.rstrip("/")
        self.url = base if base.endswith("/state") else base + "/state"
        self.token = token
        self.timeout = timeout
        self.last_ack: dict | None = None

    async def send(self, msg: dict) -> str | None:
        loop = asyncio.get_running_loop()
        body = dumps(msg).encode("ascii")
        return await loop.run_in_executor(None, self._post, body)

    def _post(self, body: bytes) -> str | None:
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 401:
                log.error("AUTH FAILED: sign answered 401 to POST /state (check the token)")
                raise TransportAuthError("HTTP 401") from e
            if e.code in (400, 413):
                log.error("sign rejected snapshot (HTTP %d): %s", e.code, detail)
                return "invalid"
            raise TransportError(f"HTTP {e.code}: {detail[:100]}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise TransportError(f"HTTP request failed: {getattr(e, 'reason', e)}") from e

        try:
            ack = json.loads(data)
        except ValueError:
            return None
        if not isinstance(ack, dict):
            return None
        self.last_ack = ack
        result = ack.get("result")
        return result if isinstance(result, str) else None


class FailoverTransport(Transport):
    """WS while connected, otherwise HTTP while WS reconnects in the background. Never both.

    ``ws`` may be None (websockets package missing): then this is HTTP-only, with a warning.
    """

    def __init__(self, ws: WsTransport | None, http: HttpTransport):
        self.ws = ws
        self.http = http
        if ws is None:
            log.warning("websockets not available: using HTTP only")
        else:
            ws.on_connected = self._ws_connected
            ws.on_ack = self._ws_ack

    def _ws_connected(self) -> None:
        if self.on_connected:
            self.on_connected()

    def _ws_ack(self, msg: dict) -> None:
        if self.on_ack:
            self.on_ack(msg)

    @property
    def connected(self) -> bool:
        return self.ws.connected if self.ws is not None else True

    async def start(self) -> None:
        if self.ws is not None:
            await self.ws.start()

    async def stop(self) -> None:
        if self.ws is not None:
            await self.ws.stop()

    async def send(self, msg: dict) -> str | None:
        if self.ws is not None and self.ws.connected:
            try:
                return await self.ws.send(msg)
            except TransportError:
                pass  # frame was not delivered; fall back to HTTP for this snapshot
        return await self.http.send(msg)
