# BracketBoard agent

Python agent that reads bout state from a source (USABracketing, or a scripted mock) and pushes
full, idempotent snapshots to the ESP32 sign over WebSocket (primary) or HTTP `POST /state`
(fallback). The protocol is in `../docs/protocol-v1.md`; the agent contract is in
`../docs/architecture.md`.

Requires Python 3.10+ and `websockets>=12`.

```
pip install -e agent            # or: pip install -e 'agent[test]'
```

Without installing, run from the `agent/` directory (`python -m bracketboard_agent ...`).

## Mock mode (no USABracketing needed)

Replays `bracketboard_agent/scripts/sample_bout.json`: a full 3-period bout with takedown, escape,
near-fall, stalling warning, injury stoppage, period breaks, a clock edit, a colour swap, expiry and
a final splash screen (about 7.5 minutes at `--speed 1`).

```
python -m bracketboard_agent --mock --host scoreboard.local --token XXXXXXXX
python -m bracketboard_agent --mock --speed 4 --loop --token XXXXXXXX      # fast, repeating demo
python -m bracketboard_agent --mock --script my_bout.json --token XXXXXXXX
```

`--speed N` scales script time and the game clock together. At `N != 1` the clock the sign shows
runs N times faster, so use it for demos/tests only. After the script ends the agent keeps
sending heartbeats of the final state (so the sign does not go stale); use `--exit-when-finished`
to quit instead.

Script format: a JSON list of `{"t": <script seconds>, "do": <action>, ...}`. Actions: `start`,
`stop`, `injury`, `resume`, `score` (`side`, `points`), `warn` (`side`, `n`), `period` (`n`,
`clock_s`|`clock_ms`), `expire`, `set_clock` (`seconds`|`ms`), `swap` (`value`), `splash` (`on`),
`weight` / `bout` (`value`). A running clock that reaches 0 becomes `expired` by itself.

## Pointing at a real sign

```
python -m bracketboard_agent --adapter usabracketing --host scoreboard.local --token XXXXXXXX
```

(see the USABracketing section below for the controller-side options)

