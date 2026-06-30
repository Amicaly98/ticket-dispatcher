"""测试 fixtures。"""
import pytest

from src.bus import InMemoryBus
from src.clock import FakeClock


@pytest.fixture
def fake_clock():
    return FakeClock(1_000_000.0)


@pytest.fixture
def mem_bus():
    return InMemoryBus()
