# Architecture

```
USABracketing --(adapter)--> Python agent --WS /ws (primary) | HTTP POST /state--> ESP32 sign
 (source of truth)           snapshots: full, idempotent                        (display endpoint)
```

## Boundaries
- **Firmware knows only the snapshot schema** (docs/protocol-v1.md). Nothing USABracketing-specific is in
  `core/` or `firmware/`. All USABracketing mapping lives in `agent/bracketboard_agent/adapters/usabracketing.py`.
- `core/` is portable C++17 (no ESP/OS headers) and is compiled both on the host (tests) and by ESP-IDF.
  cJSON v1.7.18 is vendored in `core/third_party/cjson` and used on both; it never appears in public headers.
- `firmware/` is a thin ESP-IDF layer that maps sockets to `ConnId`, calls `ControlHub`/`Board`, and
  renders `Board::frame()` to a `Display`. It cannot be built in the dev container (no ESP-IDF).

## Threading (firmware)
One FreeRTOS mutex guards `Board` + `ControlHub`. Handlers lock -> call core -> unlock -> send outbound.
A 20 Hz render task locks, calls `board.frame()`, unlocks, then `display.render()`. A 200 ms `esp_timer`
queues `hub.tick()` through `httpd_queue_work`. No blocking in the render task.

## WS token placement
First message (`hello`), not URL query (leaks into logs/history/proxies) and not an `Authorization`
header (browsers cannot set it on WebSocket; also needs larger header buffers). Costs one round trip and a
2 s pending window bounded by the 3-pending cap. HTTP uses `Authorization: Bearer`. No TLS in v1 (plaintext
token on venue Wi-Fi: accepted limitation).

## Wi-Fi / mDNS / config (firmware)
- STA to configured SSID (15 s / 5 retries). On failure: APSTA, AP `BracketBoard-XXXX` (last 4 MAC hex),
  WPA2 `ap_pass`, 192.168.4.1. Retry STA every 30 s; drop AP after STA connected for 60 s.
- mDNS via managed component `espressif/mdns`: hostname default `scoreboard` -> `scoreboard.local`;
  `_bracketboard._tcp` port 80 TXT `v=1,path=/ws`, plus `_http._tcp`. Hostname configurable (multi-mat).
- Kconfig defaults `BB_WIFI_SSID, BB_WIFI_PASS, BB_TOKEN (empty), BB_HOSTNAME, BB_AP_PASS,
  BB_STALE_TIMEOUT_MS=3000, BB_MANUAL_ENABLED=n`; NVS namespace `bb` overrides (`ssid, pass, token, host,
  ap_pass, stale_ms, manual_en`). Empty token on first boot: generate 8 alphanumerics with `esp_random`,
  store in NVS, print on serial, show on the splash for the first 60 s while in AP mode.
  `GET/POST /config` token-protected, applied on reboot.

## Manual mode (fallback)
- Setting `manual_en`, **default off**. Source is manual only when `manual_en && !agent_live()`.
- Any accepted agent snapshot immediately takes over the display (agent is always source of truth).
  While the agent is live `POST /manual/cmd` returns 409 `{"result":"manual_locked"}`; manual state is
  kept, not reset. When the agent goes stale (3 s) and manual is enabled, manual state shows again.
- `ManualController` (core) is the on-device authoritative clock/score. Device serves a minimal
  `web/manual.html` (big buttons: start/stop, +1..+4 per side, warning, period, set clock, swap). The page
  keeps the token in localStorage, sends `POST /manual/cmd` (Bearer) and polls `GET /manual/state` at 4 Hz.
  `GET /manual/state` returns the manual `Snapshot` as a state message (token required).

## Stdio bridge: `bb_core_cli` (host/bb_core_cli.cpp)
Line-delimited JSON on stdin/stdout, one object per line, driving a real `Board`+`ControlHub`.
Args: `--token T [--stale-ms 3000] [--fake-time] [--manual]`.
Without `--fake-time` it uses `SteadyTimeSource` (real time); with it, time only moves on `time` requests.
It must flush stdout after every response line and exit cleanly on stdin EOF.

