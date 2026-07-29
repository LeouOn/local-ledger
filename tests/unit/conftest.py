"""Unit-test-only fixtures.

The autouse ``block_network`` fixture denies all outbound/inbound sockets for
unit tests. Unit tests must be hermetic: no Ollama, no cloud, no real HTTP. Any
test that genuinely needs a network call belongs under ``tests/integration`` and
must opt out explicitly.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest


class NetworkBlockedError(RuntimeError):
    """Raised when a unit test attempts to use a socket."""


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real_socket = socket.socket

    def _blocked(*_args: object, **_kwargs: object) -> socket.socket:
        raise NetworkBlockedError(
            "unit tests must not open sockets; move to tests/integration or "
            "use a VCR cassette"
        )

    monkeypatch.setattr(socket, "socket", _blocked)
    yield
    monkeypatch.setattr(socket, "socket", real_socket)
