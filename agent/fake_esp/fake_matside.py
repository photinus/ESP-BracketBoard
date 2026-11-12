"""FakeMatside: a stand-in for the USABracketing matside server (and a minimal app server).

Serves, from a thread on ``127.0.0.1`` (port 0 = any free port by default):

* ``GET /usab?function=alive|getTimer|getEvent`` and ``updateTimer|updateEvent`` in the REAL quirky
  format described in docs/usab-controller-protocol.md section 4: single-quoted pseudo-JSON, scores
  and colors as strings, unescaped ``message``, ``system_millis`` stamped when the response is built,
  ``updated_millis`` taken from the pushed query (the controller's clock) or set by the test.
* ``POST /api/live_scoring/bout`` (form ``event_id``, ``device_key``, ``bout_id``) answering with the
  app server's JSON, so a single fake covers both servers. The device key is checked, never echoed
  and never printed.

Like the real controller it sends **nothing** while a clock runs: ``set_timer`` stores the sample
(stamped ``updated_millis = controller now``) and ``getTimer`` returns that same sample forever, with a
fresh ``system_millis``. The ``controller_*`` helpers mimic the controller's events (start, stop,
score, expiry) by computing the live values first, like the controller does.

Test hooks: :meth:`set_timer`, :meth:`push_update`, :meth:`set_event`, :meth:`set_bout`,
:meth:`down` / :meth:`up` (outage: the listening socket is closed and later re-bound to the same
port), ``fault`` / ``app_fault`` (``hang``, ``garbage``, ``huge``, ``http500``, ``empty``,
``truncated``), :meth:`app_fail_next`, ``request_log`` and ``bout_requests``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import fields, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from bracketboard_agent.usab.model import ControllerTimer
from bracketboard_agent.usab.parse import UsabParseError, parse_update_timer_query

log = logging.getLogger("fake_esp.matside")

_TIMER_FIELDS = {f.name for f in fields(ControllerTimer)}
FAULTS = ("hang", "garbage", "huge", "http500", "empty", "truncated")


def render_timer(t: ControllerTimer, system_millis: int) -> str:
    """The matside server's ``getTimer`` text: single quotes, quoted strings, UNESCAPED message."""
    return (
        "{"
        f"'name':'{t.name}', 'current_time':{float(t.current_time)!r}, 'running':{int(t.running)}, "
        f"'updated_millis':{int(t.updated_millis)}, "
        f"'bout_id':'{t.bout_id}','event_id':'{t.event_id}','w1_color':'{t.w1_color}','w2_color':'{t.w2_color}',"
        f"'w1_score':'{t.w1_score}','w2_score':'{t.w2_score}', "
        f"'tie_winner':'{t.tie_winner}', 't1_score':'{t.t1_score}','t2_score':'{t.t2_score}', "
        f"'riding_time':{float(t.riding_time)!r}, 'w1_position':'{t.w1_position}', "
        f"'shot_clock':{float(t.shot_clock)!r}, 'wrestler_indicator':'{t.wrestler_indicator}', "
        f"'message':'{t.message}', 'system_millis':{int(system_millis)}"
        "}"
    )


