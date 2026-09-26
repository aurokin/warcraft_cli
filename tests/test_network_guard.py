"""Proves the conftest guards: the network guard blocks real access for non-live tests and passes
loopback through, and the hermetic environment keeps tests off the developer's own files."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
from warcraft_core.paths import cache_root, config_root, data_root, state_root

from tests.conftest import NETWORK_ATTEMPTS_KEY, PRODUCT_ENV_PREFIXES, NetworkGuardError


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


def _run_inner_suite(tmp_path: Path, test_source: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``test_source`` in a child pytest under a copy of this suite's conftest."""
    shutil.copy(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_inner.py").write_text(textwrap.dedent(test_source))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_inner.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_swallowed_network_attempt_still_fails_at_teardown(tmp_path: Path) -> None:
    result = _run_inner_suite(
        tmp_path,
        """
        import socket

        def test_swallows_the_guard_error():
            try:
                socket.getaddrinfo("example.invalid", 443)
            except Exception:
                pass
        """,
        dict(os.environ),
    )
    assert result.returncode == 1, result.stdout
    assert "1 passed, 1 error" in result.stdout
    assert "Non-live test touched the network: socket.getaddrinfo 'example.invalid'" in result.stdout


def test_product_env_set_by_the_outer_shell_is_cleared(tmp_path: Path) -> None:
    result = _run_inner_suite(
        tmp_path,
        """
        import os

        def test_simc_checkout_is_not_visible():
            assert "SIMC_REPO_ROOT" not in os.environ
        """,
        {**os.environ, "SIMC_REPO_ROOT": "/nonexistent"},
    )
    assert result.returncode == 0, result.stdout


def test_product_roots_and_working_directory_are_a_per_test_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    home = Path.home()
    assert home.is_relative_to(tmp_path_factory.getbasetemp())
    assert Path.cwd() == home
    for root in (config_root(), cache_root(), data_root(), state_root()):
        assert root.is_relative_to(home)
    leaked = [
        name
        for name in os.environ
        if name.startswith(PRODUCT_ENV_PREFIXES)
        and not name.endswith("_CACHE_BACKEND")
        and name != "WARCRAFT_HTTP_MIN_INTERVAL_SECONDS"
    ]
    assert leaked == []
