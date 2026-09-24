from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from icy_veins_cli.client import load_icy_veins_cache_settings_from_env
from method_cli.client import load_method_cache_settings_from_env
from warcraft_api.cache import CacheTTLConfig, load_prefixed_cache_settings_from_env
from warcraft_api.http import request_with_retries, retry_after_seconds


def test_load_prefixed_cache_settings_from_env_builds_provider_specific_settings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TESTAPP_CACHE_BACKEND", "redis")
    monkeypatch.setenv("TESTAPP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TESTAPP_REDIS_URL", "redis://cache.example:6379/9")
    monkeypatch.setenv("TESTAPP_REDIS_PREFIX", "test_app")
    monkeypatch.setenv("TESTAPP_SEARCH_TTL", "1200")
    monkeypatch.setenv("TESTAPP_PAGE_TTL", "2400")

    settings = load_prefixed_cache_settings_from_env(
        env_prefix="TESTAPP",
        default_cache_dir=tmp_path / "default",
        default_redis_prefix="test_default",
        ttl_defaults=CacheTTLConfig(search_suggestions=900, page_html=3600),
        ttl_env_overrides={
            "search_suggestions": "TESTAPP_SEARCH_TTL",
            "page_html": "TESTAPP_PAGE_TTL",
        },
    )

    assert settings.enabled is True
    assert settings.backend == "redis"
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.redis_url == "redis://cache.example:6379/9"
    assert settings.prefix == "test_app"
    assert settings.ttls.search_suggestions == 1200
    assert settings.ttls.page_html == 2400


def test_redis_backend_without_a_url_names_the_providers_own_variables(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("METHOD_CACHE_BACKEND", "redis")

    with pytest.raises(ValueError, match=r"^METHOD_REDIS_URL is required when METHOD_CACHE_BACKEND=redis\.$"):
        load_method_cache_settings_from_env()


def test_method_and_icy_veins_cache_loaders_use_shared_prefix_loader(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("METHOD_CACHE_DIR", str(tmp_path / "method"))
    monkeypatch.setenv("METHOD_SITEMAP_CACHE_TTL_SECONDS", "7200")
    monkeypatch.setenv("METHOD_PAGE_CACHE_TTL_SECONDS", "1800")
    monkeypatch.setenv("ICY_VEINS_CACHE_DIR", str(tmp_path / "icy"))
    monkeypatch.setenv("ICY_VEINS_SITEMAP_CACHE_TTL_SECONDS", "5400")
    monkeypatch.setenv("ICY_VEINS_PAGE_CACHE_TTL_SECONDS", "900")

    method_settings, method_sitemap_ttl, method_page_ttl = load_method_cache_settings_from_env()
    icy_settings, icy_sitemap_ttl, icy_page_ttl = load_icy_veins_cache_settings_from_env()

    assert method_settings.cache_dir == tmp_path / "method"
    assert method_settings.ttls.search_suggestions == 7200
    assert method_settings.ttls.page_html == 1800
    assert method_sitemap_ttl == 7200
    assert method_page_ttl == 1800

    assert icy_settings.cache_dir == tmp_path / "icy"
    assert icy_settings.ttls.search_suggestions == 5400
    assert icy_settings.ttls.page_html == 900
    assert icy_sitemap_ttl == 5400
    assert icy_page_ttl == 900


def test_retry_after_seconds_parses_numeric_header() -> None:
    response = httpx.Response(
        429,
        headers={"Retry-After": "1.5"},
        request=httpx.Request("GET", "https://example.invalid"),
    )

    assert retry_after_seconds(response) == 1.5


def test_request_with_retries_honors_retry_after_header(monkeypatch) -> None:
    sleep_calls: list[float] = []

    class FakeClient:
        def __init__(self) -> None:
            self._responses = [
                httpx.Response(
                    429,
                    headers={"Retry-After": "2"},
                    request=httpx.Request("GET", "https://example.invalid"),
                ),
                httpx.Response(
                    200,
                    text="ok",
                    request=httpx.Request("GET", "https://example.invalid"),
                ),
            ]

        def get(self, url: str, params=None):  # noqa: ANN001
            return self._responses.pop(0)

    monkeypatch.setattr("warcraft_api.http.time.sleep", sleep_calls.append)
    response = request_with_retries(FakeClient(), "https://example.invalid", retry_attempts=2)

    assert response.status_code == 200
    assert response.text == "ok"
    assert sleep_calls == [2.0]


def test_request_with_retries_falls_back_to_backoff_without_retry_after(monkeypatch) -> None:
    sleep_calls: list[float] = []

    class FakeClient:
        def __init__(self) -> None:
            self._responses = [
                httpx.Response(
                    503,
                    request=httpx.Request("GET", "https://example.invalid"),
                ),
                httpx.Response(
                    200,
                    text="ok",
                    request=httpx.Request("GET", "https://example.invalid"),
                ),
            ]

        def get(self, url: str, params=None):  # noqa: ANN001
            return self._responses.pop(0)

    monkeypatch.setattr("warcraft_api.http.backoff_seconds", lambda attempt: 0.75)
    monkeypatch.setattr("warcraft_api.http.time.sleep", sleep_calls.append)
    response = request_with_retries(FakeClient(), "https://example.invalid", retry_attempts=2)

    assert response.status_code == 200
    assert sleep_calls == [0.75]


def test_request_with_retries_supports_post_requests() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def post(self, url: str, params=None, **kwargs):  # noqa: ANN001
            self.calls.append((url, {"params": params, **kwargs}))
            return httpx.Response(
                200,
                text="ok",
                request=httpx.Request("POST", url),
            )

    client = FakeClient()
    response = request_with_retries(
        client,
        "https://example.invalid/token",
        method="POST",
        data={"grant_type": "client_credentials"},
        retry_attempts=1,
    )

    assert response.status_code == 200
    assert client.calls == [
        (
            "https://example.invalid/token",
            {
                "params": None,
                "data": {"grant_type": "client_credentials"},
            },
        )
    ]
