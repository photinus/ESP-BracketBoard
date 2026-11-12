"""``cd agent && python -m fake_esp.matside --port 6099``: run a fake USABracketing matside server.

Serves ``/usab?function=...`` and the app server's ``/api/live_scoring/bout`` and drives a scripted demo
bout in a loop, so you can run the whole chain by hand::

    python -m fake_esp --token T --port 8080                       # fake sign (real C++ core)
    python -m fake_esp.matside --port 6099                         # fake controller
    python -m bracketboard_agent --adapter usabracketing --host 127.0.0.1 --port 8080 --token T
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

from .fake_matside import FakeMatside, run_demo


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fake_esp.matside", description="Fake USABracketing matside server with a demo bout.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6099)
    p.add_argument("--speed", type=float, default=1.0, help="demo speed multiplier (the clock still runs at 1x)")
    p.add_argument("--period-seconds", type=float, default=30.0, help="length of the demo periods (default 30)")
    p.add_argument("--no-demo", action="store_true", help="serve only; push state yourself with updateTimer requests")
    p.add_argument("--once", action="store_true", help="run the demo bout once instead of looping")
    args = p.parse_args(argv)
    try:
        fm = FakeMatside(args.host, args.port).start()
    except OSError as e:
        print(f"error: cannot listen on {args.host}:{args.port}: {e}", file=sys.stderr)
        return 2
    print(f"fake matside server on {fm.url}/usab (and {fm.url}/api/live_scoring/bout)", flush=True)
    stop = threading.Event()
    th = None
    if not args.no_demo:
        th = threading.Thread(
            target=run_demo, args=(fm, stop, args.speed, args.period_seconds, not args.once), daemon=True
        )
        th.start()
        print("demo bout running (Ctrl-C to stop)", flush=True)
    try:
        while True:
            time.sleep(0.5)
            if th is not None and not th.is_alive():
                th = None
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        fm.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
