import asyncio
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from websockets.asyncio.server import serve

from bracketboard_agent.schema import BoutState, to_message
from bracketboard_agent.transport import (
    BACKOFF_S,
    FailoverTransport,
    HttpTransport,
    Transport,
    TransportAuthError,
    TransportError,
    WsTransport,
    backoff_delay,
)


def run(coro):
    return asyncio.run(coro)


def msg(seq=1):
    return to_message(BoutState(), "sess", seq, 1)


async def until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


# --- backoff ---------------------------------------------------------------------------------------
def test_backoff_schedule_with_jitter():
    rng = random.Random(1)
    for attempt, base in enumerate([0.5, 1, 2, 4, 5, 5, 5, 5]):
        for _ in range(20):
            d = backoff_delay(attempt, rng=rng)
            assert base <= d <= base * 1.25
    assert BACKOFF_S == (0.5, 1.0, 2.0, 4.0, 5.0)


# --- WebSocket against a scripted fake sign ----------------------------------------------------
class FakeSign:
    """Minimal protocol-compliant WS server; ``behaviour`` picks what it does after hello."""

    def __init__(self, token="tok", behaviour="ok"):
        self.token = token
        self.behaviour = behaviour
        self.hellos: list[dict] = []
        self.states: list[dict] = []
        self.connections = 0
        self.server = None
        self.port = 0

    async def handler(self, ws):
        self.connections += 1
        if self.behaviour == "silent":
            await ws.wait_closed()
            return
        hello = json.loads(await ws.recv())
        self.hellos.append(hello)
        if hello.get("token") != self.token or self.behaviour == "auth_failed":
            await ws.send(json.dumps({"type": "error", "code": "auth_failed"}))
            await ws.close()
            return
        if self.behaviour == "badver":
            await ws.send(json.dumps({"type": "hello_ack", "supported_v": [2], "device": "scoreboard"}))
            await ws.wait_closed()
            return
        await ws.send(json.dumps({
            "type": "hello_ack", "supported_v": [1], "device": "scoreboard", "fw": "0.1.0",
            "mode": "agent", "session": None, "last_seq": None,
        }))
        if self.behaviour == "replace_once" and self.connections == 1:
            await asyncio.sleep(0.05)
            await ws.send(json.dumps({"type": "error", "code": "replaced"}))
            await ws.close()
            return
        if self.behaviour == "drop_once" and self.connections == 1:
            await ws.close()
            return
        async for raw in ws:
            m = json.loads(raw)
            self.states.append(m)
            await ws.send(json.dumps({"type": "ack", "seq": m["seq"], "result": "applied"}))

    async def __aenter__(self):
        self.server = await serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/ws"


def make_ws(sign, **kw):
    kw.setdefault("backoff", (0.02, 0.04))
    kw.setdefault("hello_timeout", 0.3)
    kw.setdefault("auth_failed_delay", 5.0)
    return WsTransport(sign.url, kw.pop("token", sign.token), **kw)


def test_ws_hello_send_and_ack():
    async def go():
        async with FakeSign() as sign:
            t = make_ws(sign)
            connected, acks = [], []
            t.on_connected = lambda: connected.append(1)
            t.on_ack = acks.append
            assert not t.connected
            with pytest.raises(TransportError):
                await t.send(msg())
            await t.start()
            assert await until(lambda: t.connected)
            assert connected == [1]
            assert sign.hellos[0]["type"] == "hello" and sign.hellos[0]["v"] == 1
            assert sign.hellos[0]["token"] == "tok" and "agent" in sign.hellos[0]
            assert t.supported_v == [1] and t.hello_ack["device"] == "scoreboard"
            assert await t.send(msg(5)) is None
            assert await until(lambda: acks)
            assert acks[0] == {"type": "ack", "seq": 5, "result": "applied"}
            assert sign.states[0]["seq"] == 5
            await t.stop()
            assert not t.connected

    run(go())


