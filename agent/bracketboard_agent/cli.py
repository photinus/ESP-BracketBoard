"""Command line.

    python -m bracketboard_agent --host scoreboard.local --token XXX --mock
    python -m bracketboard_agent --host scoreboard.local --token XXX --adapter usabracketing

Exit codes: 0 normal exit (Ctrl-C included), 2 usage/configuration error (no token, no source,
bad option value), 3 the selected adapter is not implemented.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import __version__
from .adapter import SourceAdapter
from .publisher import Publisher
from .transport import FailoverTransport, HttpTransport, Transport, WsTransport, websockets

log = logging.getLogger("bracketboard.cli")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bracketboard_agent",
        description="Push bout state snapshots to a BracketBoard ESP32 sign.",
    )
    p.add_argument("--host", default="scoreboard.local", help="sign host name or IP (default: scoreboard.local)")
    p.add_argument("--port", type=int, default=80, help="sign HTTP/WS port (default: 80)")
    p.add_argument("--token", default=None, help="sign token (or set BRACKETBOARD_TOKEN)")
    p.add_argument("--adapter", choices=("mock", "usabracketing"), default=None, help="bout source")
    p.add_argument("--mock", action="store_true", help="shorthand for --adapter mock")
    p.add_argument("--script", default=None, help="mock script JSON (default: bundled sample_bout.json)")
    p.add_argument("--speed", type=float, default=1.0, help="mock speed multiplier (default 1.0)")
    p.add_argument("--loop", action="store_true", help="mock: restart the script when it ends")
    p.add_argument("--exit-when-finished", action="store_true", help="mock: exit after the script ends")
    p.add_argument("--http-only", action="store_true", help="never use WebSocket, POST /state only")
    p.add_argument("--heartbeat", type=float, default=1.0, help="heartbeat period in seconds (default 1.0)")
    p.add_argument("--poll-hz", type=float, default=10.0, help="adapter poll rate (default 10)")
    p.add_argument("--max-hz", type=float, default=20.0, help="max snapshots per second (default 20)")
    g = p.add_argument_group("USABracketing source (--adapter usabracketing)")
    g.add_argument("--usab-host", default="127.0.0.1", help="matside server host (default: 127.0.0.1, the controller laptop)")
    g.add_argument("--usab-port", type=int, default=6099, help="matside server port (default: 6099)")
    g.add_argument(
        "--usab-offset-ms", type=float, default=0.0,
        help="clock offset in ms, subtracted from the controller-side age of a sample (default 0: agent runs on the "
        "controller laptop). Positive shortens the computed age, like the overlay's offset_millis",
    )
    g.add_argument("--swap-sides", action="store_true", help="flip the side rule (default: red wrestler on the left)")
    g.add_argument("--no-bout-lookup", action="store_true", help="do not fetch weight/bout number from the app server")
    p.add_argument("-v", "--verbose", action="count", default=0, help="more logging (-vv for debug)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _netloc(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 literal
    return host if port == 80 else f"{host}:{port}"


def build_transport(host: str, port: int, token: str, http_only: bool = False) -> Transport:
    netloc = _netloc(host, port)
    http = HttpTransport(f"http://{netloc}", token)
    if http_only:
        return http
    if websockets is None:
        return FailoverTransport(None, http)  # logs a warning
    return FailoverTransport(WsTransport(f"ws://{netloc}/ws", token), http)


class _UsageError(Exception):
    """Bad command line / configuration (exit code 2)."""


def build_adapter(args: argparse.Namespace) -> SourceAdapter:
    name = args.adapter or ("mock" if args.mock else None)
    if name is None:
        raise _UsageError("choose a source: --mock or --adapter usabracketing")
    if name == "mock":
        from .adapters.mock import MockBoutAdapter

        return MockBoutAdapter(args.script, speed=args.speed, loop=args.loop)
    from .adapters.usabracketing import USABracketingAdapter

    if not 0 < args.usab_port < 65536:
        raise _UsageError("--usab-port must be 1..65535")
    return USABracketingAdapter(
        args.usab_host,
        args.usab_port,
        offset_ms=args.usab_offset_ms,
        swap_sides=args.swap_sides,
        bout_lookup=not args.no_bout_lookup,
    )


async def _run(args: argparse.Namespace, token: str) -> int:
    adapter = build_adapter(args)
    transport = build_transport(args.host, args.port, token, args.http_only)
    publisher = Publisher(
        adapter, transport, heartbeat_s=args.heartbeat, poll_hz=args.poll_hz, max_hz=args.max_hz
    )
    log.info("agent %s session=%s -> %s:%d", __version__, publisher.session, args.host, args.port)

    watcher = None
    if args.exit_when_finished and hasattr(adapter, "finished"):

        async def watch():
            while not adapter.finished:
                await asyncio.sleep(0.1)
            await asyncio.sleep(2 * args.heartbeat)  # let the final screen go out
            publisher.stop()

        watcher = asyncio.ensure_future(watch())
    try:
        await publisher.run()
    finally:
        if watcher:
            watcher.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose == 1 else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.verbose == 0:
        # keep the important operational messages visible by default
        logging.getLogger("bracketboard").setLevel(logging.INFO)
    token = args.token or os.environ.get("BRACKETBOARD_TOKEN")
    if not token:
        print("error: no token: pass --token or set BRACKETBOARD_TOKEN", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args, token))
    except KeyboardInterrupt:
        return 0
    except _UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except NotImplementedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
