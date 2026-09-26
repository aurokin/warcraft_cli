from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def load_fixture_text(fixture_dir: Path, name: str) -> str:
    return (fixture_dir / name).read_text(encoding="utf-8")


# Wowhead synthetic routing fixtures: hand-written payloads keyed by expansion profile that prove
# URL routing and output shape, not parser resilience against real markup.
WOWHEAD_SYNTHETIC_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "expansion_synthetic.json"
WOWHEAD_SYNTHETIC_FIXTURE: dict[str, Any] = json.loads(WOWHEAD_SYNTHETIC_FIXTURE_PATH.read_text(encoding="utf-8"))


def make_entity_html(canonical_url: str, link_href: str, comment: dict[str, Any]) -> str:
    comments_payload = json.dumps([comment], separators=(",", ":"))
    return (
        "<html><head>"
        '<meta property="og:title" content="Thunderfury, Blessed Blade of the Windseeker">'
        '<meta name="description" content="Synthetic fixture page">'
        f'<link rel="canonical" href="{canonical_url}">'
        "</head><body>"
        f'<a href="{link_href}">Baron Geddon</a>'
        f"<script>var lv_comments0 = {comments_payload};</script>"
        "</body></html>"
    )


def install_wowhead_synthetic_transport(monkeypatch: pytest.MonkeyPatch, expansion_key: str) -> None:
    """Stub ``WowheadClient._request_with_retries`` so every fetch path (including
    ``tooltip_with_metadata``, which bypasses ``_get_json``) resolves offline."""
    from copy import deepcopy

    import httpx
    from wowhead_cli.expansion_profiles import (
        build_comment_replies_url,
        build_entity_url,
        build_search_suggestions_url,
        build_tooltip_url,
        resolve_expansion,
    )

    fixture = WOWHEAD_SYNTHETIC_FIXTURE
    profile = resolve_expansion(expansion_key)
    profile_data = fixture["profiles"][expansion_key]

    json_routes: dict[str, tuple[dict[str, Any], Any]] = {
        build_search_suggestions_url(profile): (
            {"q": fixture["query"]},
            {"search": fixture["query"], "results": [fixture["search_result"]]},
        ),
        build_tooltip_url(profile, "item", 19019): ({"dataEnv": profile.data_env}, fixture["tooltip"]),
        build_comment_replies_url(profile): ({"id": fixture["comment"]["id"]}, fixture["reply_thread"]),
    }
    page_url = build_entity_url(profile, "item", 19019)
    page_html = make_entity_html(
        canonical_url=profile_data["canonical_url"],
        link_href=profile_data["link_href"],
        comment=fixture["comment"],
    )

    def fake_request(self: Any, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        params = params or {}
        request = httpx.Request("GET", url, params=params)
        if url in json_routes:
            expected_params, payload = json_routes[url]
            assert params == expected_params, f"{expansion_key}: unexpected params for {url}: {params}"
            return httpx.Response(200, json=deepcopy(payload), request=request)
        if url == page_url:
            assert not params
            return httpx.Response(200, text=page_html, request=request)
        raise AssertionError(f"Unexpected request for {expansion_key}: url={url} params={params}")

    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient._request_with_retries", fake_request)
