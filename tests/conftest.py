"""Global test isolation guards."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def forbid_unmocked_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail tests that attempt any unmocked socket or DNS operation."""

    attempted: list[str] = []

    def blocked(operation: str):
        def fail(*_args: object, **_kwargs: object) -> None:
            attempted.append(operation)
            raise OSError("Network access is disabled in tests; mock the network boundary.")

        return fail

    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        monkeypatch.setattr(socket.socket, name, blocked(f"socket.socket.{name}"))
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, blocked(f"socket.{name}"))

    yield

    assert not attempted, "Unmocked network access attempted during test via: " + ", ".join(
        attempted
    )
