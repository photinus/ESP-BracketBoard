# ESP-BracketBoard

An ESP32 wrestling scoreboard sign driven by [USABracketing](https://usabracketing.com)'s timer/score state.

```
USABracketing --(adapter)--> Python agent --WebSocket /ws (primary) | HTTP POST /state--> ESP32 sign
 (source of truth)           full, idempotent JSON snapshots                              (display only)
```

The sign is a **display endpoint**. In agent mode it never decides scores, periods, or clock start/stop; it
interpolates the running clock locally between snapshots, and freezes it with the colon off if updates stop.
On-device manual scoring exists as an opt-in fallback (off by default).

| Path | What |
|---|---|
| `docs/protocol-v1.md` | Normative snapshot schema, seq/restart rules, WS/HTTP transport, clock model |
| `docs/architecture.md` | Boundaries, threading, manual-mode gating, stdio test bridge, agent contract |
| `docs/schema/`, `docs/fixtures/` | JSON Schema + golden valid/invalid messages shared by C++ and Python tests |
| `core/` | Portable C++17 logic (parser, seq filter, clock model, hub, board, manual mode). Host-tested |
| `host/` | Host build: unit tests (doctest), console display, `bb_core_cli` stdio bridge |
| `firmware/` | ESP-IDF 5.x glue (Wi-Fi STA→AP fallback, mDNS, WS + HTTP server, NVS config, display stub) |
| `agent/` | Python reference agent: mock bout, USABracketing adapter **stub**, fake sign for testing |

## Try it without hardware

```bash
cmake -S host -B build && cmake --build build -j && ctest --test-dir build
pip install -e 'agent[test]'
BB_CORE_CLI=$PWD/build/bb_core_cli python -m pytest agent -q     # includes end-to-end tests
```

Run a fake sign and point the mock agent at it:

```bash
cd agent && python -m fake_esp --token secret --port 8080
```

```bash
cd agent && python -m bracketboard_agent --host 127.0.0.1 --port 8080 --token secret --mock --speed 4
```

## Status / what's left

- **Verified here:** all core logic (C++ host tests) and the agent, including end-to-end runs of the real
  agent against the real C++ core over real sockets.
- **Not verified:** `firmware/` has not been compiled with ESP-IDF or run on a board (see
  `firmware/REVIEW_CHECKLIST.md`).
- **Stubs awaiting your input:** `agent/bracketboard_agent/adapters/usabracketing.py` (needs USABracketing's
  interface details and sample payloads) and `firmware/main/panel_display.cpp` (needs the display hardware).
