"""共用 pytest 夹具：离线 fixtures 加载。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsc2inv.lcsc_client import load_fixture
from lcsc2inv.lcsc_models import LCSCPart

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def capacitor() -> LCSCPart:
    """C28323 — Samsung MLCC 0805 1uF。"""
    return load_fixture(FIXTURE_DIR / "C28323.html")


@pytest.fixture(scope="session")
def optoisolator() -> LCSCPart:
    """C191386 — ORPC-817C-F 光耦。"""
    return load_fixture(FIXTURE_DIR / "C191386.html")


@pytest.fixture(scope="session")
def resistor() -> LCSCPart:
    """C25804 — 0603 10kΩ 电阻。"""
    return load_fixture(FIXTURE_DIR / "C25804.html")