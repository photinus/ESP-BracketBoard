"""Async wrapper around the ``bb_core_cli`` stdio bridge (docs/architecture.md, "Stdio bridge").

One JSON object per line in each direction; every request is answered by zero or more lines followed
by ``{"ch":"done"}``. Requests are serialised with a lock so concurrent callers never interleave.

``PurePythonBridge`` offers the same API without the C++ binary (schema validation + a tiny seq
filter); it exists for manual testing only and is much less faithful than the real core.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

REQUEST_TIMEOUT_S = 10.0


class BridgeError(RuntimeError):
    """The bridge process misbehaved (died, timed out, reported an error line)."""


def find_core_cli() -> str | None:
    """Locate ``bb_core_cli``: ``$BB_CORE_CLI``, then common build directories. None if not found."""
    env = os.environ.get("BB_CORE_CLI")
    if env:
        return env if Path(env).is_file() else None
    repo = Path(__file__).resolve().parents[2]
    candidates = [
        repo / "build" / "bb_core_cli",
        repo / "host" / "build" / "bb_core_cli",
        repo / "build" / "host" / "bb_core_cli",
    ]
    for pattern in ("/tmp/bb-*/bb_core_cli", "/tmp/bb*/bb_core_cli"):
        candidates.extend(sorted(Path("/").glob(pattern.lstrip("/"))))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


class CoreBridge:
    """Drives one ``bb_core_cli`` subprocess."""

    def __init__(
        self,
        core_cli: str | os.PathLike,
        token: str,
        stale_ms: int = 3000,
        *,
        fake_time: bool = False,
        manual: bool = False,
    ):
        self.core_cli = str(core_cli)
        self.token = token
        self.stale_ms = stale_ms
        self.fake_time = fake_time
        self.manual = manual
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._http_id = 0
        self._stderr_task: asyncio.Task | None = None
        self.stderr_lines: list[str] = []

    # -- lifecycle -------------------------------------------------------------------------------
    async def start(self) -> None:
        args = [self.core_cli, "--token", self.token, "--stale-ms", str(self.stale_ms)]
        if self.fake_time:
            args.append("--fake-time")
        if self.manual:
            args.append("--manual")
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1 << 20,
        )
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            self.stderr_lines.append(line.decode("utf-8", "replace").rstrip())
            del self.stderr_lines[:-50]

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        if self._stderr_task:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)

    # -- plumbing --------------------------------------------------------------------------------
    async def _request(self, req: dict) -> list[dict]:
        """Send one request, return its output lines (without the terminating ``done``)."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise BridgeError("bridge not running")
        async with self._lock:
            try:
                proc.stdin.write((json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8"))
                await proc.stdin.drain()
                out: list[dict] = []
                while True:
                    raw = await asyncio.wait_for(proc.stdout.readline(), REQUEST_TIMEOUT_S)
                    if not raw:
                        raise BridgeError(f"bb_core_cli exited (stderr: {self.stderr_lines[-3:]})")
                    line = json.loads(raw)
                    ch = line.get("ch")
                    if ch == "done":
                        return out
                    if ch == "error":
                        # the bridge still sends `done` after an error line; drain it, then raise
                        await self._drain_to_done(proc)
                        raise BridgeError(f"bridge error for {req}: {line.get('msg')}")
                    out.append(line)
            except asyncio.TimeoutError as e:
                raise BridgeError(f"bb_core_cli did not answer {req}") from e
            except (BrokenPipeError, ConnectionResetError) as e:
                raise BridgeError("bb_core_cli pipe closed") from e

    async def _drain_to_done(self, proc) -> None:
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), REQUEST_TIMEOUT_S)
            if not raw or json.loads(raw).get("ch") == "done":
                return

    @staticmethod
    def _outbound(lines: list[dict]) -> list[dict]:
        return [
            {"conn": ln["conn"], "send": ln["send"], "close": bool(ln.get("close"))}
            for ln in lines
            if ln.get("ch") == "ws"
        ]

    # -- API -------------------------------------------------------------------------------------
    async def open_ws(self, conn: int) -> None:
        await self._request({"ch": "ws", "conn": conn, "ev": "open"})

    async def ws_text(self, conn: int, text: str) -> list[dict]:
        """Feed one text frame; returns the Outbound dicts ``{conn, send, close}`` in order."""
        return self._outbound(await self._request({"ch": "ws", "conn": conn, "ev": "text", "data": text}))

    async def close_ws(self, conn: int) -> None:
        await self._request({"ch": "ws", "conn": conn, "ev": "close"})

    async def http_post(self, id: int | None, path: str, auth: str | None, body: str) -> tuple[int, str]:
        """Returns ``(status, body)``. ``id`` None allocates one."""
        if id is None:
            self._http_id += 1
            id = self._http_id
        lines = await self._request({"ch": "http", "id": id, "path": path, "auth": auth, "body": body})
        for ln in lines:
            if ln.get("ch") == "http" and ln.get("id") == id:
                return int(ln["status"]), ln.get("body", "")
        raise BridgeError(f"no http reply for id {id}")

    async def tick(self) -> list[dict]:
        return self._outbound(await self._request({"ch": "tick"}))

    async def frame(self) -> dict:
        for ln in await self._request({"ch": "frame"}):
            if ln.get("ch") == "frame":
                return ln["frame"]
        raise BridgeError("no frame reply")

    async def status(self) -> dict:
        for ln in await self._request({"ch": "status"}):
            if ln.get("ch") == "status":
                return json.loads(ln["body"])
        raise BridgeError("no status reply")

    async def advance_time(self, ms: int) -> None:
        if not self.fake_time:
            raise BridgeError("advance_time needs fake_time=True")
        await self._request({"ch": "time", "advance_ms": int(ms)})


