"""``provenance.cache``: every binary that builds a cache store reports its lookups the same way.

Each provider's command runs twice against a file cache in ``tmp_path``, with HTTP answered at the
transport seam so the shared store sits between the client and the canned response. The first run
must call itself a miss and the second a hit with an age.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
from typer.testing import CliRunner
from warcraft_cli.main import app as warcraft_app
from warcraft_cli.providers import PROVIDERS, get_provider

from tests.cli_testkit import FIXTURES, apply_provider_stubs

runner = CliRunner()


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


_METHOD_SITEMAP = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.method.gg/guides/mistweaver-monk</loc></url>
  <url><loc>https://www.method.gg/guides/windwalker-monk</loc></url>
</urlset>"""
_ICY_VEINS_SITEMAP = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.icy-veins.com/wow/frost-mage-pve-dps-guide</loc><lastmod>2026-09-01</lastmod></url>
</urlset>"""


def _wowhead(url: str) -> Any:
    return _fixture("wowhead/search_suggestions_thunderfury.json")


def _method(url: str) -> Any:
    return _METHOD_SITEMAP


def _icy_veins(url: str) -> Any:
    return _ICY_VEINS_SITEMAP if url.endswith("sitemap.xml") else (FIXTURES / "icy_veins/site_menu_class_hub.html").read_text(encoding="utf-8")


def _warcraft_wiki(url: str) -> Any:
    return _fixture("warcraft_wiki/search_world_boss_sha_of_anger.json")


def _raiderio(url: str) -> Any:
    return _fixture("raiderio/guild_profile_us_malganis_gn.json")


def _lorrgs(url: str) -> Any:
    return _fixture("lorrgs/specs.json")


def _warcraftlogs(url: str) -> Any:
    if url.endswith("/oauth/token"):
        return {"access_token": "fake-token", "expires_in": 3600}
    return {"data": {"worldData": {"regions": [{"id": 1, "compactName": "US", "name": "United States", "slug": "us"}]}}}


def _blizzard(url: str) -> Any:
    if url.endswith("/token"):
        return {"access_token": "fake-token", "expires_in": 3600, "token_type": "Bearer"}
    return _fixture("blizzard/item.json")


def _curseforge(url: str) -> Any:
    return _fixture("curseforge/changelog.json") if "/changelog" in url else _fixture("curseforge/mod.json")


def _raidbots(url: str) -> Any:
    return _fixture("simc/captured_arcane_mage_json2_report.json")


class CacheCase(NamedTuple):
    env_prefix: str
    argv: tuple[str, ...]
    answer: Callable[[str], Any]  # URL -> JSON-able body, or the text of a page
    env: tuple[tuple[str, str], ...] = ()


# One cheap command per provider that builds a cache store; simc builds none.
CACHE_CASES: dict[str, CacheCase] = {
    "wowhead": CacheCase("WOWHEAD", ("search", "thunderfury"), _wowhead),
    "method": CacheCase("METHOD", ("search", "monk"), _method),
    "icy-veins": CacheCase("ICY_VEINS", ("search", "frost mage"), _icy_veins),
    "warcraft-wiki": CacheCase("WARCRAFT_WIKI", ("search", "sha of anger"), _warcraft_wiki),
    "raiderio": CacheCase("RAIDERIO", ("guild", "us", "malganis", "gn"), _raiderio),
    "lorrgs": CacheCase("LORRGS", ("specs",), _lorrgs),
    "warcraftlogs": CacheCase(
        "WARCRAFTLOGS",
        ("regions",),
        _warcraftlogs,
        (("WARCRAFTLOGS_CLIENT_ID", "test-id"), ("WARCRAFTLOGS_CLIENT_SECRET", "test-secret")),
    ),
    "blizzard-api": CacheCase(
        "BLIZZARD",
        ("item", "19019"),
        _blizzard,
        (("BLIZZARD_CLIENT_ID", "test-id"), ("BLIZZARD_CLIENT_SECRET", "test-secret")),
    ),
    "curseforge": CacheCase("CURSEFORGE", ("addon", "3358"), _curseforge, (("CURSEFORGE_API_KEY", "test-key"),)),
    "raidbots": CacheCase("RAIDBOTS", ("inspect-report", "abc123XYZ"), _raidbots),
}


def _answer_http(monkeypatch: pytest.MonkeyPatch, answer: Callable[[str], Any]) -> list[str]:
    """Answer every request at the transport seam with ``answer(url)``; record the URLs requested."""
    urls: list[str] = []

    def handle_request(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        urls.append(url)
        body = answer(url)
        if isinstance(body, str):
            return httpx.Response(200, text=body, request=request)
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    return urls


def test_every_provider_with_a_cache_store_has_a_case() -> None:
    assert set(CACHE_CASES) == {registration.name for registration in PROVIDERS} - {"simc"}


@pytest.mark.parametrize("provider", sorted(CACHE_CASES))
def test_a_repeated_command_reports_a_miss_then_a_hit(provider: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    case = CACHE_CASES[provider]
    monkeypatch.setenv(f"{case.env_prefix}_CACHE_BACKEND", "file")
    monkeypatch.setenv(f"{case.env_prefix}_CACHE_DIR", str(tmp_path))
    for name, value in case.env:
        monkeypatch.setenv(name, value)
    urls = _answer_http(monkeypatch, case.answer)
    app = get_provider(provider).app

    first = runner.invoke(app, list(case.argv))
    assert first.exit_code == 0, first.output
    fetched = len(urls)
    second = runner.invoke(app, list(case.argv))
    assert second.exit_code == 0, second.output

    cold, warm = json.loads(first.stdout)["provenance"]["cache"], json.loads(second.stdout)["provenance"]["cache"]
    assert (cold["backend"], cold["hit"], cold["lookups"] > 0) == ("file", False, True)
    assert (warm["backend"], warm["hit"], warm["all_hits"]) == ("file", True, True)
    assert 0 <= warm["oldest_hit_age_seconds"] <= warm["oldest_hit_ttl_seconds"]
    # Token requests are not cached in the store, so only compare data requests.
    assert [url for url in urls[fetched:] if not url.endswith("/token")] == []


def _warm_wowhead_and_method(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """File caches for wowhead and method; every other provider stays offline and cacheless."""
    for name in PROVIDERS:
        if name.name not in {"wowhead", "method"}:
            apply_provider_stubs(name.name, monkeypatch)
    for prefix in ("WOWHEAD", "METHOD"):
        monkeypatch.setenv(f"{prefix}_CACHE_BACKEND", "file")
        monkeypatch.setenv(f"{prefix}_CACHE_DIR", str(tmp_path / prefix.lower()))
    _answer_http(monkeypatch, lambda url: _method(url) if "method.gg" in url else _wowhead(url))


def test_wrapper_search_reports_each_providers_cache_and_the_aggregate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _warm_wowhead_and_method(monkeypatch, tmp_path)
    first = json.loads(runner.invoke(warcraft_app, ["search", "monk"]).stdout)
    second = json.loads(runner.invoke(warcraft_app, ["search", "monk"]).stdout)

    blocks = {row["provider"]: row["payload"]["provenance"].get("cache") for row in second["data"]["providers"]}
    assert {name for name, block in blocks.items() if block is not None} == {"wowhead", "method"}
    assert all((blocks[name]["lookups"], blocks[name]["hits"]) == (1, 1) for name in ("wowhead", "method"))
    assert (first["provenance"]["cache"]["lookups"], first["provenance"]["cache"]["hits"]) == (2, 0)
    assert (second["provenance"]["cache"]["lookups"], second["provenance"]["cache"]["all_hits"]) == (2, True)


def test_fields_drops_the_cache_block_unless_it_is_requested(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _warm_wowhead_and_method(monkeypatch, tmp_path)
    app = get_provider("wowhead").app
    runner.invoke(app, ["search", "thunderfury"])

    requested = json.loads(runner.invoke(app, ["--fields", "provenance.cache.hit", "search", "thunderfury"]).stdout)
    unrequested = json.loads(runner.invoke(app, ["--fields", "data.count", "search", "thunderfury"]).stdout)

    assert requested == {"provenance": {"cache": {"hit": True}}}
    assert "provenance" not in unrequested
