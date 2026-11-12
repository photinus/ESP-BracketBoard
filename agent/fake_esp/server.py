"""FakeESP: a stand-in for the sign's network stack in front of the real C++ core.

One TCP port serves both WebSocket ``/ws`` and plain HTTP (``POST /state``, ``POST /manual/cmd``,
``GET /status``, plus ``GET /frame`` for debugging), like the real sign on port 80. A small asyncio
dispatcher reads the request head: ``Upgrade: websocket`` requests are piped to an internal real
``websockets`` server, everything else is answered as HTTP. Every WS open/text/close and every HTTP
request is forwarded to a :class:`CoreBridge` (``bb_core_cli``) and its Outbound frames are delivered
back to the right socket. A ~200 ms tick loop feeds ``hub.tick`` (hello timeouts).

Test hooks: ``ws_enabled`` (False -> WS upgrades are refused with 503), :meth:`drop_ws`,
:meth:`frame`, :meth:`status`, ``stats`` counters and the ``accepted`` list.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from typing import Callable

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .bridge import BridgeError, CoreBridge, PurePythonBridge, find_core_cli

log = logging.getLogger("fake_esp")

_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict", 413: "Payload Too Large", 503: "Service Unavailable"}
MAX_HTTP_BODY = 1 << 20


class FakeESP:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str = "TOKEN",
        stale_ms: int = 3000,
        pure_python: bool = False,
        core_cli: str | os.PathLike | None = None,
        *,
        manual: bool = False,
        fake_time: bool = False,
        tick_ms: int = 200,
        on_accept: Callable[[str, dict], None] | None = None,
    ):
        self.host = host
        self._requested_port = port
        self.token = token
        self.stale_ms = stale_ms
        self.tick_s = tick_ms / 1000.0
        self.on_accept = on_accept
        if pure_python:
            self.bridge = PurePythonBridge(token, stale_ms, manual=manual)
        else:
            cli = str(core_cli) if core_cli else find_core_cli()
            if not cli:
                raise FileNotFoundError("bb_core_cli not found (build host/ or set BB_CORE_CLI)")
            self.bridge = CoreBridge(cli, token, stale_ms, fake_time=fake_time, manual=manual)
        self.ws_enabled = True
        self.stats = {"ws_state": 0, "http_state": 0, "ws_connects": 0, "ws_refused": 0, "http_requests": 0}
        self.accepted: list[tuple[str, dict]] = []  # (via "ws"|"http", state message) applied by the core
        self.error: BaseException | None = None
        self.port = 0
        self._ws_server = None
        self._front = None
        self._tick_task: asyncio.Task | None = None
        self._conns: dict[int, object] = {}
        self._conn_ids = itertools.count(1)
        self._http_ids = itertools.count(1)
        self._proxy_writers: set[asyncio.StreamWriter] = set()

    # -- ports / urls ----------------------------------------------------------------------------
    @property
    def ws_port(self) -> int:
        return self.port

    @property
    def http_port(self) -> int:
        return self.port

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws"

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_connection_count(self) -> int:
        return len(self._conns)

    # -- lifecycle -------------------------------------------------------------------------------
    async def start(self) -> "FakeESP":
        await self.bridge.start()
        self._ws_server = await serve(
            self._ws_handler, "127.0.0.1", 0, ping_interval=None, compression=None, max_size=1 << 20
        )
        inner_port = self._ws_server.sockets[0].getsockname()[1]
        self._inner_port = inner_port
        self._front = await asyncio.start_server(self._on_connect, self.host, self._requested_port)
        self.port = self._front.sockets[0].getsockname()[1]
        self._tick_task = asyncio.ensure_future(self._tick_loop())
        return self

    async def stop(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
            await asyncio.gather(self._tick_task, return_exceptions=True)
            self._tick_task = None
        if self._front:
            self._front.close()
        for w in list(self._proxy_writers):
            w.close()
        if self._ws_server:
            self._ws_server.close()
            try:
                await asyncio.wait_for(self._ws_server.wait_closed(), 3.0)
            except asyncio.TimeoutError:
                pass
        if self._front:
            try:
                await asyncio.wait_for(self._front.wait_closed(), 1.0)
            except asyncio.TimeoutError:
                pass
            self._front = None
        await self.bridge.stop()

    async def __aenter__(self) -> "FakeESP":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # -- bridge passthroughs ---------------------------------------------------------------------
    async def frame(self) -> dict:
        return await self.bridge.frame()

    async def status(self) -> dict:
        return await self.bridge.status()

    async def advance_time(self, ms: int) -> None:
        await self.bridge.advance_time(ms)

    async def drop_ws(self, hard: bool = False) -> None:
        """Close every open WebSocket from the sign side (``hard``: also reset the TCP sockets)."""
        for ws in list(self._conns.values()):
            try:
                await ws.close()
            except Exception:
                pass
        if hard:
            for w in list(self._proxy_writers):
                w.transport.abort()

    # -- delivery of Outbound frames -------------------------------------------------------------
    async def _deliver(self, outs: list[dict]) -> None:
        for o in outs:
            ws = self._conns.get(o["conn"])
            if ws is None:
                continue
            try:
                await ws.send(o["send"])
                if o["close"]:
                    await ws.close()
            except ConnectionClosed:
                pass

    def _accepted(self, via: str, state: dict) -> None:
        self.accepted.append((via, state))
        self.stats[f"{via}_state"] += 1
        if self.on_accept:
            try:
                self.on_accept(via, state)
            except Exception:
                log.exception("on_accept failed")

    async def _tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.tick_s)
                await self._deliver(await self.bridge.tick())
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # keep the first failure visible to tests
            self.error = e
            log.exception("tick loop died")

    # -- websocket side --------------------------------------------------------------------------
    async def _ws_handler(self, ws) -> None:
        conn = next(self._conn_ids)
        self._conns[conn] = ws
        self.stats["ws_connects"] += 1
        try:
            await self.bridge.open_ws(conn)
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                outs = await self.bridge.ws_text(conn, raw)
                self._observe_ws(conn, raw, outs)
                await self._deliver(outs)
        except ConnectionClosed:
            pass
        except BridgeError as e:
            self.error = self.error or e
        finally:
            self._conns.pop(conn, None)
            try:
                await self.bridge.close_ws(conn)
            except BridgeError:
                pass

    def _observe_ws(self, conn: int, raw: str, outs: list[dict]) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("type") != "state":
            return
        for o in outs:
            if o["conn"] != conn:
                continue
            try:
                ack = json.loads(o["send"])
            except ValueError:
                continue
            if ack.get("type") == "ack" and ack.get("result") == "applied":
                self._accepted("ws", msg)

    # -- front dispatcher (HTTP + WS upgrade on one port) ----------------------------------------
    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10.0)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
                return
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            if headers.get("upgrade", "").lower() == "websocket":
                await self._proxy_ws(head, reader, writer)
            else:
                await self._http(method, target.split("?", 1)[0], headers, reader, writer)
        except (ConnectionError, OSError):
            pass
        finally:
            if writer not in self._proxy_writers:
                writer.close()

    async def _proxy_ws(self, head: bytes, reader, writer) -> None:
        if not self.ws_enabled:
            self.stats["ws_refused"] += 1
            await self._respond(writer, 503, '{"error":"ws_disabled"}')
            return
        try:
            ir, iw = await asyncio.open_connection("127.0.0.1", self._inner_port)
        except OSError:
            await self._respond(writer, 503, '{"error":"ws_unavailable"}')
            return
        self._proxy_writers.add(writer)
        iw.write(head)

        async def pipe(r, w):
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except (ConnectionError, OSError, asyncio.CancelledError):
                pass
            finally:
                w.close()

        try:
            await asyncio.gather(pipe(reader, iw), pipe(ir, writer))
        finally:
            self._proxy_writers.discard(writer)
            writer.close()
            iw.close()

    async def _http(self, method: str, path: str, headers: dict, reader, writer) -> None:
        self.stats["http_requests"] += 1
        try:
            n = int(headers.get("content-length", "0") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_HTTP_BODY:
            await self._respond(writer, 413, '{"error":"too_large"}')
            return
        try:
            body = (await asyncio.wait_for(reader.readexactly(n), 10.0)).decode("utf-8", "replace") if n else ""
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return
        auth = headers.get("authorization")
        try:
            if method == "GET" and path == "/status":
                status, out = 200, json.dumps(await self.bridge.status(), separators=(",", ":"))
            elif method == "GET" and path == "/frame":
                status, out = 200, json.dumps(await self.bridge.frame(), separators=(",", ":"))
            elif method == "POST" and path in ("/state", "/manual/cmd"):
                status, out = await self.bridge.http_post(next(self._http_ids), path, auth, body)
                if path == "/state" and status == 200:
                    self._observe_http(body, out)
            elif path in ("/state", "/manual/cmd", "/status", "/frame"):
                status, out = 405, '{"error":"method_not_allowed"}'
            else:
                status, out = 404, '{"error":"not_found"}'
        except BridgeError as e:
            self.error = self.error or e
            status, out = 503, json.dumps({"error": "bridge", "detail": str(e)})
        await self._respond(writer, status, out)

    def _observe_http(self, body: str, reply: str) -> None:
        try:
            msg, ack = json.loads(body), json.loads(reply)
        except ValueError:
            return
        if isinstance(msg, dict) and isinstance(ack, dict) and ack.get("result") == "applied":
            self._accepted("http", msg)

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, body: str) -> None:
        data = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
        )
        try:
            writer.write(head.encode("ascii") + data)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
