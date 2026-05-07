"""Pytest configuration and shared fixtures for MusicFlow tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_open_in_explorer() -> None:
    """Mock subprocess.Popen to prevent opening Explorer windows during tests.

    This fixture is automatically applied to all tests, preventing the annoying
    behavior of mass-opening Explorer windows when running the test suite.
    """
    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        yield mock_popen
