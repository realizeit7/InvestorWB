from __future__ import annotations

from datetime import datetime, timezone

import pytest

from equity_monitor.app import memory_app
from equity_monitor.util import Clock

# 2026-09-30 18:00 America/New_York (after the close and the default 2h data lag)
FIXED_NOW = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return Clock(FIXED_NOW)


@pytest.fixture
def app(clock, tmp_path):
    return memory_app(clock=clock, home=tmp_path)
