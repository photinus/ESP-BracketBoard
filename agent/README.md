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

## Writing the USABracketing adapter

All USABracketing knowledge lives in `bracketboard_agent/adapters/usabracketing.py`, currently a stub
whose methods raise `NotImplementedError` with detailed TODOs (data source, polling vs push, the field
mapping table, left/right and `colors_swapped`, period labelling, injury state, clock sampling).
To implement it:

1. Subclass `SourceAdapter` (already done): implement `start()` / `stop()` and the one required method
   `async read_state() -> BoutState | None`.
2. `read_state()` returns the CURRENT full state with `clock_ms_remaining` sampled right now
   (extrapolate a value+timestamp from the source while it runs). Return `None`, never raise, while the
   source is unreachable or no bout is loaded; the sign will go stale on its own.
3. Clamp to the schema ranges (scores 0-99, warnings 0-9, period 1-9, `weight`/`bout` at most 8 printable
   ASCII characters). The values are `clock_state` `running|stopped|injury|expired` and `screen`
   `score|splash`.
4. For push sources, override `wait_change(timeout)` to wake on a change event so updates go out
   immediately rather than at the next poll (`--poll-hz`, default 10).
5. Pass any connection options through the constructor (`build_adapter` in `cli.py`).

## Tests

```
python -m pytest agent -q          # from the repo root
```

The schema test checks every file in `docs/fixtures` (valid files must parse; an invalid file's prefix
before `__` must equal the `SchemaError.code`). Number rule: JSON numbers must be integral, so `1.5`
is `bad_type`, `bool` is never an int, and an integral float such as `3.0` is accepted (cJSON cannot tell it
from `3`; the agent itself never emits floats).
