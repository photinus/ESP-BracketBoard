"""``cd agent && python -m fake_esp --token T --port 8080 [--pure-python]``

Runs a fake sign (WS ``/ws`` + ``POST /state`` + ``GET /status`` on one port) and prints one line per
accepted state message. Point the agent at it with ``--host 127.0.0.1 --port 8080 --token T``.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from .server import FakeESP


def _fmt_clock(ms: int) -> str:
    s = -(-ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fake_esp", description="Fake BracketBoard sign backed by the real core.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--token", required=True)
    p.add_argument("--stale-ms", type=int, default=3000)
    p.add_argument("--manual", action="store_true", help="enable manual mode (needs the C++ core)")
    p.add_argument("--pure-python", action="store_true", help="no bb_core_cli subprocess (approximate)")
    p.add_argument("--core-cli", default=None, help="path to bb_core_cli (default: $BB_CORE_CLI or build dirs)")
    args = p.parse_args(argv)

    def on_accept(via: str, m: dict) -> None:
        print(
            f"{time.strftime('%H:%M:%S')} {via:4s} accepted session={m['session']} seq={m['seq']} "
            f"{m['left_score']}-{m['right_score']} warn {m['left_warnings']}/{m['right_warnings']} "
            f"P{m['period']} {_fmt_clock(m['clock_ms_remaining'])} {m['clock_state']} screen={m['screen']} "
            f"weight={m['weight']!r} bout={m['bout']!r}",
            flush=True,
        )

    async def run() -> None:
        esp = FakeESP(
            args.host, args.port, args.token, args.stale_ms, args.pure_python, args.core_cli,
            manual=args.manual, on_accept=on_accept,
        )
        async with esp:
            mode = "pure-python" if args.pure_python else "bb_core_cli"
            print(f"fake sign ({mode}) listening on {esp.ws_url} and {esp.http_url}/state", flush=True)
            while True:
                await asyncio.sleep(3600)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
