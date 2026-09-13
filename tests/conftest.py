from __future__ import annotations

import os
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import curl_cffi.requests
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC_DIRS = (
    ROOT / "packages" / "warcraft-core" / "src",
    ROOT / "packages" / "warcraft-api" / "src",
    ROOT / "packages" / "warcraft-content" / "src",
    ROOT / "packages" / "warcraft-cli" / "src",
    ROOT / "packages" / "wowhead-cli" / "src",
    ROOT / "packages" / "method-cli" / "src",
    ROOT / "packages" / "icy-veins-cli" / "src",
    ROOT / "packages" / "raiderio-cli" / "src",
    ROOT / "packages" / "warcraft-wiki-cli" / "src",
    ROOT / "packages" / "wowprogress-cli" / "src",
    ROOT / "packages" / "simc-cli" / "src",
    ROOT / "packages" / "warcraftlogs-cli" / "src",
    ROOT / "packages" / "raidbots-cli" / "src",
    ROOT / "packages" / "blizzard-api-cli" / "src",
    ROOT / "packages" / "curseforge-cli" / "src",
    ROOT / "packages" / "lorrgs-cli" / "src",
)
LIVE_TEST_ENV_BY_FILE: dict[str, str | tuple[str, ...]] = {
    "test_blizzard_api_live.py": "BLIZZARD_LIVE_TESTS",
    "test_cooldown_packet_live.py": ("LORRGS_LIVE_TESTS", "WARCRAFTLOGS_LIVE_TESTS"),
    "test_curseforge_live.py": "CURSEFORGE_LIVE_TESTS",
    "test_icy_veins_live.py": "ICY_VEINS_LIVE_TESTS",
    "test_live_endpoint_contracts.py": "WOWHEAD_LIVE_TESTS",
    "test_live_integration.py": "WOWHEAD_LIVE_TESTS",
    "test_lorrgs_live.py": "LORRGS_LIVE_TESTS",
    "test_method_live.py": "METHOD_LIVE_TESTS",
    "test_raidbots_live.py": "RAIDBOTS_LIVE_TESTS",
    "test_raiderio_live.py": "RAIDERIO_LIVE_TESTS",
    "test_warcraft_wiki_live.py": "WARCRAFT_WIKI_LIVE_TESTS",
    "test_warcraft_wrapper_live.py": "WARCRAFT_WRAPPER_LIVE_TESTS",
    "test_warcraftlogs_live.py": "WARCRAFTLOGS_LIVE_TESTS",
    "test_live_command_matrix.py": "WARCRAFTLOGS_LIVE_TESTS",
    "test_wowhead_parser_canaries.py": "WOWHEAD_LIVE_TESTS",
    "test_wowprogress_live.py": "WOWPROGRESS_LIVE_TESTS",
}

TESTS_DIR = str(ROOT / "tests")
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

for package_src in reversed(PACKAGE_SRC_DIRS):
    path_str = str(package_src)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

# Every provider that has a file/redis cache (warcraft_api.cache.load_prefixed_cache_settings_from_env).
CACHE_ENV_PREFIXES = (
    "ICY_VEINS",
    "METHOD",
    "RAIDBOTS",
    "RAIDERIO",
    "WARCRAFT_WIKI",
    "WARCRAFTLOGS",
    "WOWHEAD",
    "WOWPROGRESS",
)


class NetworkGuardError(RuntimeError):
    """Raised when a test without the ``live`` marker attempts real network access."""


NETWORK_ATTEMPTS_KEY = pytest.StashKey[list[str]]()
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(address: object) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    return isinstance(host, str) and host in LOOPBACK_HOSTS


@pytest.fixture(autouse=True)
def disable_cache_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for prefix in CACHE_ENV_PREFIXES:
        monkeypatch.setenv(f"{prefix}_CACHE_BACKEND", "none")
    monkeypatch.setenv("WARCRAFT_HTTP_MIN_INTERVAL_SECONDS", "0")


@pytest.fixture(autouse=True)
def block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Block real network access for non-live tests.

    Attempts raise ``NetworkGuardError`` at the socket, httpx transport, and curl_cffi seams
    (loopback passes through). Attempts are also recorded so a test still fails at teardown
    when the code under test swallows the error (retry loops, ``except Exception``).
    """
    if request.node.get_closest_marker("live") is not None:
        yield
        return

    attempts: list[str] = []
    request.node.stash[NETWORK_ATTEMPTS_KEY] = attempts

    def blocked(target: str) -> NoReturn:
        attempts.append(target)
        raise NetworkGuardError(
            f"Non-live test attempted network access: {target}. "
            "Stub the client method or mark the test @pytest.mark.live."
        )

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if not _is_loopback(address):
            blocked(f"socket.connect {address!r}")
        real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_loopback(address):
            blocked(f"socket.connect_ex {address!r}")
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(host):
            blocked(f"socket.getaddrinfo {host!r}:{port!r}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    def guarded_handle_request(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        blocked(f"httpx {request.method} {request.url}")

    async def guarded_handle_async_request(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        blocked(f"httpx async {request.method} {request.url}")

    def guarded_curl_request(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        blocked(f"curl_cffi {method} {url}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    # Transport level, not Client.send, so httpx.MockTransport keeps working.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded_handle_request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_handle_async_request)
    # libcurl bypasses Python sockets, so wowprogress needs its own seam.
    monkeypatch.setattr(curl_cffi.requests.Session, "request", guarded_curl_request)
    yield
    if attempts:
        pytest.fail("Non-live test touched the network: " + "; ".join(attempts))


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _live_env_for_item(item: pytest.Item) -> tuple[str, ...]:
    file_name = Path(str(item.path)).name
    env_names = LIVE_TEST_ENV_BY_FILE.get(file_name, "WOWHEAD_LIVE_TESTS")
    return (env_names,) if isinstance(env_names, str) else env_names


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        if item.get_closest_marker("live") is None:
            continue
        env_names = _live_env_for_item(item)
        missing = [env_name for env_name in env_names if not _env_enabled(env_name)]
        if missing:
            requested = " and ".join(f"{env_name}=1" for env_name in missing)
            item.add_marker(pytest.mark.skip(reason=f"Set {requested} to run this live test."))
