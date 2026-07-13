"""Shared test fixtures."""

import sys
from pathlib import Path

import pytest

# Make `custom_components.history_mover` importable from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_db_url: str, enable_custom_integrations: None
) -> None:
    """Allow Home Assistant to load the integration in every test.

    ``recorder_db_url`` is listed first on purpose: it asserts that hass has not
    been set up yet, and ``enable_custom_integrations`` pulls in (and starts)
    hass. Resolving the recorder's database URL before that lets the
    recorder-based tests initialise their in-memory database in the right order.
    """
    return