* `--host` defaults to `scoreboard.local` (mDNS); use the IP (for example `192.168.4.1` when
  joined to the sign's `BracketBoard-XXXX` access point) if mDNS does not resolve. `--port`
  defaults to 80.
* The token is printed on the sign's serial console and shown on its splash for the first 60 s in AP
  mode. Pass it with `--token` or the `BRACKETBOARD_TOKEN` environment variable.
* The agent connects with WebSocket (`ws://HOST/ws`, token in the first `hello` frame) and falls
  back to `POST /state` while WebSocket is down, reconnecting with 0.5/1/2/4/5 s backoff. `--http-only`
  disables WebSocket.
* `auth_failed` (wrong token) is logged as `AUTH FAILED` and retried only every 30 s. `replaced` means
  another controller took the sign over (is a second agent running?).
* `--heartbeat 1.0` is the snapshot period when nothing changes (the sign goes stale after 3 s).
  `-v` / `-vv` for more logging.

## Cadence

The publisher sends a snapshot immediately on any non-clock field change or when the clock
deviates more than 150 ms from the extrapolation of the last sent value (a clock edit), at most
`--max-hz` (20) per second, right after every (re)connect, and otherwise one fresh snapshot (new
`seq`) per heartbeat. Plain clock ticks send nothing: the sign extrapolates a running clock. Each
agent process uses a new random `session` and counts `seq` from 1 (masked at 2^32). Snapshots the
adapter produces that violate the schema are logged and not sent.

## USABracketing

The agent reads the controller through the **matside server** that ships with the USABracketing
controller (the Java server on port 6099 that the project's own video overlays poll). It never writes
to the controller. The protocol model, the field mapping and the open questions are in
`../docs/usab-controller-protocol.md`; the code is `bracketboard_agent/usab/` (model, parser, clock
extrapolation, mapping, HTTP clients) plus the glue in `bracketboard_agent/adapters/usabracketing.py`.

### Running against the real controller (same laptop)

Run the agent on the laptop that runs the controller (so the matside server is `127.0.0.1:6099` and both
clocks are the same clock):

```
python -m bracketboard_agent --adapter usabracketing --host scoreboard.local --token XXXXXXXX
```

| flag | default | meaning |
|---|---|---|
| `--usab-host` / `--usab-port` | `127.0.0.1` / `6099` | where the matside server listens |
| `--usab-offset-ms N` | `0` | clock offset, see below; leave at 0 when the agent runs on the controller laptop |
| `--swap-sides` | off | flip the side rule. Default: the **red** wrestler is on the **left**, green/blue on the right (if neither wrestler's color is red, wrestler 1 is left) |
| `--no-bout-lookup` | off | do not fetch weight / bout number from the app server (they stay empty) |

What it does: `read_state()` polls `getTimer` on every call (10 Hz by default, cheap on localhost) and
keeps the receive time. The controller sends **nothing while a clock runs**, so a running clock is
extrapolated from the sample (`current_time - age`), and reported `expired` when it reaches 0. When the
bout changes, `getEvent` and the app server's `/api/live_scoring/bout` are asked in the background
(cached per bout, retried with backoff 1/2/4/8/15/30 s, never blocking state delivery) for the weight
and bout number. If the matside server stops answering (or answers garbage) `read_state()` returns
`None`, the sign goes stale after 3 s, one warning is logged per outage and one line when it is back.
The `device_key` the controller hands out in `getEvent` is a credential and is never logged.

**Clock offset.** `--usab-offset-ms` is *subtracted* from the controller-side age of a sample
(`age = max(0, system_millis - updated_millis - offset) + local elapsed`), the same convention as the
overlay's `offset_millis`. It only matters if the controller runs on a different machine than the
matside server (the sample's `updated_millis` is the controller's clock, `system_millis` the server's).
A positive value shortens the computed age. Note the spec text calls it "controller clock minus server
clock", which has the opposite sign from its own formula: trust the formula and the overlay; if a
running clock on the sign is consistently *behind* the controller, increase the offset, if *ahead*,
decrease it. A constant error of 100-200 ms is invisible (the sign slews < 250 ms), larger ones show as
a jump on every stop. If the controller laptop and the agent laptop are different machines, also keep
their clocks synchronised (NTP): `system_millis - updated_millis` is only the *controller* side of the
age; the agent's own receive time is measured with its monotonic clock and does not depend on wall
clocks.

### What is verified and what is inferred

Verified by reading the controller's JavaScript (`js/VideoServer.js`, `js/Timer.js`, `java/apps/overlays/Overlay.html`):
the `updateTimer` query keys and types, that updates are event-driven only (nothing while a clock runs),
that the clock counts down in float seconds, riding time sign and rate (`top` = `+dt`, `bottom` = `-dt`),
what the overlay does with `getTimer` (polls at 1 Hz, `eval`s the reply, extrapolates with
`updated_millis`), the `getEvent` -> `POST {server}/api/live_scoring/bout` flow with `event_id`,
`device_key`, `bout_id`, and that the reply has `status == "success"` and `bout.{weight_name,
bout_number, match_id}` (bout number falls back to `m<match_id>`).

**Inferred, not observable** (the Java server only ships as `.class` files): the exact text format of
`getTimer` (single quotes, quoted strings, not valid JSON: the parser accepts that and real JSON), that
`system_millis` is stamped when the response is built, the shape of `getEvent`, and the value of the
`alive` reply. Also unconfirmed: which side the venue's sign wants red on, `stop_seconds` per period
(the clock is assumed to expire at 0), the owner of an injury timer (so `blink` stays `none` unless the
timer name carries `W1`/`W2`), warnings (always 0 for now), and the period numbers for tie-breakers
(`UTB` is 6 as specified, which collides with `Tie Breaker 2`; override with `period_overrides`).
Check all of that against a real capture before a tournament.

### Capturing a real `getTimer` sample

With the controller running and a bout selected, on the controller laptop:

```
curl -s 'http://127.0.0.1:6099/usab?function=getTimer'   > getTimer_stopped.txt
curl -s 'http://127.0.0.1:6099/usab?function=getEvent'   > getEvent.txt    # contains the device key: do not share or commit
curl -s 'http://127.0.0.1:6099/usab?function=alive'      > alive.txt
```

Take samples in different situations (nothing selected, stopped, running, a Break, a blood/injury timer,
riding time, shot clock, a score message with an apostrophe in a name) and drop them into
`tests/fixtures/usab/` (remove the `device_key` first); `test_usab_parse.py` shows how they are used.
Send the first one (stopped, with a bout selected) to the maintainers: it settles the format questions above.

### Try it without the controller

```
python -m fake_esp --token T --port 8080                                                  # fake sign (real C++ core if built)
python -m fake_esp.matside --port 6099                                                    # fake controller + app server, demo bout loop
python -m bracketboard_agent --adapter usabracketing --host 127.0.0.1 --port 8080 --token T -v
```

`fake_esp.matside` emulates `/usab?function=alive|getTimer|getEvent|updateTimer|updateEvent` in the real
quirky format and a minimal `POST /api/live_scoring/bout`; the demo plays a bout with scores, a blood
timer, a break, riding time, a shot clock and expiry, alternating which wrestler wears red. In tests,
`fake_esp.FakeMatside` (`set_timer`, `controller_start/stop/score/expire`, `set_event`, `set_bout`,
`down()` / `up()`, `fault`) drives the same server from code.

### Extension fields

The adapter also sends the optional v1-compatible fields `clock_label`, `blink`, `riding_ms`,
`riding_dir`, `shot_clock_ms`, `shot_clock_side` (docs section 6). The current firmware ignores them.
`riding_*` is only sent once the controller reports riding time or a top/bottom position, `shot_clock_*`
only while a shot clock exists. The publisher treats `riding_ms` / `shot_clock_ms` like the clock: it
does not send on every poll while they tick, only on deviation from the linear extrapolation.

### Exit codes

`0` normal exit, `2` usage/configuration error (no token, no source, bad option value), `3` the
selected adapter is not implemented (kept for adapter authors).

## Tests

```
python -m pytest agent -q          # from the repo root
```

The schema test checks every file in `docs/fixtures` (valid files must parse; an invalid file's prefix
before `__` must equal the `SchemaError.code`). Number rule: JSON numbers must be integral, so `1.5`
is `bad_type`, `bool` is never an int, and an integral float such as `3.0` is accepted (cJSON cannot tell it
from `3`; the agent itself never emits floats).
