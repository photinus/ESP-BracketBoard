"""End-to-end: real agent code (Publisher + FailoverTransport + MockBoutAdapter) against ``fake_esp``
wired to the real C++ core through ``bb_core_cli``. Skipped when the C++ bridge is not built."""
import asyncio
import inspect
import json
import time
import urllib.error
import urllib.request

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from fake_esp import FakeESP
from bracketboard_agent.adapters.mock import DEFAULT_SCRIPT, MockBoutAdapter
from bracketboard_agent.publisher import Publisher
from bracketboard_agent.transport import FailoverTransport, HttpTransport, TransportAuthError, WsTransport

pytestmark = pytest.mark.e2e

TOKEN = "e2etoken"


def run(coro, timeout=15.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def until(pred, timeout=3.0, interval=0.02, what="condition"):
    """Poll ``pred`` (sync or async) until truthy; returns its value or fails the test."""
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if inspect.isawaitable(v):
            v = await v
        if v:
            return v
        if time.monotonic() >= end:
            pytest.fail(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(interval)


def write_script(tmp_path, events, name="script.json"):
    p = tmp_path / name
    p.write_text(json.dumps(events))
    return p


def clock_s(frame):
    m, s = frame["clock"].split(":")
    return int(m) * 60 + int(s)


class PausableMock(MockBoutAdapter):
    """Mock bout whose source can be made 'unavailable' (read_state -> None) to simulate a paused agent."""

    paused = False

    async def read_state(self):
        return None if self.paused else await super().read_state()


class Agent:
    """The real agent stack, with fast reconnect timings for tests."""

    def __init__(self, esp, script=None, speed=1.0, heartbeat=0.1, token=TOKEN, ws_backoff=(0.05, 0.1)):
        self.adapter = PausableMock(script, speed=speed)
        self.ws = WsTransport(esp.ws_url, token, backoff=ws_backoff, hello_timeout=0.5, auth_failed_delay=5.0)
        self.http = HttpTransport(esp.http_url, token, timeout=1.0)
        self.transport = FailoverTransport(self.ws, self.http)
        self.acks: list[dict] = []
        self.transport.on_ack = self.acks.append
        self.pub = Publisher(self.adapter, self.transport, heartbeat_s=heartbeat)
        self.task = None

    async def start(self):
        self.task = asyncio.ensure_future(self.pub.run())
        return self

    async def stop(self):
        self.pub.stop()
        await asyncio.wait_for(self.task, 3.0)

    async def kill(self):
        """Hard stop: cancel the task, no graceful publisher shutdown."""
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def http_request(esp, method, path, body=None, token=TOKEN):
    """Returns (status, parsed-json-or-text)."""

    def go():
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            esp.http_url + path, data=None if body is None else json.dumps(body).encode(), method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                status, data = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, data = e.code, e.read()
        try:
            return status, json.loads(data)
        except ValueError:
            return status, data.decode()

    return await asyncio.to_thread(go)


def esp_for(core_cli, **kw):
    kw.setdefault("token", TOKEN)
    return FakeESP("127.0.0.1", 0, core_cli=core_cli, **kw)


# --- 1. whole bout at high speed -------------------------------------------------------------------
def test_bout_runs_to_completion(core_cli):
    class Fake:
        t = 0.0

        def __call__(self):
            return self.t

    # what the mock's final state is, computed independently with a fake clock
    c = Fake()
    ref = MockBoutAdapter(DEFAULT_SCRIPT, clock=c)
    asyncio.run(ref.start())  # t0 = 0
    c.t = 10_000.0
    final = asyncio.run(ref.read_state())
    assert ref.finished and final.clock_state == "expired" and final.clock_ms_remaining == 0

    async def go():
        async with esp_for(core_cli) as esp:
            agent = await Agent(esp, DEFAULT_SCRIPT, speed=250).start()
            try:
                await until(lambda: agent.adapter.finished, 5.0, what="mock bout to finish")

                async def at_final():
                    f = await esp.frame()
                    return f["screen"] == final.screen and f["left_score"] == final.left_score and f["clock_state"] == "expired"

                await until(at_final, 3.0, what="final frame")
                f = await esp.frame()
                assert (f["left_score"], f["right_score"]) == (final.left_score, final.right_score) == (7, 4)
                assert (f["left_warnings"], f["right_warnings"]) == (final.left_warnings, final.right_warnings)
                assert f["period"] == final.period == 3
                assert f["clock"] == "0:00" and f["clock_state"] == "expired"
                assert f["screen"] == "splash" and f["weight"] == "157" and f["bout"] == "1203"
                assert f["colors_swapped"] is True
                assert not f["stale"] and not f["waiting"] and not f["manual"]
                bad = [a for a in agent.acks if a.get("type") != "ack" or a.get("result") != "applied"]
                assert not bad, bad
                assert esp.stats["ws_state"] > 10 and esp.stats["http_state"] <= 1  # only the very first snapshot may precede the WS handshake
                assert esp.error is None
            finally:
                await agent.stop()

    run(go())


# --- 2. agent restart / replaced ---------------------------------------------------------------------
def test_agent_restart_takes_over(core_cli, tmp_path):
    script_a = write_script(tmp_path, [{"t": 0, "do": "weight", "value": "AAA"}, {"t": 0, "do": "score", "side": "left", "points": 5},
                                       {"t": 0, "do": "start"}], "a.json")
    script_b = write_script(tmp_path, [{"t": 0, "do": "weight", "value": "BBB"}, {"t": 0, "do": "score", "side": "right", "points": 2},
                                       {"t": 0, "do": "start"}], "b.json")

    async def go():
        async with esp_for(core_cli) as esp:
            # a stale authenticated connection (e.g. half-open socket of a crashed agent)
            async with connect(esp.ws_url) as ghost:
                await ghost.send(json.dumps({"type": "hello", "v": 1, "token": TOKEN, "agent": "ghost"}))
                assert json.loads(await ghost.recv())["type"] == "hello_ack"

                a = await Agent(esp, script_a).start()
                try:
                    # the new agent authenticates and replaces the ghost
                    err = json.loads(await asyncio.wait_for(ghost.recv(), 3.0))
                    assert err == {"type": "error", "code": "replaced"}
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(ghost.recv(), 3.0)
                    await until(lambda: _weight_is(esp, "AAA"), 3.0, what="agent A snapshot")
                    # let a few heartbeats go by so A's seq is well above 1
                    await until(lambda: a.pub.seq > 5, 3.0, what="A seq > 5")
                finally:
                    await a.kill()  # killed: no goodbye, nothing stops its seq from being high

            a_session = a.pub.session
            a_last_seq = a.pub.seq - 1
            b = await Agent(esp, script_b).start()
            try:
                assert b.pub.session != a_session
                await until(lambda: _weight_is(esp, "BBB"), 3.0, what="agent B snapshot applied")
                f = await esp.frame()
                assert f["right_score"] == 2 and f["left_score"] == 0
                st = await esp.status()
                assert st["session"] == b.pub.session
                assert st["last_seq"] <= b.pub.seq - 1 < a_last_seq + 50
                first_b = next(m for via, m in esp.accepted if m["session"] == b.pub.session)
                assert first_b["seq"] == 1  # seq restarts at 1 in a new session and is accepted
                assert [a_["result"] for a_ in b.acks if a_.get("type") == "ack"][:1] == ["applied"]
            finally:
                await b.stop()

    run(go())


async def _weight_is(esp, w):
    return (await esp.frame())["weight"] == w


# --- 3. paused agent -> stale -> resume ------------------------------------------------------------------
def test_stale_freezes_clock_and_recovers(core_cli, tmp_path):
    script = write_script(tmp_path, [{"t": 0, "do": "weight", "value": "STL"}, {"t": 0, "do": "start"}])

    async def go():
        async with esp_for(core_cli, stale_ms=600) as esp:
            agent = await Agent(esp, script, heartbeat=0.1).start()
            try:
                async def live():
                    f = await esp.frame()
                    return f if (not f["waiting"] and not f["stale"] and f["clock_state"] == "running") else None

                f0 = await until(live, 3.0, what="live running frame")
                assert f0["colon_on"] is True and f0["weight"] == "STL"
                await asyncio.sleep(0.5)  # longer than the stale timeout: heartbeats must keep it live
                f1 = await esp.frame()
                assert f1["stale"] is False and f1["colon_on"] is True

                agent.adapter.paused = True  # agent goes quiet
                async def stale():
                    f = await esp.frame()
                    return f if f["stale"] else None

                fs = await until(stale, 3.0, what="stale frame")
                assert fs["colon_on"] is False
                assert fs["clock_state"] == "running" and fs["weight"] == "STL"  # other fields keep their value
                await asyncio.sleep(1.2)  # the agent clock keeps running meanwhile
                fs2 = await esp.frame()
                assert fs2["stale"] is True and fs2["colon_on"] is False
                assert fs2["clock"] == fs["clock"]  # frozen, no drift while stale

                agent.adapter.paused = False
                f2 = await until(lambda: live(), 3.0, what="recovered frame")
                assert f2["colon_on"] is True and f2["stale"] is False
                assert clock_s(f2) < clock_s(fs)  # clock snapped to the agent's current value
            finally:
                await agent.stop()

    run(go())


# --- 4. WS lost mid-bout: HTTP fallback, then back to WS -------------------------------------------------
def test_ws_drop_falls_back_to_http_then_returns(core_cli, tmp_path):
    script = write_script(tmp_path, [
        {"t": 0, "do": "start"},
        {"t": 1.0, "do": "score", "side": "left", "points": 1},
        {"t": 2.2, "do": "score", "side": "right", "points": 1},
    ])

    async def go():
        async with esp_for(core_cli) as esp:
            agent = await Agent(esp, script).start()
            try:
                await until(lambda: agent.ws.connected and esp.stats["ws_state"] >= 1, 3.0, what="WS up")
                # (the very first snapshot may legitimately go out over HTTP before the WS handshake is done)
                h_start = esp.stats["http_state"]
                await asyncio.sleep(0.3)
                assert esp.stats["http_state"] == h_start  # WS is used while it is up

                esp.ws_enabled = False  # sign stops accepting upgrades and drops the live socket
                await esp.drop_ws()
                await until(lambda: not agent.ws.connected, 2.0, what="agent to notice the drop")
                await until(lambda: esp.stats["http_state"] >= 2, 3.0, what="HTTP fallback snapshots")
                st = await esp.status()
                assert st["agent_live"] is True
                # frames keep updating over HTTP: the scripted score at t=1 s arrives
                async def left_scored():
                    f = await esp.frame()
                    return f["left_score"] == 1 and not f["stale"]

                await until(left_scored, 3.0, what="score update delivered over HTTP")
                assert not agent.ws.connected and esp.stats["ws_refused"] >= 1

                esp.ws_enabled = True  # WS available again
                await until(lambda: agent.ws.connected, 3.0, what="WS reconnect")
                ws0 = esp.stats["ws_state"]
                await until(lambda: esp.stats["ws_state"] >= ws0 + 3, 3.0, what="snapshots over WS again")
                h0 = esp.stats["http_state"]
                await asyncio.sleep(0.4)
                assert esp.stats["http_state"] == h0  # never both: HTTP is idle once WS is back
                await until(lambda: _right_scored(esp), 3.0, what="second score applied")
                f = await esp.frame()
                assert f["left_score"] == 1 and f["right_score"] == 1 and not f["stale"]
                assert esp.error is None
            finally:
                await agent.stop()

    run(go())


async def _right_scored(esp):
    return (await esp.frame())["right_score"] == 1


# --- 5. wrong token -------------------------------------------------------------------------------------
def test_wrong_token_is_rejected(core_cli):
    async def go():
        async with esp_for(core_cli) as esp:
            # raw WS: auth_failed then close
            async with connect(esp.ws_url) as ws:
                await ws.send(json.dumps({"type": "hello", "v": 1, "token": "nope", "agent": "t"}))
                assert json.loads(await asyncio.wait_for(ws.recv(), 3.0)) == {"type": "error", "code": "auth_failed"}
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(ws.recv(), 3.0)
            # raw HTTP: 401 for bad and missing credentials
            assert (await http_request(esp, "POST", "/state", {"type": "state"}, token="nope"))[0] == 401
            assert (await http_request(esp, "POST", "/state", {"type": "state"}, token=None))[0] == 401
            # /status needs no auth and never leaks the token
            code, st = await http_request(esp, "GET", "/status", token=None)
            assert code == 200 and st["agent_live"] is False and TOKEN not in json.dumps(st)

            agent = await Agent(esp, token="nope").start()
            try:
                await until(lambda: agent.ws.auth_failed, 3.0, what="agent to see auth_failed")
                await until(lambda: agent.pub.last_reason is None and agent.pub.seq > 1, 3.0, what="a rejected HTTP attempt")
                with pytest.raises(TransportAuthError):
                    await agent.http.send({"type": "state"})
                f = await esp.frame()
                assert f["waiting"] is True and f["stale"] is False
                st = await esp.status()
                assert st["agent_live"] is False and st["session"] is None
                assert esp.accepted == [] and agent.pub.sent_count == 0
            finally:
                await agent.stop()

    run(go())


# --- 6. manual mode gating ---------------------------------------------------------------------------------
def test_manual_mode_gating(core_cli, tmp_path):
    script = write_script(tmp_path, [{"t": 0, "do": "weight", "value": "AGT"}, {"t": 0, "do": "start"}])
    left_plus_one = {"cmd": "score", "side": "left", "delta": 1}

    async def go():
        async with esp_for(core_cli, stale_ms=600, manual=True) as esp:
            # no agent yet: manual works
            code, body = await http_request(esp, "POST", "/manual/cmd", left_plus_one)
            assert (code, body) == (200, {"result": "applied"})
            f = await esp.frame()
            assert f["manual"] is True and f["left_score"] == 1
            assert (await http_request(esp, "POST", "/manual/cmd", left_plus_one, token="bad"))[0] == 401

            agent = await Agent(esp, script).start()
            try:
                async def agent_shows():
                    f = await esp.frame()
                    return f["weight"] == "AGT" and not f["manual"] and not f["stale"]

                await until(agent_shows, 3.0, what="agent to take over the display")
                code, body = await http_request(esp, "POST", "/manual/cmd", left_plus_one)
                assert (code, body) == (409, {"result": "manual_locked"})
                assert (await esp.frame())["left_score"] == 0  # agent's value, manual state untouched
            finally:
                await agent.stop()

            async def manual_back():
                f = await esp.frame()
                return f["manual"] and f["left_score"] == 1  # earlier manual state was kept

            await until(manual_back, 3.0, what="manual state to show again after the agent went stale")
            code, body = await http_request(esp, "POST", "/manual/cmd", left_plus_one)
            assert (code, body) == (200, {"result": "applied"})
            assert (await esp.frame())["left_score"] == 2

    run(go())
