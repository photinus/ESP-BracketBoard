# BracketBoard protocol v1

The sign is a **display endpoint**. A local agent (laptop/Pi at the mat) reads USABracketing's
timer/score state and pushes **full, idempotent snapshots** to the ESP32. In agent mode the sign
never decides scores, periods, or clock start/stop. Firmware knows only this schema.

## 1. State message (agent -> sign)

```json
{"type":"state","v":1,"session":"a1b2c3d4e5f6","seq":1042,"sender_ts":1763000000123,
 "left_score":3,"right_score":1,"left_warnings":0,"right_warnings":1,
 "period":2,"clock_ms_remaining":87340,"clock_state":"running",
 "screen":"score","weight":"157","bout":"1203","colors_swapped":false}
```

| field | type | rule |
|---|---|---|
| type | string | must be `"state"` |
| v | int | schema major version; only `1` accepted, else `unsupported_version` |
| session | string | 1-16 chars `[A-Za-z0-9_-]`; random per agent process start |
| seq | int | 0..4294967295; +1 for every snapshot sent |
| sender_ts | int | 0..2^53-1; agent wall clock, Unix ms. Diagnostics only, **never used in clock math** |
| left_score, right_score | int | 0..99 |
| left_warnings, right_warnings | int | 0..9 |
| period | int | 1..9 (1-3 regulation; 4+ overtime/tiebreak) |
| clock_ms_remaining | int | 0..3600000, sampled immediately before sending |
| clock_state | enum | `running` \| `stopped` \| `injury` \| `expired` |
| screen | enum | `score` \| `splash` |
| weight, bout | string | 0-8 bytes printable ASCII (0x20-0x7E) |
| colors_swapped | bool | |

**Validation.** Every field is required. All-or-nothing: any missing/invalid field rejects the
whole message and nothing is applied. Numbers must be integral and in range (no clamping). Wrong JSON
types are rejected. Messages over **1024 bytes** are rejected (`too_large`). Duplicate keys: last wins.
`left`/`right` are screen sides as the agent wants them shown; `colors_swapped` tells the sign to swap
the red/green side colours (it does not move scores).

**Error codes** (wire strings, `parse_error_code`): `too_large`, `not_json`, `not_object`,
`wrong_type`, `unsupported_version`, `missing_field`, `bad_type`, `out_of_range`.
Precedence when several apply: `too_large` > `not_json` > `not_object` > `wrong_type`
(`type` missing or not `"state"`) > `unsupported_version` (`v` is an int != 1; `v` missing is
`missing_field`; `v` non-int is `bad_type`) > per-field errors in table order.

**Versioning / forward compat.** Unknown fields are ignored. New *optional* fields do not bump `v`.
Reserved optional names: `left_name`, `right_name`, `left_team`, `right_team`, `injury_ms`,
`period_label`, `aux`. Any change to the meaning, type, or required-ness of an existing field bumps `v`.
The sign reports `supported_v` in `hello_ack`; the agent sends the highest common version.

## 2. Seq rules (stale / duplicate / restart)

Filter state: `(session, last_seq, has_last)`.
- `has_last == false` or `session != current session` -> **accept** as a new epoch (adopt session,
  `last_seq = seq`). An agent restart (new session, seq back to 1) takes over immediately.
- Same session: `delta = (int32_t)(seq - last_seq)` (RFC 1982). `delta > 0` accept; `0` -> `duplicate`;
  `< 0` -> `stale`. Wraparound at 2^32 works. Duplicate/stale are dropped and **do not refresh liveness**.
- Sign reboot: `has_last == false`, first snapshot accepted.
- WS and HTTP feed the same filter; a snapshot sent on both applies once.
- Known limit: an old agent process that is still alive can take control back over HTTP. Documented, not mitigated in v1.

## 3. WebSocket (`ws://scoreboard.local/ws`)

One **control client** at a time. Token is sent in the first message (not URL/header; see
architecture.md for rationale).

- client -> sign, first frame within 2000 ms: `{"type":"hello","v":1,"token":"...","agent":"name/ver"}`
- sign -> client: `{"type":"hello_ack","supported_v":[1],"device":"scoreboard","fw":"0.1.0","mode":"agent"|"manual","session":<str|null>,"last_seq":<int|null>}`
- sign -> client, for every state message: `{"type":"ack","seq":N,"result":"applied"|"duplicate"|"stale"|"invalid","error":"<code>","field":"<name>"}` (`error`/`field` only when `invalid`; `field` may be absent)
- sign -> client errors, followed by close: `{"type":"error","code":"auth_failed"|"hello_timeout"|"unsupported_version"|"bad_message"|"replaced"}`
  (`bad_message` closes the connection only when it happens before hello; a control client sending a non-state message gets `error bad_message` and stays connected.)

Rules: connections are *pending* until a valid hello; bad token -> `auth_failed`+close; no hello in 2 s ->
`hello_timeout`+close; max 3 pending. A newly authenticated client **replaces** the current one (old gets
`replaced`+close) which handles half-open sockets after agent restart / laptop sleep. State frames from
non-control connections are ignored.

## 4. HTTP fallback

- `POST /state`, header `Authorization: Bearer <token>`, body = state message.
  `200` + ack body for applied/duplicate/stale; `400` invalid (ack body with `error`,`field`);
  `401` bad/missing token; `413` too large.
- `GET /status` (no auth): `{"device","fw","mode","agent_live","session","last_seq","uptime_s"}`; never the token.
- `POST /manual/cmd` (token): see architecture.md "Manual mode".

## 5. Liveness and cadence

- Agent sends immediately on any meaningful change (rate limit 20 Hz); otherwise a fresh snapshot (new seq)
  every **1.0 s** as heartbeat.
- Sign treats the link as **stale after N = 3 s** (configurable 2-10 s) with no *accepted* snapshot.
- While stale: the clock freezes at the value it had when the timeout hit (no jump back); the colon is off
  regardless of `clock_state`; every other field keeps its last value.
- First accepted snapshot after stale: the clock always **snaps**.

## 6. Clock model (sign side)

On each accepted snapshot at local time `t_rx` store `R`, `S`. Let `D_prev` be the display value just before.
- `base(t)` = `R - (t - t_rx)` if running (ms), else `R`; `0` if `S == expired`.
- Drift `e = D_prev(t_rx) - R`. **Slew** (`off0 = e`) only if previous *and* new state are `running`, not stale,
  had data, and `|e| <= 250 ms`. Otherwise **snap** (`off0 = 0`): first data, any state change, stale recovery,
  `|e| > 250`.
- `off(t) = sign(off0) * max(0, |off0| - slew_permille*(t-t_rx)/1e6)` (default 100 ms of correction per s).
- `display = max(0, base + off)`. Display rate while slewing is 0.9x-1.1x: it never counts up.
- Hitting 0 locally: hold `0:00`, state stays `running`; the sign waits for the agent to say `expired`.
- Stale: `display = D(t_rx + stale_timeout)`, `stale = true`, `colon_on = false`. `colon_on = has_data && !stale`.
- Rendering: whole seconds rounded **up**, `M:SS` (see `format_clock`).
