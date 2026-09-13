"""Proves the conftest network guard blocks real access for non-live tests and passes loopback through."""

from __future__ import annotations

import socket

import curl_cffi.requests
import httpx
import pytest

from tests.conftest import NETWORK_ATTEMPTS_KEY, NetworkGuardError


@pytest.fixture
def clear_attempts(request: pytest.FixtureRequest):
    yield
    # These tests deliberately trigger the guard; clear the record so teardown does not fail them.
    request.node.stash[NETWORK_ATTEMPTS_KEY].clear()


@pytest.mark.usefixtures("clear_attempts")
def test_httpx_request_is_blocked() -> None:
    with pytest.raises(NetworkGuardError, match="httpx GET https://example.invalid/"):
        httpx.get("https://example.invalid/")


@pytest.mark.usefixtures("clear_attempts")
def test_socket_connect_is_blocked() -> None:
    with pytest.raises(NetworkGuardError, match="example.invalid"):
        socket.create_connection(("example.invalid", 80), timeout=1)


@pytest.mark.usefixtures("clear_attempts")
def test_getaddrinfo_is_blocked() -> None:
    with pytest.raises(NetworkGuardError, match="getaddrinfo"):
        socket.getaddrinfo("example.invalid", 443)


@pytest.mark.usefixtures("clear_attempts")
def test_curl_cffi_request_is_blocked() -> None:
    with pytest.raises(NetworkGuardError, match="curl_cffi GET https://example.invalid/"):
        curl_cffi.requests.Session().get("https://example.invalid/")


def test_guard_records_attempts(request: pytest.FixtureRequest) -> None:
    with pytest.raises(NetworkGuardError):
        httpx.get("https://example.invalid/")
    attempts = request.node.stash[NETWORK_ATTEMPTS_KEY]
    assert attempts == ["httpx GET https://example.invalid/"]
    attempts.clear()


def test_loopback_connect_passes_through() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    with pytest.raises(OSError) as exc_info:
        socket.create_connection(("127.0.0.1", closed_port), timeout=1)
    assert not isinstance(exc_info.value, NetworkGuardError)


def test_httpx_mock_transport_passes_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.get("https://example.invalid/").json() == {"ok": True}
