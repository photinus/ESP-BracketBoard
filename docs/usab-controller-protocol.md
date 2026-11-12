# USABracketing timer-controller protocol model

Derived by reading the `usab_controller` web app and its Java matside server (the zip supplied by the
project owner). This is **our model of what the controller emits**, plus the rules for mapping it onto
our snapshot (docs/protocol-v1.md). Everything below was read from source; items marked **(inferred)**
were not directly observable (the Java server ships as `.class` files only; its string constants were
inspected, not executed) and must be checked against a real capture before relying on them.

## 1. Topology

```
 Controller (browser app, LiveScoring.html)          <- the timer/score "driver"; source of truth
   | HTTP GET  localhost:6099/usab?function=updateTimer&...   (state push, event driven)
   v
 Matside server (Java VideoServer, com.sun HttpServer, port 6099, context /usab)
   |  GET /usab?function=getTimer   -> current Timer as JSON-ish text   <-- agent reads this
   |  GET /usab?function=getEvent   -> {event_id, device_key, server}
   |  GET /usab?function=alive      -> liveness probe
   v
 Overlays / virtual boards (poll getTimer every 1 s and extrapolate locally)

 Controller --HTTP--> physical LED clock  <ip>:6056   (see section 7; not our integration point)
 Controller --HTTP--> App server  <app_server_url>/api/live_scoring/*   (bout/match_format, scores, clock)
 Controller --postMessage--> launched "virtual" pages (JSON {code, object})
```

**Integration point for our agent: poll the matside server's `getTimer`** (it is exactly what the
project's own video overlays do), plus a one-off `getEvent` + app-server `bout` fetch per bout for
weight/bout number. The agent never writes to the controller.

## 2. Controller timer semantics (js/Timer.js, RidingTimer.js, ShotClock.js)

- Clocks **count down** in fractional **seconds** (`current_time`, a float). `tick()` runs every 50 ms
  (10 ms under 1 s), subtracting wall-clock elapsed time. The timer stops at `stopping_time`
  (`period.stop_seconds`, default 0): `running` becomes false, the horn sounds if time > 0 remained.
  `getTimer` does not expose `stopping_time` (assume 0; **TODO** confirm per match format).
- "Current timer" is whichever `Timer` is active; **`name` is that timer's name**, not the period.
  Timers are numbered: period timers = `period.index`; wrestler timers (injury/blood/recovery...) =
  `100+i` for wrestler 1 and `200+i` for wrestler 2 (names come from `match_format.wrestler_timers`;
  names seen in code: `Blood`, `Injury`, `Recovery`, `Head/Neck`); riding timer 301/302; shot clock 303.
  `Break` is a period timer; when it expires the controller auto-advances (`nextPeriod()`).
  Period names seen: `Period 1..3`, `Sudden Victory`, `Tie Breaker 2`, `TB Choice 1`, `UTB Choice`,
  `Break` (full list comes from the app server's match_format, not the zip).
- **An injury/blood timer is a countdown of its own**, and while it is current it is *the* clock shown
  on the physical board, with the injured wrestler's score blinking (BuritoClock: blink 0 none / 1 red /
  2 green). The period clock value is *not* reported while a wrestler timer is current.
- Setting the time by hand (`setCurrentTime`) stops the clock; `+/-1 s` buttons call `increment`;
  "revert" restores `last_stopped_time` (clock jumps back while stopped).
- **Riding time** (folkstyle) is a signed float: positive = wrestler 1 ahead, negative = wrestler 2.
  While the clock runs it moves by `+dt` if `w1_position == TOP`, `-dt` if `BOTTOM`, 0 if NEUTRAL.
  Constants: `NEUTRAL=0, TOP=1, BOTTOM=2`. The riding timer belongs to the current timer (period).
- **Shot clock** (freestyle/greco): counts down while the main clock runs; `wrestler_indicator` 1 or 2
  says whose it is; null/0 = none. Set to `starting_time + .99` when a color is chosen.

## 3. Controller -> matside server: `updateTimer`

`GET http://localhost:6099/usab?function=updateTimer&<query>` (browser is on the same machine; CORS
header `Access-Control-Allow-Origin` is added by the server). Sent **only on events**: start, stop
(including expiry), manual set/increment/revert, timer switch, score change, message. **There is no
periodic refresh while the clock runs**, so consumers must extrapolate.

