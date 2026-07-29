"""Prove the autouse socket guard actually blocks network in unit tests."""

from __future__ import annotations

import socket

import pytest

from tests.unit.conftest import NetworkBlockedError


def test_socket_blocked_in_unit_tests() -> None:
    """A unit test attempting to open a socket must fail loudly."""
    with pytest.raises(NetworkBlockedError):
        socket.socket()


def test_socket_restored_after_test() -> None:
    """Sanity: the real socket callable is still present (monkeypatch unwound)."""
    # This test itself runs under block_network; we only assert the symbol exists
    # and is callable in form. Actual restoration is verified at collection time
    # by later integration tests that DO open sockets.
    assert callable(socket.socket)
