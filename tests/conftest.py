from __future__ import annotations

import os
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

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
    ROOT / "packages" / "simc-cli" / "src",
    ROOT / "packages" / "warcraftlogs-cli" / "src",
    ROOT / "packages" / "raidbots-cli" / "src",
    ROOT / "packages" / "blizzard-api-cli" / "src",
    ROOT / "packages" / "curseforge-cli" / "src",
    ROOT / "packages" / "lorrgs-cli" / "src",
)

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
)
# Prefixes of every setting the binaries read from the environment: credentials, cache and Redis
# config, endpoint overrides, the SimC checkout and the worktree runtime roots.
PRODUCT_ENV_PREFIXES = (
    "BLIZZARD_",
    "CURSEFORGE_",
    "ICY_VEINS_",
    "LORRGS_",
    "METHOD_",
    "RAIDBOTS_",
    "RAIDERIO_",
    "SIMC_",
    "WARCRAFT_",
    "WARCRAFTLOGS_",
    "WOWHEAD_",
)
XDG_ENV_NAMES = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")
class NetworkGuardError(RuntimeError):
    """Raised when a test without the ``live`` marker attempts real network access."""


NETWORK_ATTEMPTS_KEY = pytest.StashKey[list[str]]()
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(address: object) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    return isinstance(host, str) and host in LOOPBACK_HOSTS


def _is_e2e(request: pytest.FixtureRequest) -> bool:
    return request.node.get_closest_marker("e2e") is not None


@pytest.fixture(autouse=True)
def hermetic_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep every non-e2e test off the developer's config, credentials, caches, state and SimC checkout.

    Product settings are cleared, HOME and the XDG roots point into a fresh per-test directory, and
    the working directory moves there too because the credential loaders read ``.env.local`` from it.
    """
    if _is_e2e(request):
        # End-to-end journeys run the binaries as subprocesses with their own isolated cache root
        # (tests/e2e/conftest.py) and use the developer's real provider credentials on purpose.
        return
    for name in list(os.environ):
        if name.startswith(PRODUCT_ENV_PREFIXES):
            monkeypatch.delenv(name)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for name in XDG_ENV_NAMES:
        monkeypatch.setenv(name, str(home / name.lower()))
    monkeypatch.chdir(home)
    for prefix in CACHE_ENV_PREFIXES:
        monkeypatch.setenv(f"{prefix}_CACHE_BACKEND", "none")
    monkeypatch.setenv("WARCRAFT_HTTP_MIN_INTERVAL_SECONDS", "0")


@pytest.fixture(autouse=True)
def block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Block real network access for non-live tests.

    Attempts raise ``NetworkGuardError`` at the socket and httpx transport seams
    (loopback passes through). Attempts are also recorded so a test still fails at teardown
    when the code under test swallows the error (retry loops, ``except Exception``).
    """
    if request.node.get_closest_marker("live") is not None or _is_e2e(request):
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

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    # Transport level, not Client.send, so httpx.MockTransport keeps working.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded_handle_request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_handle_async_request)
    yield
    if attempts:
        pytest.fail("Non-live test touched the network: " + "; ".join(attempts))


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        if item.get_closest_marker("e2e") is not None:
            if not _env_enabled("WARCRAFT_E2E"):
                item.add_marker(pytest.mark.skip(reason="Set WARCRAFT_E2E=1 (make test-e2e) to run end-to-end journeys."))
            continue
        # The only live-marked suite is the Wowhead parser canary (make test-canary).
        if item.get_closest_marker("live") is not None and not _env_enabled("WOWHEAD_LIVE_TESTS"):
            item.add_marker(pytest.mark.skip(reason="Set WOWHEAD_LIVE_TESTS=1 (make test-canary) to run the live canary."))
