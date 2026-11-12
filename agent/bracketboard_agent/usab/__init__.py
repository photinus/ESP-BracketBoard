"""USABracketing controller model: parsing, clock extrapolation, mapping to our snapshot, HTTP clients.

Everything here follows docs/usab-controller-protocol.md. Nothing in this package ever evaluates
text received from the controller (the matside server's ``getTimer`` is *not* JSON: it is parsed by
key extraction, see :mod:`.parse`).
"""
from .clock import age_ms, remaining_ms
from .client import AppServerClient, MatsideClient, UsabClientError
from .mapping import MappingConfig, StateMapper, classify_color
from .model import BoutInfo, ControllerTimer, EventInfo
from .parse import UsabParseError, parse_get_event, parse_get_timer, parse_update_timer_query

__all__ = [
    "AppServerClient",
    "BoutInfo",
    "ControllerTimer",
    "EventInfo",
    "MappingConfig",
    "MatsideClient",
    "StateMapper",
    "UsabClientError",
    "UsabParseError",
    "age_ms",
    "classify_color",
    "parse_get_event",
    "parse_get_timer",
    "parse_update_timer_query",
    "remaining_ms",
]