| query key | meaning |
|---|---|
| `name` | current timer name (see section 2) |
| `time` | `getCurrentTimeRaw()`: float seconds remaining at the moment of sending |
| `running` | `1` / `0` |
| `bout_id`, `event_id` | opaque ids |
| `w1_color`, `w2_color` | hex **without `#`** (from `match_format.colors[].hex`), or `black` if unknown |
| `w1_score`, `w2_score` | match scores (ints as text) |
| `tie_winner` | `1`/`2`/`""` (freestyle/greco tie-break indicator) |
| `t1_score`, `t2_score` | dual/team scores |
| `riding_time` | signed float seconds (0 if the timer has none) |
| `w1_position` | `top` / `bottom` / `neutral` |
| `shot_clock` | float seconds (0 if none) |
| `wrestler_indicator` | `1` / `2` / `0` / `null` |
| `updated_millis` | **controller machine** epoch ms when the sample was taken |
| `message` | `"<counter>.<text>"` score/challenge event text (overlay strips up to the first `.`) |

## 4. Matside server -> clients: `getTimer`

Returns `Timer.toJSON()`. **It is not valid JSON**: keys and strings are single-quoted, strings are
not escaped, scores/ids are quoted strings, and the overlay `eval`s it. **Our parser must never eval
it.** Reconstructed template (from the class's string constants):

```
{'name':'Period 1', 'current_time':120.0, 'running':0, 'updated_millis':1763000000123,
 'bout_id':'1234','event_id':'77','w1_color':'FF0000','w2_color':'0000FF','w1_score':'3','w2_score':'1',
 'tie_winner':'', 't1_score':'0','t2_score':'0', 'riding_time':0.0, 'w1_position':'neutral',
 'shot_clock':0.0, 'wrestler_indicator':'', 'message':'4.T2 Smith', 'system_millis':1763000000150}
```

Numbers unquoted: `current_time`, `running`, `updated_millis`, `riding_time`, `shot_clock`,
`system_millis`. Everything else is a quoted string. `message` is free text and may contain `'`, so
extract fields by key, not by quote-balancing. Before any state push the server returns its default
(`name` = `Period 1`, empty ids/colors).
`system_millis` is the **server's** `currentTimeMillis()` at response time.

`getEvent` -> `{'event_id':'..','device_key':'..','server':'..'}` (**`device_key` is a credential:
never log it, never put it in snapshots**). `alive` is a liveness probe (body not inspected by the app).

## 5. Time-sync model

Controller samples at `updated_millis` (its clock); the server stamps `system_millis` at response.
If both run on one machine, `age_at_response = system_millis - updated_millis`. The overlay uses
`elapsed = now - updated_millis - (offset_millis - system_diff_millis)` with `offset_millis` a manual
per-site constant (server file placeholder `[offset_millis]`), which exists because the controller
may be on a different device (project notes: "matside computer time +100 ms", web server +200 ms).

Agent rule (all in ms, monotonic clock for local elapsed):

```
age_ms(now) = max(0, (system_millis - updated_millis) - controller_offset_ms) + (mono_now - mono_rx)
remaining_ms = current_time*1000 - age_ms     if running == 1
             = current_time*1000              otherwise
remaining_ms = clamp(remaining_ms, 0, 3_600_000)   (stopping_time assumed 0)
```

`controller_offset_ms` (default 0, configurable) = controller clock minus server clock. Because
the sign re-syncs from every snapshot and slews < 250 ms, a 100-200 ms constant error is
acceptable; a larger one shows as a visible jump on each stop.

Liveness: the controller sends no heartbeat, so an unchanged `updated_millis` is normal while stopped
or running. Agent liveness = **the matside server answered** (`alive`/`getTimer` HTTP 200), not new data. If
the server stops answering, `read_state()` returns None and the sign goes stale (3 s).
While `running` the agent keeps emitting snapshots with the extrapolated clock; if extrapolation
reaches 0 it reports `expired`.

## 6. Mapping onto our snapshot (agent side)

| snapshot field | source / rule |
|---|---|
| `left_score`, `right_score` | **Left = the red wrestler, right = green/blue** (matches the physical clock's fixed "Red"/"Green-Blue" fields). Classify `w1_color`/`w2_color` by hue; if neither classifies as red, left = wrestler 1. Config `swap_sides` flips. **(assumption: confirm which side the venue's sign puts red on.)** |
| `colors_swapped` | `false` by default (red is always left). Config-driven only. |
| `left_warnings`, `right_warnings` | **Not available** from `getTimer` (stalling/caution counts live in `bout.scores`). `0` until we map them from the app server (**TODO**, needs sample `bout` payload). |
| `period` | Parse from `name`: `Period N` -> N; `Sudden Victory`/`SV` -> 4; `Tie Breaker N`/`TB ...` -> 4+; `UTB ...` -> 6; clamp 1..9. If `name` is a non-period timer (`Break`, wrestler timers) **keep the last known period** (stateful), default 1. Overridable map in config. |
| `clock_ms_remaining` | Section 5 formula on the *current timer*, whatever its kind. |
| `clock_state` | `running` if `running==1`; else `expired` if remaining==0 after a run (or current_time==0); else `stopped`. The enum value `injury` is **not** used for wrestler timers: those are real countdowns, so they are `running`/`stopped` plus `clock_label` and `blink` (below). |
| `screen` | `splash` if `bout_id` is empty (no bout selected) or the server is in its default state; otherwise `score`. |
| `weight`, `bout` | Not in `getTimer`. Fetch once per `bout_id` via `getEvent`, then `POST {server}/api/live_scoring/bout` with form fields `event_id`, `device_key`, `bout_id`; take `bout.weight_name` and `bout.bout_number` (`"m"+match_id` if empty), truncated to 8 printable ASCII chars. Failure -> empty strings (never block state delivery). |
| `sender_ts`, `seq`, `session` | Set by the Publisher as today. |

### Optional extension fields (v1-compatible, additive)

Per the v1 forward-compat policy these are **optional**; the current firmware ignores them (follow-up:
core/`Frame`/panel support). The agent sends them when the source provides them.

| field | type | meaning |
|---|---|---|
| `clock_label` | string 0-12 printable ASCII | name of the clock being shown (`Period 2`, `Break`, `Blood`...) |
| `blink` | enum `none\|left\|right` | blink that side's score (set while a wrestler's injury/blood/recovery/head-neck timer is current; side = the owning wrestler `100+i` -> w1, `200+i` -> w2, mapped through the left/right rule above). `getTimer` does not report the timer number, so **until a real capture shows otherwise the agent infers the owner from the timer name only when the controller supplies it; otherwise `none`** (**TODO**). |
| `riding_ms` | int -3600000..3600000 | signed riding time, **positive = left side ahead** (flip sign if sides are swapped) |
| `riding_dir` | int -1, 0, 1 | rate sign while the clock runs: `+1` left accumulating (w1 TOP when w1 is left; w2 TOP otherwise), `-1` right accumulating, `0` neutral/none |
| `shot_clock_ms` | int 0..3600000 | shot clock remaining |
| `shot_clock_side` | enum `none\|left\|right` | whose shot clock |

