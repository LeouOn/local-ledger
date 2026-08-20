"""Unit-test-only fixtures.

The autouse ``block_network`` fixture denies all outbound/inbound sockets for
unit tests. Unit tests must be hermetic: no Ollama, no cloud, no real HTTP. Any
test that genuinely needs a network call belongs under ``tests/integration`` and
must opt out explicitly.

``tiktoken`` is configured to use a vendored BPE cache under
``tests/fixtures/tiktoken_cache/`` (see ``TIKTOKEN_CACHE_DIR`` below) so the
token-cap tests stay hermetic and never trigger a CDN download.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

# Point tiktoken at the vendored BPE cache before any test imports a module
# that calls tiktoken.get_encoding(...). Without this, the first call would
# attempt to download from openaipublic.blob.core.windows.net and trip the
# block_network fixture.
_VENDORED_TIKTOKEN_CACHE = Path(__file__).resolve().parents[1] / "fixtures" / "tiktoken_cache"
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(_VENDORED_TIKTOKEN_CACHE))


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