Requests (stdin):
- `{"ch":"ws","conn":1,"ev":"open"}` | `{"ch":"ws","conn":1,"ev":"text","data":"<raw text frame>"}` | `{"ch":"ws","conn":1,"ev":"close"}`
- `{"ch":"http","id":7,"path":"/state"|"/manual/cmd","auth":"Bearer x"|null,"body":"<raw>"}`
- `{"ch":"tick"}`  (calls `hub.tick`)
- `{"ch":"frame"}`  `{"ch":"status"}`
- `{"ch":"time","advance_ms":N}` (only with `--fake-time`)
Every request produces zero or more output lines, then exactly one terminating line
`{"ch":"done"}` (so a client can read until `done`).

Responses (stdout):
- `{"ch":"ws","conn":1,"send":"<text>","close":false}` (one line per `Outbound`, in order)
- `{"ch":"http","id":7,"status":200,"body":"<raw JSON text>"}`
- `{"ch":"frame","frame":{"screen":"score","clock":"1:27","colon_on":true,"stale":false,"left_score":3,
  "right_score":1,"left_warnings":0,"right_warnings":1,"period":2,"clock_state":"running","weight":"157",
  "bout":"1203","colors_swapped":false,"waiting":false,"manual":false}}`
- `{"ch":"status","body":"<status_json text>"}`
- `{"ch":"done"}`

## Python agent contract (agent/bracketboard_agent)
- `schema.py`: `@dataclass BoutState(left_score,right_score,left_warnings,right_warnings,period,
  clock_ms_remaining,clock_state,screen,weight,bout,colors_swapped)`; `class SchemaError(Exception)`
  with `.code` (same strings as C++) and `.field`; `validate_state_message(d: dict) -> None` (mirrors C++ rules
  and error precedence exactly; verified against docs/fixtures); `to_message(state, session, seq, sender_ts) -> dict`.
- `adapter.py`: `class SourceAdapter(ABC)` with `async start()`, `async stop()`, abstract
  `async read_state() -> BoutState | None` (CURRENT full state, clock sampled now; None = unavailable),
  `async wait_change(timeout: float)` (default `asyncio.sleep(timeout)`).
- `adapters/mock.py`: `MockBoutAdapter(script_path, speed=1.0, loop=False)` replays `scripts/sample_bout.json`
  (`[{"t": s, "do": "start|stop|injury|resume|score|warn|period|expire|set_clock|swap|splash|weight|bout", ...}]`),
  keeps its own authoritative clock; `finished` property.
- `adapters/usabracketing.py`: stub subclass; every method raises `NotImplementedError` with TODOs.
- `publisher.py`: `Publisher(adapter, transport, heartbeat_s=1.0, poll_hz=10, max_hz=20, clock=time.monotonic)`;
  `async run()` / `stop()`. Session = `secrets.token_hex(6)`; seq starts at 1, `& 0xFFFFFFFF`; `sender_ts =
  time.time_ns()//1_000_000`. Immediate send on any non-clock field change or clock deviating > 150 ms from
  linear extrapolation of the last sent value; else heartbeat each `heartbeat_s`; immediate send on reconnect.
- `transport.py`: `Transport` ABC `async send(msg: dict) -> str | None` (ack result), `on_connected` callback;
  `WsTransport(url, token)` (hello, 2 s hello_ack wait, backoff 0.5/1/2/4/5 s + jitter),
  `HttpTransport(url, token)` (`urllib`, 1 s timeout), `FailoverTransport(ws, http)` (WS when connected, else
  HTTP while WS reconnects in the background; never both; HTTP-only with warning if `websockets` missing).
- CLI: `python -m bracketboard_agent --host scoreboard.local --token XXX [--mock [--script f] [--speed 4]]
  [--http-only] [--heartbeat 1.0]`.
- `fake_esp/`: `server.py` runs a real `websockets` server (`/ws`) + `http.server` (`/state`, `/status`) that forward
  events to a `bb_core_cli` subprocess (`bridge.py`); `--pure-python` mode for manual testing without the C++ build.
