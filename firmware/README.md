# BracketBoard firmware (ESP-IDF 5.x)

Thin ESP-IDF layer over the portable `core/` library: Wi-Fi, mDNS, one HTTP/WebSocket server,
a 20 Hz render task and NVS-backed config. The display driver is a **stub** (see "Display").

> This tree was written without an ESP-IDF toolchain available. It has not been compiled or run
> on hardware; see `REVIEW_CHECKLIST.md` for what was and was not verified.

## Layout

| path | purpose |
|---|---|
| `main/main.cpp` | wiring: config -> Board/ControlHub -> Wi-Fi -> mDNS -> server -> render task |
| `main/wifi_mgr.*` | STA first, APSTA fallback (`BracketBoard-XXXX`, 192.168.4.1) |
| `main/mdns_svc.*` | `scoreboard.local`, `_bracketboard._tcp` (TXT `v=1`, `path=/ws`), `_http._tcp` |
| `main/net_server.*` | `/ws`, `POST /state`, `GET /status`, `/manual/*`, `/config`, `/` |
| `main/config_store.*` | Kconfig defaults + NVS (`bb` namespace) overrides, first-boot token |
| `main/panel_display.*` | `bb::Display` stub (logs the Frame at 1 Hz) |
| `main/web/manual.html` | embedded manual-mode controller page |
| `components/bb_core` | compiles `../core/src/*.cpp` + vendored cJSON as an IDF component |

## Build and flash

```sh
. $IDF_PATH/export.sh            # ESP-IDF >= 5.1
cd firmware
idf.py set-target esp32          # or esp32s3 / esp32c3 ...; sdkconfig.defaults assumes 4 MB flash
idf.py menuconfig                # "BracketBoard" menu, see below
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

`sdkconfig.defaults` is only applied when `sdkconfig` is created; delete `sdkconfig` to re-apply
after editing it. Adding a file under `core/src/` needs `idf.py reconfigure` (the component globs).
The `espressif/mdns` managed component is downloaded on the first configure (needs internet).

## Configuration

`idf.py menuconfig` -> **BracketBoard**:

| option | default | meaning |
|---|---|---|
| `BB_WIFI_SSID` / `BB_WIFI_PASS` | empty | network to join; empty SSID = start in AP setup mode |
| `BB_TOKEN` | empty | empty = generate on first boot (recommended; a value here is baked into the binary) |
| `BB_HOSTNAME` | `scoreboard` | mDNS name (`<host>.local`); change per mat |
| `BB_AP_PASS` | `bracketboard` | WPA2 password of the fallback AP (8-63 chars). **Change it.** |
| `BB_STALE_TIMEOUT_MS` | 3000 | 2000-10000; no accepted snapshot for this long = stale |
| `BB_MANUAL_ENABLED` | n | enable the on-device manual controller fallback |

At runtime, NVS namespace `bb` overrides these (`ssid, pass, token, host, ap_pass, stale_ms,
manual_en`) through `POST /config`. Changes apply **on reboot**.

### Token

If no token is configured, 8 alphanumerics are generated on first boot, stored in NVS and printed
**once** on the serial console:

```
==========================================
 BracketBoard first boot: control token
   Xk3Qm9aB
...
```

Read it with `idf.py monitor` (reset the board right after flashing if you missed it, but note it
is only printed on the boot that generates it; to regenerate, `idf.py erase-flash` or set a new
token via `POST /config`). The token is never logged otherwise and never returned by `/config` or
`/status`. On first boot it is also meant to be shown on the splash for 60 s while the AP is up
(the stub display just logs that the overlay is active; the real driver must draw it).

### Wi-Fi behaviour

1. STA to the configured SSID (15 s / 5 attempts).
2. On failure: AP+STA, AP `BracketBoard-XXXX` (last 4 hex of the AP MAC), WPA2 with `ap_pass`,
   `192.168.4.1`. STA is retried every 30 s; the AP is dropped after STA has been up for 60 s.
3. With no SSID configured the device stays in AP-only mode.

Join the AP and configure the sign:

```sh
curl -H "Authorization: Bearer $TOKEN" http://192.168.4.1/config
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -d '{"ssid":"VenueWiFi","pass":"secret-pass","host":"mat2","reboot":true}' \
     http://192.168.4.1/config
```

Accepted keys: `ssid` (<=32), `pass` (empty or 8-63), `token` (8-32 of `[A-Za-z0-9_-]`),
`host` (`[a-z0-9-]`, <=32), `ap_pass` (8-63), `stale_ms` (2000-10000), `manual_en` (bool),
`reboot` (bool, reboots 1 s after the reply).

## Pointing the agent at the sign

```sh
python -m bracketboard_agent --host scoreboard.local --token Xk3Qm9aB            # WebSocket /ws
python -m bracketboard_agent --host scoreboard.local --token Xk3Qm9aB --http-only # POST /state
```

If your OS cannot resolve `.local`, use the IP printed on serial (`got IP ...`) or `192.168.4.1`
when connected to the fallback AP. `curl http://scoreboard.local/status` should answer without a
token.

## Manual mode

Off by default (`BB_MANUAL_ENABLED` or `{"manual_en":true}` + reboot). Open
`http://scoreboard.local/`, paste the token (kept in the browser's localStorage). The page is locked
while an agent is live; when the agent goes stale (3 s) the manual state shows again.

## Display

`main/panel_display.cpp` is a stub: it logs each Frame once per second. The panel hardware is not
decided. Implement `PanelDisplay::begin()` / `render()` (search for `TODO(HARDWARE)`). `render()` is
called at ~20 Hz from the render task and must not block for long.

## Notes

- No TLS in v1; the token travels in clear text on the venue Wi-Fi (documented limitation).
- One mutex guards `Board` + `ControlHub`; WebSocket frames are only sent after it is released.
