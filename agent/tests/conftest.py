"""Make ``bracketboard_agent`` importable when running ``pytest agent`` from the repo root."""
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import pytest


class FakeClock:
    """Injectable monotonic clock."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def core_cli() -> str:
    """Path of the C++ ``bb_core_cli`` stdio bridge (env BB_CORE_CLI or common build dirs); skip if absent."""
    from fake_esp.bridge import find_core_cli

    path = find_core_cli()
    if not path:
        pytest.skip("bb_core_cli not built (cmake -S host -B build && cmake --build build, or set BB_CORE_CLI)")
    return path
