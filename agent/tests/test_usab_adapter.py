"""USABracketingAdapter against FakeMatside: polling, extrapolation, outages, background bout lookup."""
import asyncio
import logging
import time

import pytest

from bracketboard_agent.adapters.usabracketing import UsabAdapter, USABracketingAdapter
from bracketboard_agent.schema import to_message, validate_state_message
from bracketboard_agent.usab.client import AppServerClient, UsabClientError
from fake_esp.fake_matside import FakeMatside

KEY = "ADAPTER-SECRET-KEY-42"


def run(coro, timeout=15.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def until(pred, timeout=3.0, interval=0.01):
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if asyncio.iscoroutine(v):
            v = await v
        if v:
            return v
        if time.monotonic() > end:
            pytest.fail("timed out waiting for condition")
        await asyncio.sleep(interval)


@pytest.fixture
def fm():
    with FakeMatside() as f:
        f.set_event("77", KEY)
        f.set_bout(None, "157", "1203", "9001")
        f.set_timer(name="Period 1", current_time=120, running=0, bout_id="1234", event_id="77",
                    w1_color="FF0000", w2_color="0000FF", w1_score=3, w2_score=1)
        yield f


def make(fm, **kw):
    kw.setdefault("timeout", 0.4)
    kw.setdefault("lookup_backoff", (0.05, 0.05, 0.1))
    kw.setdefault("app_client", AppServerClient(timeout=0.4))
    return USABracketingAdapter("127.0.0.1", fm.port, **kw)


def test_alias():
    assert UsabAdapter is USABracketingAdapter


def test_state_is_mapped_and_schema_valid(fm):
    async def go():
        a = make(fm)
        await a.start()
        try:
            st = await a.read_state()
            validate_state_message(to_message(st, "s", 1, 0))
            assert (st.left_score, st.right_score, st.period) == (3, 1, 1)
            assert (st.clock_ms_remaining, st.clock_state, st.screen) == (120_000, "stopped", "score")
            await until(lambda: _weight_is(a, "157"))
            st = await a.read_state()
            assert (st.weight, st.bout) == ("157", "1203")
        finally:
            await a.stop()

    run(go())


async def _weight_is(a, w):
    return (await a.read_state()).weight == w


def test_running_clock_counts_down_between_controller_updates(fm):
    async def go():
        a = make(fm, bout_lookup=False)
        fm.set_timer(running=1, current_time=100.0)
        s1 = await a.read_state()
        n = len(fm.request_log)
        await asyncio.sleep(0.3)  # real time passes, the controller sends nothing
        s2 = await a.read_state()
        assert s1.clock_state == s2.clock_state == "running"
        # every read re-fetches getTimer: the same sample, with a newer system_millis
        assert 250 <= s1.clock_ms_remaining - s2.clock_ms_remaining <= 450
        assert len(fm.request_log) > n

    run(go())


def test_expiry_without_a_controller_push(fm):
    async def go():
        a = make(fm, bout_lookup=False)
        fm.set_timer(running=1, current_time=0.4)
        assert (await a.read_state()).clock_state == "running"
        await asyncio.sleep(0.5)
        st = await a.read_state()
        assert (st.clock_state, st.clock_ms_remaining) == ("expired", 0)

    run(go())


def test_sample_age_at_response_is_accounted(fm):
    async def go():
        a = make(fm, bout_lookup=False)
        fm.set_timer(running=1, current_time=30.0, updated_millis=fm.controller_now_ms() - 6000)
        st = await a.read_state()
        assert 23_800 <= st.clock_ms_remaining <= 24_100  # the sample was already 6 s old

    run(go())


def test_controller_stop_freezes_the_clock(fm):
    async def go():
        a = make(fm, bout_lookup=False)
        fm.set_timer(running=1, current_time=60.0)
        await a.read_state()
        await asyncio.sleep(0.2)
        fm.controller_stop()  # controller pushes the live value with running=0
        st1 = await a.read_state()
        await asyncio.sleep(0.2)
        st2 = await a.read_state()
        assert st1.clock_state == "stopped" and 59_000 < st1.clock_ms_remaining < 60_000
        assert st2.clock_ms_remaining == st1.clock_ms_remaining

    run(go())


def test_options_swap_and_offset(fm):
    async def go():
        a = make(fm, swap_sides=True, bout_lookup=False)
        st = await a.read_state()
        assert (st.left_score, st.right_score) == (1, 3)
        fm.set_timer(running=1, current_time=100.0, updated_millis=fm.controller_now_ms() - 400)
        a0 = make(fm, bout_lookup=False, offset_ms=0)
        a1 = make(fm, bout_lookup=False, offset_ms=300)
        s0, s1 = await a0.read_state(), await a1.read_state()
        assert 99_450 < s0.clock_ms_remaining < 99_650 and 99_750 < s1.clock_ms_remaining < 99_950

    run(go())


# --- outage ----------------------------------------------------------------------------------------
def test_outage_returns_none_logs_once_and_recovers(fm, caplog):
    caplog.set_level(logging.INFO, logger="bracketboard.usab")

    async def go():
        a = make(fm, bout_lookup=False)
        await a.start()
        assert "reachable" in caplog.text
        assert await a.read_state() is not None
        fm.down()
        for _ in range(8):
            assert await a.read_state() is None
        fm.up()
        assert await a.read_state() is not None
        assert await a.read_state() is not None
        fm.down()
        assert await a.read_state() is None
        fm.up()
        assert await a.read_state() is not None
        await a.stop()

    run(go())
    lost = [r for r in caplog.records if "lost the matside server" in r.getMessage()]
    back = [r for r in caplog.records if "answering again" in r.getMessage()]
    assert len(lost) == 2 and len(back) == 2  # one line per outage, not one per poll


def test_start_when_server_is_down_warns_and_later_works(fm, caplog):
    caplog.set_level(logging.INFO, logger="bracketboard.usab")
    fm.down()

    async def go():
        a = make(fm, bout_lookup=False)
        await a.start()
        assert "not reachable" in caplog.text
        assert await a.read_state() is None
        fm.up()
        assert await a.read_state() is not None

    run(go())
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


@pytest.mark.parametrize("fault", ["garbage", "truncated", "empty", "http500", "huge"])
def test_garbled_replies_return_none(fm, fault):
    async def go():
        a = make(fm, bout_lookup=False)
        fm.fault = fault
        for _ in range(3):
            assert await a.read_state() is None
        fm.fault = None
        assert await a.read_state() is not None

    run(go())


def test_garbled_logged_once(fm, caplog):
    caplog.set_level(logging.INFO, logger="bracketboard.usab")

    async def go():
        a = make(fm, bout_lookup=False)
        fm.fault = "garbage"
        for _ in range(6):
            assert await a.read_state() is None

    run(go())
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_hanging_server_does_not_block_longer_than_timeout(fm):
    async def go():
        a = make(fm, bout_lookup=False, timeout=0.3)
        fm.fault, fm.fault_hang_s = "hang", 3.0
        t0 = time.monotonic()
        assert await a.read_state() is None
        assert time.monotonic() - t0 < 1.2

    run(go())


def test_unexpected_client_exception_still_returns_none():
    class Boom:
        base_url = "x"

        async def get_timer(self):
            raise RuntimeError("boom")

        async def alive(self):
            raise RuntimeError("boom")

    async def go():
        a = USABracketingAdapter(client=Boom())
        await a.start()
        assert await a.read_state() is None
        await a.stop()

    run(go())


# --- splash / no bout ------------------------------------------------------------------------------
def test_splash_when_no_bout_and_no_lookup(fm):
    fm.set_timer(bout_id="", event_id="")

    async def go():
        a = make(fm)
        st = await a.read_state()
        assert st.screen == "splash" and (st.weight, st.bout) == ("", "")
        await asyncio.sleep(0.1)
        assert "bout" not in fm.request_log and "getEvent" not in fm.request_log

    run(go())


# --- bout lookup -----------------------------------------------------------------------------------
def test_lookup_never_blocks_read_state(fm):
    fm.app_fault, fm.fault_hang_s = "hang", 0.7

    async def go():
        a = make(fm, app_client=AppServerClient(timeout=0.3))
        t0 = time.monotonic()
        for _ in range(5):
            st = await a.read_state()
            assert st is not None and (st.weight, st.bout) == ("", "")
        assert time.monotonic() - t0 < 0.5
        await a.stop()

    run(go())


def test_lookup_retries_with_backoff_until_success(fm):
    fm.app_fail_next(2)

    async def go():
        a = make(fm)
        await a.read_state()
        await until(lambda: _weight_is(a, "157"), 4.0)
        assert a.lookup_failures == 2
        assert len(fm.bout_requests) == 3
        await a.stop()

    run(go())


def test_lookup_is_cached_per_bout(fm):
    async def go():
        a = make(fm)
        await a.read_state()
        await until(lambda: _weight_is(a, "157"))
        n = len(fm.bout_requests)
        for _ in range(10):
            await a.read_state()
        await asyncio.sleep(0.1)
        assert len(fm.bout_requests) == n == 1
        # new bout -> new lookup, old info not shown meanwhile
        fm.set_bout("2000", "125", "2001", "5")
        fm.app_fail_next(1)
        fm.set_timer(bout_id="2000")
        st = await a.read_state()
        assert (st.weight, st.bout) == ("", "")
        await until(lambda: _weight_is(a, "125"))
        assert (await a.read_state()).bout == "2001"
        # back to the first bout: served from the cache, no request
        n = len(fm.bout_requests)
        fm.set_timer(bout_id="1234")
        assert (await a.read_state()).weight == "157"
        await asyncio.sleep(0.1)
        assert len(fm.bout_requests) == n
        await a.stop()

    run(go())


def test_lookup_follows_bout_changes_and_cancels_stale_lookups(fm):
    fm.app_fault, fm.fault_hang_s = "hang", 0.7

    async def go():
        a = make(fm, app_client=AppServerClient(timeout=1.5))
        await a.read_state()
        await asyncio.sleep(0.1)
        first = a._lookup_task
        fm.set_timer(bout_id="2")
        await a.read_state()
        await asyncio.sleep(0.05)
        assert first.cancelled() or first.done()
        assert a._lookup_task is not first and a._lookup_key == ("77", "2")
        fm.set_timer(bout_id="")
        await a.read_state()
        await asyncio.sleep(0.05)
        assert a._lookup_task is None
        await a.stop()

    run(go())


def test_lookup_when_event_not_selected_yet(fm):
    fm.clear_event()

    async def go():
        a = make(fm)
        st = await a.read_state()
        assert st is not None and st.weight == ""
        await asyncio.sleep(0.2)
        assert (await a.read_state()).weight == ""
        fm.set_event("77", KEY)
        await until(lambda: _weight_is(a, "157"), 4.0)
        await a.stop()

    run(go())


def test_event_is_refetched_when_the_device_key_rotates(fm):
    async def go():
        a = make(fm)
        await a.read_state()
        await until(lambda: _weight_is(a, "157"))
        fm.set_event("77", "NEW-KEY")
        fm.set_bout("3", "133", "9", "9")
        fm.set_timer(bout_id="3")
        await a.read_state()
        await until(lambda: _weight_is(a, "133"), 4.0)
        # the first attempt used the cached (now rotated) key and failed; the retry re-asked getEvent
        assert [r["device_key_ok"] for r in fm.bout_requests[-2:]] == [False, True]
        await a.stop()

    run(go())


def test_no_bout_lookup_flag(fm):
    async def go():
        a = make(fm, bout_lookup=False)
        for _ in range(3):
            st = await a.read_state()
            assert (st.weight, st.bout) == ("", "")
            await asyncio.sleep(0.05)
        assert "bout" not in fm.request_log and "getEvent" not in fm.request_log

    run(go())


def test_wait_change_sleeps_min_of_timeout_and_poll_interval(fm):
    async def go():
        a = make(fm, poll_interval=0.05)
        t0 = time.monotonic()
        await a.wait_change(5.0)
        assert 0.04 <= time.monotonic() - t0 < 0.5
        t0 = time.monotonic()
        await a.wait_change(0.01)
        assert time.monotonic() - t0 < 0.04

    run(go())


def test_stop_is_safe_without_start_and_cancels_lookup(fm):
    fm.app_fault, fm.fault_hang_s = "hang", 0.7

    async def go():
        a = make(fm, app_client=AppServerClient(timeout=1.5))
        await a.stop()
        await a.read_state()
        await asyncio.sleep(0.05)
        task = a._lookup_task
        assert task is not None and not task.done()
        await a.stop()
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()

    run(go())


def test_device_key_never_in_logs(fm, caplog):
    caplog.set_level(logging.DEBUG)
    fm.app_fail_next(1)

    async def go():
        a = make(fm)
        await a.start()
        await a.read_state()
        await until(lambda: _weight_is(a, "157"), 4.0)
        fm.set_event("77", KEY, server="http://user:pw@127.0.0.1:1/x")  # unreachable app server
        fm.set_timer(bout_id="9")
        await a.read_state()
        await asyncio.sleep(0.3)
        await a.stop()

    run(go())
    assert KEY not in caplog.text and "pw@" not in caplog.text