class PurePythonBridge:
    """Same API as :class:`CoreBridge`, no subprocess. Approximate; for manual testing only.

    Implements hello/auth/replace, the seq filter, staleness and a minimal frame. It does not
    implement manual mode or the clock slew model.
    """

    def __init__(self, token: str, stale_ms: int = 3000, *, manual: bool = False, **_ignored):
        self.token = token
        self.stale_ms = stale_ms
        self.manual = manual
        self.stderr_lines: list[str] = []
        self._pending: dict[int, float] = {}
        self._client: int | None = None
        self._session: str | None = None
        self._last_seq: int | None = None
        self._state: dict | None = None
        self._last_accept: float | None = None
        self._frozen_ms: int | None = None
        self._boot = time.monotonic()
        self._offset = 0.0

    def _now(self) -> float:
        return time.monotonic() + self._offset

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    # -- state -----------------------------------------------------------------------------------
    def _stale(self) -> bool:
        return self._last_accept is None or (self._now() - self._last_accept) * 1000 > self.stale_ms

    def _ingest(self, text: str) -> dict:
        from bracketboard_agent.schema import SchemaError, parse_and_validate

        try:
            msg = parse_and_validate(text)
        except SchemaError as e:
            ack = {"type": "ack", "seq": 0, "result": "invalid", "error": e.code}
            if e.field:
                ack["field"] = e.field
            return ack
        seq, session = msg["seq"], msg["session"]
        if self._last_seq is not None and session == self._session:
            delta = ((seq - self._last_seq + 2**31) % 2**32) - 2**31
            if delta == 0:
                return {"type": "ack", "seq": seq, "result": "duplicate"}
            if delta < 0:
                return {"type": "ack", "seq": seq, "result": "stale"}
        self._session, self._last_seq = session, seq
        self._state = msg
        self._last_accept = self._now()
        return {"type": "ack", "seq": seq, "result": "applied"}

    # -- API -------------------------------------------------------------------------------------
    async def open_ws(self, conn: int) -> None:
        self._pending[conn] = self._now()

    async def ws_text(self, conn: int, text: str) -> list[dict]:
        def out(conn_, obj, close=False):
            return {"conn": conn_, "send": json.dumps(obj, separators=(",", ":")), "close": close}

        if conn in self._pending:
            del self._pending[conn]
            try:
                hello = json.loads(text)
            except ValueError:
                hello = None
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                return [out(conn, {"type": "error", "code": "bad_message"}, True)]
            if hello.get("token") != self.token:
                return [out(conn, {"type": "error", "code": "auth_failed"}, True)]
            outs = []
            if self._client is not None:
                outs.append(out(self._client, {"type": "error", "code": "replaced"}, True))
            self._client = conn
            outs.append(out(conn, {
                "type": "hello_ack", "supported_v": [1], "device": "scoreboard", "fw": "pure-python",
                "mode": "agent", "session": self._session, "last_seq": self._last_seq,
            }))
            return outs
        if conn != self._client:
            return []
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if not isinstance(obj, dict) or obj.get("type") != "state":
            return [out(conn, {"type": "error", "code": "bad_message"})]
        return [out(conn, self._ingest(text))]

    async def close_ws(self, conn: int) -> None:
        self._pending.pop(conn, None)
        if self._client == conn:
            self._client = None

    async def http_post(self, id, path: str, auth: str | None, body: str) -> tuple[int, str]:
        if auth != f"Bearer {self.token}":
            return 401, '{"error":"unauthorized"}'
        if path != "/state":
            return 404, '{"error":"not_found"}'
        if len(body.encode("utf-8")) > 1024:
            return 413, '{"error":"too_large"}'
        ack = self._ingest(body)
        return (400 if ack["result"] == "invalid" else 200), json.dumps(ack, separators=(",", ":"))

    async def tick(self) -> list[dict]:
        outs = []
        for c, t in list(self._pending.items()):
            if self._now() - t > 2.0:
                del self._pending[c]
                outs.append({"conn": c, "send": '{"type":"error","code":"hello_timeout"}', "close": True})
        return outs

    async def frame(self) -> dict:
        s = self._state
        if s is None:
            return {"screen": "splash", "clock": "-:--", "colon_on": False, "stale": False, "left_score": 0,
                    "right_score": 0, "left_warnings": 0, "right_warnings": 0, "period": 1,
                    "clock_state": "stopped", "weight": "", "bout": "", "colors_swapped": False,
                    "waiting": True, "manual": False}
        stale = self._stale()
        rem = s["clock_ms_remaining"]
        if s["clock_state"] == "running":
            rem = max(0, rem - int((min(self._now(), self._last_accept + self.stale_ms / 1000) - self._last_accept) * 1000))
        sec = -(-rem // 1000)
        return {"screen": s["screen"], "clock": f"{sec // 60}:{sec % 60:02d}", "colon_on": not stale,
                "stale": stale, "left_score": s["left_score"], "right_score": s["right_score"],
                "left_warnings": s["left_warnings"], "right_warnings": s["right_warnings"],
                "period": s["period"], "clock_state": s["clock_state"], "weight": s["weight"],
                "bout": s["bout"], "colors_swapped": s["colors_swapped"], "waiting": False, "manual": False}

    async def status(self) -> dict:
        return {"device": "scoreboard", "fw": "pure-python", "mode": "agent",
                "agent_live": not self._stale(), "session": self._session, "last_seq": self._last_seq,
                "uptime_s": int(time.monotonic() - self._boot)}

    async def advance_time(self, ms: int) -> None:
        self._offset += ms / 1000.0