class FakeMatside:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        controller_offset_ms: int = 0,
        server_offset_ms: int = 0,
    ):
        self.host = host
        self._requested_port = port
        self.port = port
        self.controller_offset_ms = controller_offset_ms  # controller wall clock minus real time
        self.server_offset_ms = server_offset_ms  # matside server wall clock minus real time
        self._lock = threading.Lock()
        self._timer = ControllerTimer()
        self._event: dict | None = None  # {event_id, device_key, server}
        self._bouts: dict[str, dict] = {}
        self._default_bout: dict | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self.fault: str | None = None
        self.app_fault: str | None = None
        self.fault_hang_s = 5.0
        self._app_fail = 0
        self.request_log: list[str] = []  # "getTimer", "bout", ...
        self.bout_requests: list[dict] = []  # {event_id, bout_id, device_key_ok}
        self.event_refresh_requests = 0  # updateTimer answered "0" (matside wants the controller's event)

    # -- lifecycle -------------------------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "FakeMatside":
        self._stop_evt.clear()
        self._bind()
        return self

    def stop(self) -> None:
        self._stop_evt.set()
        self._unbind()

    def __enter__(self) -> "FakeMatside":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _bind(self) -> None:
        outer = self

        class Handler(_Handler):
            fm = outer

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False
            allow_reuse_address = True

        self._httpd = Server((self.host, self.port if self.port else self._requested_port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()

    def _unbind(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(2.0)

    def down(self) -> None:
        """Simulate an outage: stop listening (connections are refused) until :meth:`up`."""
        self._unbind()

    def up(self) -> None:
        """End the outage: listen again on the same port."""
        if self._httpd is None:
            self._bind()

    @property
    def is_up(self) -> bool:
        return self._httpd is not None

    # -- clocks ----------------------------------------------------------------------------------
    def controller_now_ms(self) -> int:
        return int(time.time() * 1000) + self.controller_offset_ms

    def server_now_ms(self) -> int:
        return int(time.time() * 1000) + self.server_offset_ms

    # -- controller side (test API) --------------------------------------------------------------
    def set_timer(self, **fields_: object) -> None:
        """Store a controller sample. ``updated_millis`` defaults to the controller's clock now.

        Field names are those of :class:`ControllerTimer` (``running`` may be a bool, scores may be
        ints; they are stored as the quirky string types). ``system_millis`` is stamped per response.
        """
        unknown = set(fields_) - _TIMER_FIELDS
        if unknown:
            raise TypeError(f"unknown timer fields: {sorted(unknown)}")
        with self._lock:
            t = replace(self._timer)
            for k, v in fields_.items():
                if k == "running":
                    v = 1 if v else 0
                elif k in ("current_time", "riding_time", "shot_clock"):
                    v = float(v)
                elif k in ("updated_millis", "system_millis"):
                    v = int(v)
                else:
                    v = str(v)
                    if k in ("w1_color", "w2_color"):
                        v = v.lstrip("#")
                setattr(t, k, v)
            if "updated_millis" not in fields_:
                t.updated_millis = self.controller_now_ms()
            self._timer = t

    def push_update(self, query: str) -> str:
        """Process a controller ``updateTimer`` query string exactly as the HTTP endpoint does."""
        t = parse_update_timer_query(query)
        with self._lock:
            if not t.updated_millis:
                t.updated_millis = self.controller_now_ms()
            self._timer = t
            return "1" if self._event is not None else "0"

    def timer(self) -> ControllerTimer:
        """The stored sample (a copy)."""
        with self._lock:
            return replace(self._timer)

    def live(self) -> ControllerTimer:
        """The controller's timer extrapolated to now (what the controller itself would hold)."""
        with self._lock:
            t = replace(self._timer)
        now = self.controller_now_ms()
        if t.running == 1 and t.updated_millis > 0:
            dt = max(0.0, (now - t.updated_millis) / 1000.0)
            dt = min(dt, max(0.0, t.current_time))
            t.current_time = t.current_time - dt
            pos = t.w1_position
            t.riding_time += dt if pos == "top" else -dt if pos == "bottom" else 0.0
            if t.shot_clock > 0:
                t.shot_clock = max(0.0, t.shot_clock - dt)
        t.updated_millis = now
        return t

    def _push(self, t: ControllerTimer) -> None:
        with self._lock:
            self._timer = t

    def controller_start(self) -> None:
        t = self.live()
        t.running = 1
        self._push(t)

    def controller_stop(self) -> None:
        t = self.live()
        t.running = 0
        self._push(t)

    def controller_expire(self) -> None:
        """What the controller pushes when a clock reaches 0: running 0, time 0."""
        t = self.live()
        t.running, t.current_time = 0, 0.0
        self._push(t)

    def controller_score(self, w1: int | None = None, w2: int | None = None, message: str | None = None) -> None:
        t = self.live()
        if w1 is not None:
            t.w1_score = str(w1)
        if w2 is not None:
            t.w2_score = str(w2)
        if message is not None:
            t.message = message
        self._push(t)

    # -- event / bout data -----------------------------------------------------------------------
    def set_event(self, event_id: str = "77", device_key: str = "DEVICE-KEY-SECRET", server: str | None = None) -> None:
        """What ``getEvent`` returns. ``server`` defaults to this fake (it serves the app-server API too)."""
        with self._lock:
            self._event = {"event_id": str(event_id), "device_key": str(device_key), "server": server or self.url}

    def clear_event(self) -> None:
        with self._lock:
            self._event = None

    def set_bout(self, bout_id: str | None = None, weight_name: str = "157", bout_number: str = "1203",
                 match_id: str = "9001") -> None:
        """App-server bout data for ``bout_id`` (``None``: the default for any bout id)."""
        data = {"weight_name": weight_name, "bout_number": bout_number, "match_id": match_id}
        with self._lock:
            if bout_id is None:
                self._default_bout = data
            else:
                self._bouts[str(bout_id)] = data

    def app_fail_next(self, n: int) -> None:
        """Answer the next ``n`` bout requests with HTTP 500."""
        with self._lock:
            self._app_fail = n

    # -- request handling (called from handler threads) ------------------------------------------
    def _fault_response(self, fault: str | None):
        """``(status, body)`` for a fault, or ``None`` for normal operation. ``hang`` blocks first."""
        if fault is None:
            return None
        if fault == "hang":
            self._stop_evt.wait(self.fault_hang_s)
            return 200, b"{}"
        if fault == "garbage":
            return 200, b"<html><body>this is not a timer</body></html>"
        if fault == "huge":
            return 200, b"{'name':'" + b"A" * 300_000 + b"'}"
        if fault == "http500":
            return 500, b"internal error"
        if fault == "empty":
            return 200, b""
        if fault == "truncated":
            full = render_timer(self.timer(), self.server_now_ms())
            return 200, full[: len(full) // 2].encode()
        raise ValueError(fault)

    def handle_usab(self, function: str, raw_query: str) -> tuple[int, bytes]:
        self.request_log.append(function)
        fr = self._fault_response(self.fault)
        if fr is not None:
            return fr
        if function == "alive":
            return 200, b"alive"
        if function == "getTimer":
            with self._lock:
                t = replace(self._timer)
            return 200, render_timer(t, self.server_now_ms()).encode("utf-8", "replace")
        if function == "getEvent":
            with self._lock:
                ev = dict(self._event) if self._event else {"event_id": "", "device_key": "", "server": ""}
            return 200, (
                f"{{'event_id':'{ev['event_id']}','device_key':'{ev['device_key']}','server':'{ev['server']}'}}"
            ).encode()
        if function == "updateTimer":
            try:
                reply = self.push_update(raw_query)
            except UsabParseError:
                return 400, b"bad request"
            if reply == "0":
                self.event_refresh_requests += 1
            return 200, reply.encode()
        if function == "updateEvent":
            q = {k: v[0] for k, v in parse_qs(raw_query, keep_blank_values=True).items()}
            self.set_event(q.get("event_id", ""), q.get("device_key", ""), q.get("server", "").replace("[f]", "/") or None)
            return 200, b"1"
        return 404, b"unknown function"

    def handle_bout(self, form: dict[str, str]) -> tuple[int, bytes]:
        self.request_log.append("bout")
        with self._lock:
            ev = dict(self._event) if self._event else None
            data = self._bouts.get(form.get("bout_id", ""), self._default_bout)
            fail = self._app_fail > 0
            if fail:
                self._app_fail -= 1
        key_ok = ev is not None and form.get("device_key") == ev["device_key"] and form.get("event_id") == ev["event_id"]
        self.bout_requests.append({"event_id": form.get("event_id"), "bout_id": form.get("bout_id"), "device_key_ok": key_ok})
        if fail:
            return 500, b"temporarily broken"
        fr = self._fault_response(self.app_fault)
        if fr is not None:
            return fr
        if not key_ok:
            return 200, b'{"status":"error","message":"invalid event or device key"}'
        if data is None:
            return 200, b'{"status":"error","message":"bout not found"}'
        body = {
            "status": "success",
            "match_format": {"discipline": {"riding_time": 1}},
            "bout": {"id": form.get("bout_id"), **data},
        }
        return 200, json.dumps(body).encode()


class _Handler(BaseHTTPRequestHandler):
    fm: FakeMatside  # set by FakeMatside._bind
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):  # noqa: A002 - silence; request lines can carry secrets
        return

    def _send(self, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError):
            pass

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path != "/usab":
            return self._send(404, b"not found")
        q = parse_qs(parts.query, keep_blank_values=True)
        function = (q.get("function") or [""])[0]
        status, body = self.fm.handle_usab(function, self.path)
        self._send(status, body)

    def do_POST(self):  # noqa: N802
        parts = urlsplit(self.path)
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(min(max(n, 0), 1 << 20)) if n else b""
        if parts.path == "/api/live_scoring/bout":
            form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
            status, body = self.fm.handle_bout(form)
            return self._send(status, body, "application/json")
        if parts.path == "/usab":
            q = parse_qs(parts.query, keep_blank_values=True)
            status, body = self.fm.handle_usab((q.get("function") or [""])[0], self.path)
            return self._send(status, body)
        self._send(404, b"not found")


# --- scripted demo (python -m fake_esp.matside) ----------------------------------------------------
def run_demo(fm: FakeMatside, stop: threading.Event, speed: float = 1.0, period_s: float = 30.0, loop: bool = True) -> None:
    """Drive ``fm`` like a controller through a short bout: scores, a blood timer, a break, riding
    time, a shot clock and expiry. Alternates which wrestler wears red so both side mappings show."""

    def wait(s: float) -> bool:
        return stop.wait(s / speed)

    fm.set_event("77", "demo-device-key", None)
    fm.set_bout(None, "157", "1203", "9001")
    n = 0
    while not stop.is_set():
        w1_red = n % 2 == 0
        c1, c2 = ("FF0000", "0000FF") if w1_red else ("00AA00", "FF0000")
        base = dict(bout_id="1234", event_id="77", w1_color=c1, w2_color=c2, w1_score="0", w2_score="0",
                    running=0, riding_time=0.0, w1_position="neutral", shot_clock=0.0, wrestler_indicator="", message="")
        fm.set_timer(name="Period 1", current_time=period_s, **base)
        if wait(3):
            return
        fm.controller_start()
        if wait(period_s * 0.2):
            return
        fm.controller_score(w1=2, message="1.T1 Smith")
        if wait(period_s * 0.2):
            return
        fm.controller_score(w2=1, message="2.E2 Jones")
        if wait(period_s * 0.1):
            return
        # blood time: its own countdown; the period clock is not reported while it is current
        live = fm.live()
        fm.controller_stop()
        fm.set_timer(name="Blood", current_time=10.0, running=1)
        if wait(10.5):
            return
        fm.controller_expire()
        fm.set_timer(name="Period 1", current_time=live.current_time, running=1)
        if wait(live.current_time + 0.5):
            return
        fm.controller_expire()
        # break, then period 2 with riding time
        fm.set_timer(name="Break", current_time=8.0, running=1)
        if wait(8.5):
            return
        fm.set_timer(name="Period 2", current_time=period_s, running=0, w1_position="top", riding_time=0.0)
        if wait(2):
            return
        fm.controller_start()
        if wait(period_s * 0.4):
            return
        fm.controller_stop()
        fm.set_timer(w1_position="bottom")
        fm.controller_start()
        if wait(period_s * 0.3):
            return
        fm.controller_score(w1=5, w2=4, message="3.N3 Smith")
        # freestyle-style shot clock for a few seconds
        fm.set_timer(shot_clock=30.0, wrestler_indicator="2")
        if wait(period_s * 0.3 + 1):
            return
        fm.controller_expire()
        if wait(6):
            return
        n += 1
        if not loop:
            return
