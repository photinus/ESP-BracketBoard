"""MatsideClient / AppServerClient against FakeMatside and a few raw misbehaving sockets."""
import asyncio
import contextlib
import logging
import socket
import threading
import time

import pytest

from bracketboard_agent.usab.client import AppServerClient, MatsideClient, UsabClientError
from bracketboard_agent.usab.model import EventInfo
from fake_esp.fake_matside import FakeMatside

KEY = "SENTINEL-DEVICE-KEY-7f3a9c"


def run(coro, timeout=10.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


@pytest.fixture
def fm():
    with FakeMatside() as f:
        f.set_event("77", KEY)
        f.set_bout(None, "157", "1203", "9001")
        f.set_timer(name="Period 1", current_time=120, running=0, bout_id="1234", event_id="77",
                    w1_color="FF0000", w2_color="0000FF", w1_score=3, w2_score=1, message="4.T2 O'Brien")
        yield f


@contextlib.contextmanager
def raw_server(handler):
    """A one-connection-at-a-time raw TCP server; ``handler(conn)`` speaks (mis)HTTP."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(0.1)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.settimeout(2.0)
                conn.recv(65536)
                handler(conn)
            except OSError:
                pass
            finally:
                conn.close()

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        th.join(2.0)
        srv.close()


def test_alive_and_outage(fm):
    c = MatsideClient("127.0.0.1", fm.port, timeout=0.5)
    assert run(c.alive()) is True
    fm.down()
    assert run(c.alive()) is False
    fm.up()
    assert run(c.alive()) is True


def test_get_timer_and_rx_mono(fm):
    now = [100.0]
    c = MatsideClient("127.0.0.1", fm.port, clock=lambda: now[0])
    t, rx = run(c.get_timer())
    assert rx == 100.0
    assert t.name == "Period 1" and t.current_time == 120.0 and t.w1_score == "3" and t.message == "4.T2 O'Brien"
    assert t.system_millis >= t.updated_millis > 0
    assert "getTimer" in fm.request_log


def test_get_event(fm):
    c = MatsideClient("127.0.0.1", fm.port)
    ev = run(c.get_event())
    assert ev.event_id == "77" and ev.device_key == KEY and ev.server == fm.url
    assert KEY not in repr(ev) and KEY not in str(ev)


def test_unreachable_kind(fm):
    fm.down()
    c = MatsideClient("127.0.0.1", fm.port, timeout=0.5)
    with pytest.raises(UsabClientError) as ei:
        run(c.get_timer())
    assert ei.value.kind == "unreachable"


def test_timeout_is_enforced(fm):
    fm.fault, fm.fault_hang_s = "hang", 3.0
    c = MatsideClient("127.0.0.1", fm.port, timeout=0.3)
    t0 = time.monotonic()
    with pytest.raises(UsabClientError) as ei:
        run(c.get_timer())
    assert ei.value.kind == "timeout" and time.monotonic() - t0 < 1.5
    t0 = time.monotonic()
    assert run(c.alive()) is False and time.monotonic() - t0 < 1.5


def test_oversized_response_rejected(fm):
    fm.fault = "huge"  # 300 KB with a Content-Length header
    c = MatsideClient("127.0.0.1", fm.port)
    with pytest.raises(UsabClientError) as ei:
        run(c.get_timer())
    assert ei.value.kind == "too_large"


def test_oversized_response_without_content_length_rejected():
    def handler(conn):
        conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
        for _ in range(200):
            conn.sendall(b"x" * 8192)

    with raw_server(handler) as port:
        c = MatsideClient("127.0.0.1", port, timeout=2.0, max_bytes=20_000)
        with pytest.raises(UsabClientError) as ei:
            run(c.get_timer())
        assert ei.value.kind == "too_large"


def test_slow_drip_response_hits_total_deadline():
    def handler(conn):
        conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
        for _ in range(100):
            conn.sendall(b"{")
            time.sleep(0.1)

    with raw_server(handler) as port:
        c = MatsideClient("127.0.0.1", port, timeout=0.5)
        t0 = time.monotonic()
        with pytest.raises(UsabClientError) as ei:
            run(c.get_timer())
        assert ei.value.kind in ("timeout", "parse") and time.monotonic() - t0 < 2.0


@pytest.mark.parametrize("fault,kind", [("garbage", "parse"), ("empty", "parse"), ("truncated", "parse"), ("http500", "http")])
def test_bad_replies(fm, fault, kind):
    fm.fault = fault
    c = MatsideClient("127.0.0.1", fm.port)
    with pytest.raises(UsabClientError) as ei:
        run(c.get_timer())
    assert ei.value.kind == kind
    assert "this is not a timer" not in str(ei.value)
    assert run(c.alive()) is (fault != "http500")


def test_redirects_are_not_followed():
    def handler(conn):
        conn.sendall(b"HTTP/1.0 302 Found\r\nLocation: http://127.0.0.1:1/evil\r\nContent-Length: 0\r\n\r\n")

    with raw_server(handler) as port:
        c = MatsideClient("127.0.0.1", port)
        with pytest.raises(UsabClientError) as ei:
            run(c.get_timer())
        assert ei.value.kind == "http"


def test_connection_closed_midway():
    def handler(conn):
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 500\r\n\r\n{'name'")

    with raw_server(handler) as port:
        c = MatsideClient("127.0.0.1", port)
        with pytest.raises(UsabClientError):
            run(c.get_timer())


def test_ipv6_literal_host_is_bracketed():
    assert MatsideClient("::1", 6099).base_url == "http://[::1]:6099/usab"
    assert MatsideClient("127.0.0.1").base_url == "http://127.0.0.1:6099/usab"


# --- app server ------------------------------------------------------------------------------------
def test_fetch_bout(fm):
    ev = run(MatsideClient("127.0.0.1", fm.port).get_event())
    fm.set_bout("1234", "165", "42", "777")
    info = run(AppServerClient().fetch_bout(ev, "1234"))
    assert (info.weight_name, info.bout_number, info.match_id) == ("165", "42", "777")
    assert fm.bout_requests == [{"event_id": "77", "bout_id": "1234", "device_key_ok": True}]


def test_fetch_bout_empty_bout_number_uses_match_id(fm):
    ev = run(MatsideClient("127.0.0.1", fm.port).get_event())
    fm.set_bout("5", "125", "", "31337")
    info = run(AppServerClient().fetch_bout(ev, "5"))
    assert info.bout_number == "" and info.display_bout == "m31337"


def test_fetch_bout_wrong_key_or_unknown_bout_is_an_error(fm):
    c = AppServerClient()
    with pytest.raises(UsabClientError) as ei:
        run(c.fetch_bout(EventInfo("77", "WRONG-KEY", fm.url), "1234"))
    assert ei.value.kind == "http" and "WRONG-KEY" not in str(ei.value)
    fm.set_bout(None, "x", "y", "z")
    fm._default_bout = None
    with pytest.raises(UsabClientError):
        run(c.fetch_bout(EventInfo("77", KEY, fm.url), "nope"))


@pytest.mark.parametrize("server", ["", "file:///etc/passwd", "ftp://example.org", "javascript:alert(1)", "//example.org", "http://"])
def test_fetch_bout_rejects_bad_server_urls(server):
    with pytest.raises(UsabClientError) as ei:
        run(AppServerClient().fetch_bout(EventInfo("77", KEY, server), "1"))
    assert ei.value.kind == "config" and KEY not in str(ei.value)


def test_fetch_bout_incomplete_event_is_a_config_error(fm):
    for ev in (EventInfo("", KEY, fm.url), EventInfo("77", "", fm.url)):
        with pytest.raises(UsabClientError) as ei:
            run(AppServerClient().fetch_bout(ev, "1"))
        assert ei.value.kind == "config"
    with pytest.raises(UsabClientError):
        run(AppServerClient().fetch_bout(EventInfo("77", KEY, fm.url), ""))


def test_fetch_bout_failures(fm):
    ev = EventInfo("77", KEY, fm.url)
    c = AppServerClient(timeout=0.4)
    fm.app_fail_next(1)
    with pytest.raises(UsabClientError) as ei:
        run(c.fetch_bout(ev, "1234"))
    assert ei.value.kind == "http"
    assert run(c.fetch_bout(ev, "1234")).weight_name == "157"  # next one works
    fm.app_fault = "garbage"
    with pytest.raises(UsabClientError) as ei:
        run(c.fetch_bout(ev, "1234"))
    assert ei.value.kind == "parse"
    fm.app_fault = "hang"
    fm.fault_hang_s = 3.0
    with pytest.raises(UsabClientError) as ei:
        run(c.fetch_bout(ev, "1234"))
    assert ei.value.kind == "timeout"
    fm.app_fault = "huge"
    with pytest.raises(UsabClientError) as ei:
        run(AppServerClient(max_bytes=10_000).fetch_bout(ev, "1234"))
    assert ei.value.kind == "too_large"


@pytest.mark.parametrize("body,ok", [
    ('{"status":"success","bout":{"weight_name":"157","bout_number":"12","match_id":"3"}}', True),
    ('{"bout":{"weight_name":157,"bout_number":12,"match_id":3.0}}', True),  # no status, numbers
    ('{"status":"success","bout":{"weight_name":null}}', True),
    ('{"status":"error","message":"no"}', False),
    ('{"status":"success"}', False),
    ('{"status":"success","bout":[]}', False),
    ("[]", False), ("not json", False), ("", False), ('{"status":"success","bout":{"weight_name":NaN}}', False),
])
def test_bout_reply_parsing_is_tolerant_but_strict_on_shape(body, ok):
    if ok:
        info = AppServerClient._parse(body)
        assert isinstance(info.weight_name, str) and isinstance(info.bout_number, str)
    else:
        with pytest.raises(UsabClientError):
            AppServerClient._parse(body)


def test_bout_reply_numbers_become_strings():
    info = AppServerClient._parse('{"bout":{"weight_name":157,"bout_number":12.0,"match_id":3}}')
    assert (info.weight_name, info.bout_number, info.match_id) == ("157", "12", "3")


# --- secrecy ---------------------------------------------------------------------------------------
def test_device_key_never_logged_or_in_errors(fm, caplog):
    caplog.set_level(logging.DEBUG)
    texts = []

    async def go():
        mc = MatsideClient("127.0.0.1", fm.port, timeout=0.4)
        ac = AppServerClient(timeout=0.4)
        ev = await mc.get_event()
        texts.extend([repr(ev), str(ev), repr(mc), repr(ac)])
        await ac.fetch_bout(ev, "1234")
        for setup in (lambda: fm.app_fail_next(1), lambda: setattr(fm, "app_fault", "garbage"),
                      lambda: (setattr(fm, "app_fault", None), fm.clear_event())):
            setup()
            try:
                await ac.fetch_bout(ev, "1234")
            except UsabClientError as e:
                texts.append(str(e))
                texts.append(repr(e))
        fm.set_event("77", KEY)
        fm.fault = "garbage"
        for fn in (mc.get_event, mc.get_timer):
            try:
                await fn()
            except UsabClientError as e:
                texts.append(str(e))
        fm.fault = None
        fm.down()
        try:
            await ac.fetch_bout(EventInfo("77", KEY, fm.url), "1")
        except UsabClientError as e:
            texts.append(str(e))

    run(go())
    assert texts
    assert all(KEY not in t for t in texts), texts
    assert KEY not in caplog.text