def test_ws_auth_failed_does_not_hot_loop_and_send_raises_auth_error():
    async def go():
        async with FakeSign(token="right") as sign:
            t = make_ws(sign, token="wrong")
            await t.start()
            assert await until(lambda: t.auth_failed)
            await asyncio.sleep(0.5)  # a hot loop would have reconnected dozens of times
            assert sign.connections == 1
            with pytest.raises(TransportAuthError):
                await t.send(msg())
            assert not t.connected
            await t.stop()

    run(go())


def test_ws_auth_failed_logs_clearly(caplog):
    async def go():
        async with FakeSign(behaviour="auth_failed") as sign:
            t = make_ws(sign)
            await t.start()
            await until(lambda: t.auth_failed)
            await t.stop()

    with caplog.at_level("ERROR", logger="bracketboard.transport"):
        run(go())
    assert any("AUTH FAILED" in r.message for r in caplog.records)


def test_ws_replaced_reconnects_with_backoff_and_logs(caplog):
    async def go():
        async with FakeSign(behaviour="replace_once") as sign:
            t = make_ws(sign)
            connected = []
            t.on_connected = lambda: connected.append(1)
            await t.start()
            assert await until(lambda: len(connected) == 2)
            assert sign.connections == 2
            await t.send(msg())
            assert await until(lambda: sign.states)
            await t.stop()

    with caplog.at_level("WARNING", logger="bracketboard.transport"):
        run(go())
    assert any("REPLACED" in r.message for r in caplog.records)


def test_ws_reconnects_after_server_close():
    async def go():
        async with FakeSign(behaviour="drop_once") as sign:
            t = make_ws(sign)
            n = []
            t.on_connected = lambda: n.append(1)
            await t.start()
            assert await until(lambda: len(n) == 2)
            await t.stop()

    run(go())


def test_ws_hello_ack_timeout_retries():
    async def go():
        async with FakeSign(behaviour="silent") as sign:
            t = make_ws(sign, hello_timeout=0.1)
            await t.start()
            assert await until(lambda: sign.connections >= 2)
            assert not t.connected
            await t.stop()

    run(go())


def test_ws_unsupported_version_not_connected():
    async def go():
        async with FakeSign(behaviour="badver") as sign:
            t = make_ws(sign, auth_failed_delay=5.0)
            await t.start()
            await asyncio.sleep(0.3)
            assert not t.connected and sign.connections == 1
            await t.stop()

    run(go())


def test_ws_connection_refused_keeps_retrying():
    async def go():
        t = WsTransport("ws://127.0.0.1:1/ws", "x", backoff=(0.02,), connect_timeout=0.5)
        await t.start()
        await asyncio.sleep(0.2)
        assert not t.connected
        await t.stop()

    run(go())


# --- HTTP ------------------------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        srv = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        srv.requests.append((self.path, self.headers.get("Authorization"), body))
        if srv.delay:
            time.sleep(srv.delay)
        self.send_response(srv.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(srv.reply)


@pytest.fixture
def http_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests, srv.status, srv.delay = [], 200, 0
    srv.reply = b'{"type":"ack","seq":1,"result":"applied"}'
    th = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True)
    th.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def base_url(srv):
    return f"http://127.0.0.1:{srv.server_address[1]}"


def test_http_200_returns_result_and_sends_bearer(http_server):
    t = HttpTransport(base_url(http_server), "sekret")
    assert run(t.send(msg(1))) == "applied"
    path, auth, body = http_server.requests[0]
    assert path == "/state" and auth == "Bearer sekret"
    assert json.loads(body)["seq"] == 1
    assert b" " not in body  # compact


@pytest.mark.parametrize("result", ["duplicate", "stale"])
def test_http_other_ack_results(http_server, result):
    http_server.reply = json.dumps({"type": "ack", "seq": 1, "result": result}).encode()
    assert run(HttpTransport(base_url(http_server), "t").send(msg())) == result


