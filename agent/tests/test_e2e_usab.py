"""End-to-end: fake matside (controller) + real UsabAdapter + real Publisher/FailoverTransport
+ FakeESP wired to the real C++ core (bb_core_cli). Skipped when the C++ bridge is not built."""
import asyncio

import pytest

from bracketboard_agent.adapters.usabracketing import USABracketingAdapter
from bracketboard_agent.publisher import Publisher
from bracketboard_agent.transport import FailoverTransport, HttpTransport, WsTransport
from fake_esp import FakeMatside
from test_e2e import TOKEN, clock_s, esp_for, run, until  # shared helpers (not collected twice: no test_ names)

pytestmark = pytest.mark.e2e


class UsabAgent:
    """The real agent stack reading a FakeMatside, with fast timings for tests."""

    def __init__(self, esp, fm, heartbeat=0.1, **adapter_kw):
        adapter_kw.setdefault("timeout", 0.4)
        self.adapter = USABracketingAdapter("127.0.0.1", fm.port, lookup_backoff=(0.05, 0.1), **adapter_kw)
        ws = WsTransport(esp.ws_url, TOKEN, backoff=(0.05, 0.1), hello_timeout=0.5)
        self.transport = FailoverTransport(ws, HttpTransport(esp.http_url, TOKEN, timeout=1.0))
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


@pytest.fixture
def fm():
    with FakeMatside() as f:
        f.set_event("77", "E2E-DEVICE-KEY")
        f.set_bout(None, "157", "1203", "9001")
        f.set_timer(name="Period 2", current_time=87.0, running=0, bout_id="1234", event_id="77",
                    w1_color="FF0000", w2_color="0000FF", w1_score=3, w2_score=1)
        yield f


def live(esp):
    async def check():
        f = await esp.frame()
        return f if (not f["waiting"] and not f["stale"]) else None

    return check


def test_sign_shows_mapped_values(core_cli, fm):
    async def go():
        async with esp_for(core_cli) as esp:
            agent = await UsabAgent(esp, fm).start()
            try:
                async def full():
                    f = await live(esp)()
                    return f if f and f["weight"] == "157" else None

                f = await until(full, 4.0, what="frame with weight")
                assert (f["left_score"], f["right_score"]) == (3, 1)  # red wrestler (w1) on the left
                assert f["clock"] == "1:27" and f["clock_state"] == "stopped" and f["period"] == 2
                assert (f["weight"], f["bout"], f["screen"]) == ("157", "1203", "score")
                assert f["colors_swapped"] is False and (f["left_warnings"], f["right_warnings"]) == (0, 0)

                # the controller makes wrestler 2 red: the scores swap sides on the sign
                fm.set_timer(w1_color="0000FF", w2_color="FF0000", w1_score=5, w2_score=4)

                async def swapped():
                    f = await esp.frame()
                    return f if (f["left_score"], f["right_score"]) == (4, 5) else None

                await until(swapped, 3.0, what="sides to swap")

                # a Break timer keeps the period; a new bout without selection shows the splash
                fm.set_timer(name="Break", current_time=30.0)
                await until(lambda: _frame_is(esp, clock="0:30"), 3.0, what="break clock")
                assert (await esp.frame())["period"] == 2

                fm.set_timer(bout_id="")
                await until(lambda: _frame_is(esp, screen="splash"), 3.0, what="splash")

                bad = [a for a in agent.acks if a.get("type") != "ack" or a.get("result") != "applied"]
                assert not bad, bad
                assert esp.error is None
            finally:
                await agent.stop()

    run(go())


async def _frame_is(esp, **want):
    f = await esp.frame()
    return all(f[k] == v for k, v in want.items())


