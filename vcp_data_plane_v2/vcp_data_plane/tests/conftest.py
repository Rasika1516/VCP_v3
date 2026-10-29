"""Pytest configuration and shared fixtures."""
from __future__ import annotations

import asyncio
import pytest


# Use asyncio mode = "auto" so all async test functions run without
# explicit @pytest.mark.asyncio in each file (requires pytest-asyncio >= 0.21)
# Configured in pyproject.toml under [tool.pytest.ini_options].


@pytest.fixture
def event_loop():
    """Override default event loop to use a fresh loop per test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