def test_http_400_is_invalid(http_server):
    http_server.status = 400
    http_server.reply = b'{"type":"ack","seq":1,"result":"invalid","error":"out_of_range","field":"period"}'
    assert run(HttpTransport(base_url(http_server), "t").send(msg())) == "invalid"


def test_http_401_raises_auth_error(http_server):
    http_server.status = 401
    http_server.reply = b"{}"
    with pytest.raises(TransportAuthError):
        run(HttpTransport(base_url(http_server), "bad").send(msg()))


def test_http_timeout_and_refused_raise_transport_error(http_server):
    http_server.delay = 1.0
    t = HttpTransport(base_url(http_server), "t", timeout=0.2)
    start = time.monotonic()
    with pytest.raises(TransportError):
        run(t.send(msg()))
    assert time.monotonic() - start < 0.9
    with pytest.raises(TransportError):
        run(HttpTransport("http://127.0.0.1:1", "t", timeout=0.5).send(msg()))


def test_http_500_raises_transport_error(http_server):
    http_server.status = 500
    with pytest.raises(TransportError) as ei:
        run(HttpTransport(base_url(http_server), "t").send(msg()))
    assert not isinstance(ei.value, TransportAuthError)


def test_http_url_normalisation():
    assert HttpTransport("http://h", "t").url == "http://h/state"
    assert HttpTransport("http://h/", "t").url == "http://h/state"
    assert HttpTransport("http://h/state", "t").url == "http://h/state"


# --- failover --------------------------------------------------------------------------------------
class Fake(Transport):
    def __init__(self, connected=True, fail=False, result=None):
        self._connected, self.fail, self.result = connected, fail, result
        self.sent = []

    @property
    def connected(self):
        return self._connected

    async def send(self, m):
        if self.fail:
            raise TransportError("x")
        self.sent.append(m)
        return self.result


def test_failover_uses_ws_when_connected_never_both():
    ws, http = Fake(True), Fake(True, result="applied")
    f = FailoverTransport(ws, http)
    run(f.send(msg()))
    assert len(ws.sent) == 1 and http.sent == []


def test_failover_uses_http_when_ws_down():
    ws, http = Fake(False), Fake(True, result="applied")
    f = FailoverTransport(ws, http)
    assert run(f.send(msg())) == "applied"
    assert ws.sent == [] and len(http.sent) == 1


def test_failover_falls_back_when_ws_send_fails():
    ws, http = Fake(True, fail=True), Fake(True, result="applied")
    f = FailoverTransport(ws, http)
    assert run(f.send(msg())) == "applied"
    assert len(http.sent) == 1


def test_failover_http_only_without_websockets(caplog):
    http = Fake(True, result="applied")
    with caplog.at_level("WARNING", logger="bracketboard.transport"):
        f = FailoverTransport(None, http)
    assert any("HTTP only" in r.message for r in caplog.records)
    assert run(f.send(msg())) == "applied"
    run(f.start())
    run(f.stop())


def test_failover_forwards_connect_and_ack_callbacks():
    ws, http = Fake(False), Fake(True)
    f = FailoverTransport(ws, http)
    seen = []
    f.on_connected = lambda: seen.append("c")
    f.on_ack = seen.append
    ws.on_connected()
    ws.on_ack({"type": "ack"})
    assert seen == ["c", {"type": "ack"}]


def test_failover_with_real_ws_and_http(http_server):
    async def go():
        async with FakeSign() as sign:
            ws = WsTransport(sign.url, "tok", backoff=(0.02,))
            http = HttpTransport(base_url(http_server), "tok")
            f = FailoverTransport(ws, http)
            connected = []
            f.on_connected = lambda: connected.append(1)
            # before WS is up: HTTP
            assert await f.send(msg(1)) == "applied"
            await f.start()
            assert await until(lambda: connected)
            await f.send(msg(2))
            assert await until(lambda: sign.states)
            await f.stop()

    run(go())
    assert len(http_server.requests) == 1
