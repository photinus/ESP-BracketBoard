"""Fake ESP32 sign for end-to-end tests and manual agent testing.

``FakeESP`` serves WebSocket ``/ws`` plus ``POST /state`` / ``GET /status`` on one port and forwards
everything to the real C++ core through the ``bb_core_cli`` stdio bridge (see ``bridge.py``).
"""
from .bridge import BridgeError, CoreBridge, PurePythonBridge, find_core_cli
from .server import FakeESP

__all__ = ["BridgeError", "CoreBridge", "FakeESP", "PurePythonBridge", "find_core_cli"]