## 7. Physical clock interface (documented, deliberately not used)

`http://<clock-ip>:6056/` with two dialects: "burito" (`/a6<hex payload>`; fixed-width hex fields:
update-time 1, clock hex4, update-running 1, running 1, blink 1, top-number hex4, red score1 hex2,
red periods 1, green score1 hex2, green periods 1, red score2 hex2, green score2 hex2, riding
[update 1, negative 1, hex4], w1 color 1, w1 position 1, clock decimals hex2, riding decimals hex2,
weight hex4, bout hex4, then a **checksum + random produced by obfuscated code**) and "pi"
(`/tw?function=updateClock&data=<comma list>`). The clock authenticates updates with that obfuscated
handshake, so emulating it from the ESP32 is not viable; reading `getTimer` is the supported route.

## 8. Open questions (need a real capture / the project owner)

1. A real `getTimer` response (verify quoting, number formats, default state, colors).
2. Which side the venue sign shows red on; whether `colors_swapped` should ever be driven.
3. `stop_seconds` per period (clock "expires" at non-zero for some formats?).
4. Owning wrestler of an injury timer is not in `getTimer` (timer number is not sent). Options:
   read `bout`/controller state, or extend the controller push (out of our scope).
5. Warnings/cautions/stalling counts: need the app-server `bout.scores` shape.
6. Is the agent run on the controller laptop (clock offset 0) or elsewhere?