def test_running_clock_counts_on_the_sign_without_controller_updates(core_cli, fm):
    fm.set_timer(running=1, current_time=6.0)  # the controller sends nothing from here on
    stamp = fm.timer().updated_millis

    async def go():
        async with esp_for(core_cli) as esp:
            agent = await UsabAgent(esp, fm).start()
            try:
                async def running():
                    f = await live(esp)()
                    return f if f and f["clock_state"] == "running" else None

                f0 = await until(running, 4.0, what="running frame")
                assert clock_s(f0) <= 6
                n0 = esp.stats["ws_state"] + esp.stats["http_state"]

                async def lower():
                    f = await esp.frame()
                    return f if clock_s(f) <= clock_s(f0) - 2 else None

                f1 = await until(lower, 4.0, what="the sign's clock to count down")
                assert f1["clock_state"] == "running" and not f1["stale"] and f1["colon_on"]
                assert esp.stats["ws_state"] + esp.stats["http_state"] > n0  # heartbeats kept it live
                assert fm.timer().updated_millis == stamp  # and the controller really pushed nothing
                assert "updateTimer" not in fm.request_log
            finally:
                await agent.stop()

    run(go())


def test_expiry_reaches_the_sign(core_cli, fm):
    fm.set_timer(running=1, current_time=1.5)

    async def go():
        async with esp_for(core_cli) as esp:
            agent = await UsabAgent(esp, fm).start()
            try:
                f = await until(live(esp), 4.0, what="live frame")
                assert f["clock_state"] in ("running", "expired")

                async def expired():
                    f = await esp.frame()
                    return f if f["clock_state"] == "expired" else None

                f = await until(expired, 5.0, what="expiry")
                assert f["clock"] == "0:00" and not f["stale"] and f["colon_on"]
                assert (f["left_score"], f["right_score"], f["period"]) == (3, 1, 2)
                # the controller's own stop push at 0 changes nothing visible
                fm.controller_expire()
                await asyncio.sleep(0.4)
                f = await esp.frame()
                assert f["clock_state"] == "expired" and f["clock"] == "0:00"
            finally:
                await agent.stop()

    run(go())


def test_matside_outage_goes_stale_and_recovery_unstales(core_cli, fm):
    fm.set_timer(running=1, current_time=60.0)

    async def go():
        async with esp_for(core_cli, stale_ms=600) as esp:
            agent = await UsabAgent(esp, fm).start()
            try:
                async def running():
                    f = await live(esp)()
                    return f if f and f["clock_state"] == "running" else None

                await until(running, 4.0, what="running frame")
                await asyncio.sleep(0.5)  # longer than the stale timeout: heartbeats keep it live
                assert (await esp.frame())["stale"] is False

                fm.down()

                async def stale():
                    f = await esp.frame()
                    return f if f["stale"] else None

                fs = await until(stale, 3.0, what="sign to go stale")
                assert fs["colon_on"] is False and fs["left_score"] == 3  # other fields keep their value
                await asyncio.sleep(1.3)
                fs2 = await esp.frame()
                assert fs2["stale"] and fs2["clock"] == fs["clock"]  # frozen while stale

                fm.up()
                f2 = await until(live(esp), 3.0, what="recovered frame")
                assert f2["colon_on"] is True and f2["clock_state"] == "running"
                assert clock_s(f2) < clock_s(fs)  # snapped to the controller's clock, which kept running
                assert esp.error is None
            finally:
                await agent.stop()

    run(go())


def test_extension_fields_reach_the_sign_without_flooding(core_cli, fm):
    fm.set_timer(running=1, current_time=60.0, w1_position="top", riding_time=12.0, shot_clock=20.0, wrestler_indicator="2")

    async def go():
        async with esp_for(core_cli) as esp:
            agent = await UsabAgent(esp, fm, heartbeat=0.5).start()
            try:
                await until(live(esp), 4.0, what="live frame")
                await asyncio.sleep(0.3)
                n0 = agent.pub.sent_count
                await asyncio.sleep(1.6)
                sent = agent.pub.sent_count - n0
                # riding/shot clock move every poll while running; they must not trigger a send each time
                assert 1 <= sent <= 5, sent
                msg = esp.accepted[-1][1]
                assert msg["clock_label"] == "Period 2" and msg["blink"] == "none"
                assert msg["riding_dir"] == 1 and 12_000 <= msg["riding_ms"] <= 16_000
                assert msg["shot_clock_side"] == "right" and 14_000 <= msg["shot_clock_ms"] <= 20_000
                bad = [a for a in agent.acks if a.get("type") != "ack" or a.get("result") != "applied"]
                assert not bad, bad
            finally:
                await agent.stop()

    run(go())
