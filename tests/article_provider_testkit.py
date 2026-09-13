from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

PROVIDER_LIVE_ENV = {
    "Blizzard": "BLIZZARD_LIVE_TESTS",
    "CurseForge": "CURSEFORGE_LIVE_TESTS",
    "Icy Veins": "ICY_VEINS_LIVE_TESTS",
    "Method": "METHOD_LIVE_TESTS",
    "Warcraft Wiki": "WARCRAFT_WIKI_LIVE_TESTS",
    "WowProgress": "WOWPROGRESS_LIVE_TESTS",
}


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def require_live(provider_name: str) -> None:
    env_name = PROVIDER_LIVE_ENV.get(provider_name, "WOWHEAD_LIVE_TESTS")
    if not _env_enabled(env_name):
        pytest.skip(f"Set {env_name}=1 to run live {provider_name} tests.")


def invoke_live(runner: CliRunner, app: Any, args: list[str], *, provider_name: str, attempts: int = 3):
    last_result = None
    for attempt in range(1, attempts + 1):
        result = runner.invoke(app, args)
        if result.exit_code == 0:
            return result
        last_result = result
        if attempt < attempts:
            time.sleep(float(attempt))
    assert last_result is not None
    try:
        payload = json.loads(last_result.stderr or last_result.output)
    except json.JSONDecodeError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
    error_code = error.get("code") if isinstance(error.get("code"), str) else None
    if error_code == "blocked":
        # Blocking is a product outage, not a reason to go green: the clients impersonate a browser
        # on purpose, so a block means the transport needs work (see docs/wowprogress/README.md).
        pytest.fail(
            f"Live {provider_name} requests are blocked by upstream bot protection (error.code=blocked). "
            "This is a transport regression, not an environment problem; fix the client rather than skipping."
        )
    pytest.fail(
        f"Live {provider_name} command failed after {attempts} attempts.\n"
        f"args={args}\n"
        f"exit_code={last_result.exit_code}\n"
        f"output={last_result.output[:2000]}"
    )


def payload_for_live(runner: CliRunner, app: Any, args: list[str], *, provider_name: str) -> dict[str, Any]:
    result = invoke_live(runner, app, args, provider_name=provider_name)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"Command did not produce JSON.\nargs={args}\nstdout={result.stdout[:2000]}\n{exc}")
    assert payload.get("ok") is not False
    return payload


def error_payload(result: Any) -> dict[str, Any]:
    return json.loads(result.stderr or result.output)


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
