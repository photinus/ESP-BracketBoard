# Firmware review checklist

Nothing here has been compiled with ESP-IDF or run on hardware (no toolchain in the dev container).

## Verified by inspection / host syntax check
- [x] `g++ -std=gnu++17 -fsyntax-only -Wall -Wextra` is clean for every `main/*.cpp` against
      **hand-written stub IDF headers** (catches typos, bad member names, lambda/return types,
      missing std includes; it does NOT validate real IDF signatures). This caught one missing
      `freertos/task.h`, now fixed.
- [x] Only public headers from `core/include/bb/` are used; no frozen file was modified.
- [x] No ESP/OS includes leak into `core`; cJSON headers are used only in `net_server.cpp`.
- [x] Mutex discipline: `MutexLock` scopes contain only core calls (no socket I/O, no logging of
      secrets); `send_outbound()` always runs after the lock is released. The render task uses a
      10 ms bounded `xSemaphoreTake` and re-renders the previous frame on failure.
- [x] fd->ConnId table is touched only from the httpd task (handlers, `close_fn`, `httpd_queue_work`
      tick), so it needs no lock. The esp_timer callback only enqueues work (and de-duplicates).
- [x] Custom `close_fn` closes the socket itself (default behaviour replaced).
- [x] Body reads capped at 1025 bytes (`/state`, `/manual/cmd`) / 512 (`/config`); oversize bodies are
      answered by the core (`413`) and the session is closed afterwards.
- [x] Token is printed exactly once (first boot, `printf` in `main.cpp`); not logged elsewhere;
      `/config` GET and `/status` never return it; display stub never prints the overlay token.
- [x] Wi-Fi: mode is set before the AP/STA config for that interface; state machine is single-task
      (events via queue); retry/timeout constants match architecture.md (15 s/5, 30 s, 60 s).
- [x] Config values are validated on save and sanitised on load (AP password 8-63, etc.).

## NOT verified (needs a real build / board)
- [ ] Real compile against ESP-IDF 5.x: exact `esp_http_server` field names
      (`handle_control_frames`, `supported_subprotocol`, `global_user_ctx`, `close_fn` type),
      `wifi_sta_config_t::sae_pwe_h2e`, `WIFI_INIT_CONFIG_DEFAULT()` in C++.
- [ ] Component name `espressif__mdns` in `PRIV_REQUIRES` and `espressif/mdns: ^1.2.0` resolution.
- [ ] `httpd_sess_trigger_close()` being callable from the httpd task itself (used after sending
      error frames); if an IDF version refuses, replace with returning `ESP_FAIL` from the handler.
- [ ] `file(GLOB)` + out-of-tree sources in `components/bb_core` link correctly; no duplicate
      `cJSON` symbols (IDF `json` component must not be pulled in by anything).
- [ ] Linking `main` against cJSON symbols that live in the `bb_core` archive.
- [ ] `httpd_ws_send_frame_async` from the httpd task, WS close-frame handling, LRU purge behaviour.
- [ ] mDNS announce on STA and AP netifs; `scoreboard.local` resolution from Windows/Android.
- [ ] Wi-Fi fallback timing on real hardware (APSTA channel switching while STA retries).
- [ ] Stack sizes (httpd 10 KB, render 4 KB, wifi 4 KB, main 6 KB) under load; heap use.
- [ ] `manual.html` in real browsers (written and checked by reading only).
- [ ] Real display driver: still a stub.

## Contract concerns
- `ControlHub::on_open` silently ignores connections beyond the pending cap and nothing times them
  out (they are not in the pending list), so such a socket stays open until the client closes it or
  httpd LRU-purges it (`lru_purge_enable=true`). Suggest the hub emit `bad_message`/close for
  rejected opens, or expose a bool return from `on_open`.
- `ControlHub` takes the token by value at construction, so token changes need a reboot (matches
  "applied on reboot"; noted for completeness).
- `BoardConfig.manual_enabled` / stale timeout are fixed at boot from config (same reason).
