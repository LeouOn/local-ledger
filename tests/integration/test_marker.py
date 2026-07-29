"""Integration tests are deselected by default (pyproject addopts).

This file ensures the marker path collects; run with: pytest -m integration
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_integration_marker_collectable() -> None:
    """Placeholder proving the integration suite is discoverable on demand."""
    assert True
