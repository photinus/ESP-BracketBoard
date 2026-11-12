"""USABracketing source adapter (STUB).

Everything USABracketing-specific lives in this file. The sign (firmware) only knows the snapshot
schema in docs/protocol-v1.md, so this adapter's whole job is:

    USABracketing's live bout data  ->  one ``BoutState`` per ``read_state()`` call.

The USABracketing interface details have not been supplied yet, so every method raises
``NotImplementedError``. Fill in the TODOs below; nothing else in the agent needs to change.
"""
from __future__ import annotations

from ..adapter import SourceAdapter
from ..schema import BoutState


class USABracketingAdapter(SourceAdapter):
    """Adapter for the USABracketing scoreboard/timer.

    TODO(source): decide HOW to read USABracketing. Candidates, in order of preference:
      1. A documented/observable HTTP or WebSocket API of the USABracketing mat/timer page
         (look at the browser dev tools network tab while a bout is running: is there a JSON
         endpoint or socket the page itself polls/subscribes to?). Record the exact URL(s),
         auth/cookies/headers, request cadence and one sample response in this docstring.
      2. A local file or export the desktop software writes (path, format, update frequency).
      3. Scraping the DOM of the timer page with a headless browser (last resort: fragile).
    TODO(source): polling vs push.
      * Polling: implement ``read_state`` as a fetch; keep ``wait_change`` as the default sleep
        (the Publisher polls at ``--poll-hz`` anyway).
      * Push (WebSocket/SSE): in ``start`` open the stream and keep the latest parsed state in
        ``self._latest``; override ``wait_change`` to await an ``asyncio.Event`` that the reader
        sets on every message, so changes are published immediately instead of at the next poll.
    TODO(source): connection handling. ``read_state`` must return ``None`` (not raise) while the
      source is unreachable or no bout is loaded; the sign then goes stale on its own after ~3 s.
      Reconnect in the background; never block ``read_state`` for more than ~200 ms.

    Field mapping (fill the middle column from the real USABracketing payload)::

        BoutState field        USABracketing field / how to derive             notes
        ---------------------  ----------------------------------------------  ---------------------
        left_score             ??? (score of the competitor on the LEFT)        int 0..99
        right_score            ??? (score of the competitor on the RIGHT)       int 0..99
        left_warnings          ??? (stalling/caution count, left)               int 0..9, clamp
        right_warnings         ???                                              int 0..9, clamp
        period                 ??? (current period index)                       1..3 regulation,
                                                                                4+ overtime/tiebreak
        clock_ms_remaining     ??? (remaining time of the *period* clock)       ms, sampled NOW,
                                                                                0..3_600_000
        clock_state            ??? running / stopped / injury / expired         see below
        screen                 "score" normally; "splash" between bouts or      score | splash
                               when no bout is loaded / final result screen
        weight                 ??? (weight class label)                         <= 8 printable ASCII
        bout                   ??? (bout number)                                <= 8 printable ASCII
        colors_swapped         True when the competitor on the LEFT side wears  see "left/right and
                               the colour the sign gives to the right side      colours" below

    TODO(mapping) left/right and colours. ``left_*`` / ``right_*`` are SCREEN SIDES, in the order
      the operator wants them shown. The sign colours the left side red and the right side green
      by default. Decide which USABracketing competitor goes on which screen side, then set
      ``colors_swapped=True`` when the competitor on the LEFT side wears the colour the sign
      would give to the right side (i.e. the left competitor is the green one). ``colors_swapped``
      only swaps the side colours; it never moves scores. Verify against a real bout where
      colours/sides change mid-match, if the format has that.

    TODO(mapping) period labelling. ``period`` is an integer only. Map regulation periods to
      1..3 and every overtime/tiebreak/ride-out period to 4..9 in order. If USABracketing exposes a
      textual label ("SV-1", "TB-2", "UTB"), it cannot be sent in v1 (reserved optional field
      ``period_label``); just map it to the right integer.

    TODO(mapping) clock state. Map the source's timer states to:
        running  - clock is counting down
        stopped  - paused (whistle), between periods, or waiting to start
        injury   - an injury / blood time stoppage is active (clock_ms_remaining stays the *main*
                   period clock; the injury time itself is not carried in v1, ``injury_ms`` is a
                   reserved optional field)
        expired  - period/bout time reached 0 (the sign holds 0:00 until it sees this)

    TODO(clock) sampling. ``clock_ms_remaining`` must be sampled *immediately* before returning
      from ``read_state`` and be correct for that instant:
        - if the source gives a value + timestamp, extrapolate: ``value - (now - ts)`` while it is
          running (use a monotonic clock), never go below 0;
        - if the source only gives whole seconds, do not fake sub-second precision; the sign slews
          small errors but snaps anything > 250 ms off, so sample as often as possible.
      Do not use the sender wall clock for anything but ``sender_ts``.

    TODO(validation) ``read_state`` must return values inside the schema ranges (the Publisher
      validates and refuses to send invalid snapshots, logging the problem). Clamp scores/warnings
      and truncate ``weight``/``bout`` to 8 printable ASCII characters here.
    """

    def __init__(self, **options):
        # TODO(config): accept whatever is needed to reach USABracketing (base URL, mat/ring id,
        # credentials, poll interval, ...). Wire matching CLI flags in cli.py (--adapter
        # usabracketing) once the interface is known; extra options arrive as keyword arguments.
        self.options = options

    async def start(self) -> None:
        # TODO: create the HTTP session / open the socket / start the background reader task.
        raise NotImplementedError("USABracketingAdapter.start: see the TODOs in this module's docstring")

    async def stop(self) -> None:
        # TODO: cancel background tasks, close sessions/sockets. Must be safe if start() failed.
        raise NotImplementedError("USABracketingAdapter.stop: see the TODOs in this module's docstring")

    async def read_state(self) -> BoutState | None:
        # TODO: fetch (or take ``self._latest``), map every field per the table above, sample the
        # clock now, and return a BoutState. Return None if unavailable / no bout loaded.
        raise NotImplementedError("USABracketingAdapter.read_state: see the TODOs in this module's docstring")

    async def wait_change(self, timeout: float) -> None:
        # TODO (push sources only): await an asyncio.Event set by the reader, with ``timeout``.
        # Polling sources can drop this override and use the default sleep.
        raise NotImplementedError("USABracketingAdapter.wait_change: see the TODOs in this module's docstring")
