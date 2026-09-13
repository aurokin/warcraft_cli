from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any

import httpx
import pytest
from wowhead_cli.expansion_profiles import (
    ExpansionProfile,
    build_comment_replies_url,
    build_entity_url,
    build_search_suggestions_url,
    build_tooltip_url,
    list_profiles,
)
from wowhead_cli.page_parser import (
    extract_comments_dataset,
    extract_linked_entities_from_href,
    parse_page_meta_json,
    parse_page_metadata,
)

pytestmark = pytest.mark.live

LIVE_ENABLED = os.getenv("WOWHEAD_LIVE_TESTS", "").strip().lower() in {"1", "true", "yes", "on"}
QUERY = "thunderfury"
ENTITY_TYPE = "item"
ENTITY_ID = 19019
PROFILE_KEYS = tuple(profile.key for profile in list_profiles())
ENTITY_DISCOVERY_QUERIES: dict[str, str] = {
    "quest": "defias in dustwallow",
    "npc": "defias ringleader",
    "spell": "thunderfury",
}
# Wowhead's page meta reports which in-development environments currently have a build behind them.
# The flag names differ from our profile keys, and released environments never appear in the map.
DEV_ENV_ACTIVE_FLAGS: dict[str, str] = {"ptr": "ptr", "beta": "beta", "classic-ptr": "classicptr"}


def _require_live() -> None:
    if not LIVE_ENABLED:
        pytest.skip("Set WOWHEAD_LIVE_TESTS=1 to run live endpoint contract tests.")


def _http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
) -> Any:
    payload, _health = _http_get_json_with_health(url, params=params, attempts=attempts)
    return payload


def _http_get_json_with_health(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
) -> tuple[Any, dict[str, Any]]:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                response = client.get(url, params=params)
                latency_ms = (time.perf_counter() - started) * 1000
                response.raise_for_status()
                health = {
                    "status_code": response.status_code,
                    "latency_ms": round(latency_ms, 1),
                    "latency_bucket": "fast"
                    if latency_ms < 500
                    else "moderate"
                    if latency_ms < 2000
                    else "slow",
                }
                return response.json(), health
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(float(attempt))
    raise AssertionError(f"GET JSON failed for {url} params={params}: {last_exc}")


def _http_get_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                return response.text
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(float(attempt))
    raise AssertionError(f"GET text failed for {url} params={params}: {last_exc}")


def _http_get_status(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                return client.get(url, params=params).status_code
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(float(attempt))
    raise AssertionError(f"GET failed for {url} params={params}: {last_exc}")


def _profile_for(profile_key: str) -> ExpansionProfile:
    return next(profile for profile in list_profiles() if profile.key == profile_key)


@lru_cache(maxsize=1)
def _live_env_activity() -> dict[str, bool]:
    """Read Wowhead's live `dataEnv.active` map, which every page (including retail) carries."""
    retail = _profile_for("retail")
    html = _http_get_text(build_entity_url(retail, ENTITY_TYPE, ENTITY_ID))
    page_meta = parse_page_meta_json(html)
    assert isinstance(page_meta, dict)
    data_env = page_meta.get("dataEnv")
    assert isinstance(data_env, dict), f"page meta no longer exposes dataEnv: {page_meta.keys()}"
    active = data_env.get("active")
    assert isinstance(active, dict) and active, f"page meta no longer exposes dataEnv.active: {data_env}"
    return {str(name): bool(flag) for name, flag in active.items()}


def _profile_env_is_active(profile_key: str) -> bool:
    """True when the profile has a live dataset; only in-development environments can be inactive."""
    flag = DEV_ENV_ACTIVE_FLAGS.get(profile_key)
    if flag is None:
        return True
    active = _live_env_activity()
    assert flag in active, f"Wowhead stopped reporting activity for {profile_key!r}: {sorted(active)}"
    return active[flag]


def _discover_entity_id(profile_key: str, *, entity_type: str, query: str) -> int:
    profile = next(profile for profile in list_profiles() if profile.key == profile_key)
    search_url = build_search_suggestions_url(profile)
    payload = _http_get_json(search_url, params={"q": query})
    assert isinstance(payload, dict)
    results = payload.get("results")
    assert isinstance(results, list)
    for row in results:
        if not isinstance(row, dict):
            continue
        if row.get("typeName", "").lower() != entity_type:
            continue
        entity_id = row.get("id")
        if isinstance(entity_id, int):
            return entity_id
    raise AssertionError(
        f"Could not discover {entity_type!r} from query={query!r} "
        f"for profile={profile_key}. results={results}"
    )


@pytest.mark.parametrize("profile_key", PROFILE_KEYS)
def test_live_search_endpoint_contract(profile_key: str) -> None:
    _require_live()
    profile = next(profile for profile in list_profiles() if profile.key == profile_key)
    url = build_search_suggestions_url(profile)
    payload, health = _http_get_json_with_health(url, params={"q": QUERY})

    assert health["status_code"] == 200
    assert health["latency_bucket"] in {"fast", "moderate", "slow"}
    assert isinstance(payload, dict)
    assert payload.get("search") == QUERY
    results = payload.get("results")
    assert isinstance(results, list)
    assert len(results) > 0
    first = results[0]
    assert isinstance(first, dict)
    for key in ["id", "name", "type", "typeName"]:
        assert key in first


@pytest.mark.parametrize("profile_key", PROFILE_KEYS)
def test_live_tooltip_endpoint_contract(profile_key: str) -> None:
    _require_live()
    profile = _profile_for(profile_key)
    url = build_tooltip_url(profile, ENTITY_TYPE, ENTITY_ID)
    if not _profile_env_is_active(profile_key):
        # Between builds Wowhead reports the environment inactive and holds no dataset for it.
        # The tooltip endpoint must say so rather than serve retail data under the inactive env's URL.
        status = _http_get_status(url, params={"dataEnv": profile.data_env})
        assert status == 404, f"inactive {profile_key} tooltip returned HTTP {status}, expected 404 for {url}"
        return

    payload = _http_get_json(url, params={"dataEnv": profile.data_env})

    assert isinstance(payload, dict)
    assert isinstance(payload.get("name"), str)
    assert isinstance(payload.get("tooltip"), str)


@pytest.mark.parametrize(
    ("entity_type", "query"),
    [
        ("quest", ENTITY_DISCOVERY_QUERIES["quest"]),
        ("npc", ENTITY_DISCOVERY_QUERIES["npc"]),
        ("spell", ENTITY_DISCOVERY_QUERIES["spell"]),
    ],
)
def test_live_tooltip_endpoint_contract_retail_discovered_entity_types(entity_type: str, query: str) -> None:
    _require_live()
    profile_key = "retail"
    profile = next(profile for profile in list_profiles() if profile.key == profile_key)
    entity_id = _discover_entity_id(profile_key, entity_type=entity_type, query=query)
    url = build_tooltip_url(profile, entity_type, entity_id)
    payload = _http_get_json(url, params={"dataEnv": profile.data_env})

    assert isinstance(payload, dict)
    assert isinstance(payload.get("name"), str)
    assert payload.get("tooltip") is not None


@pytest.mark.parametrize("profile_key", PROFILE_KEYS)
def test_live_entity_page_parser_contract(profile_key: str) -> None:
    _require_live()
    profile = _profile_for(profile_key)
    url = build_entity_url(profile, ENTITY_TYPE, ENTITY_ID)
    html = _http_get_text(url)

    meta = parse_page_metadata(html, fallback_url=url)
    assert isinstance(meta.get("canonical_url"), str)
    assert meta["canonical_url"].startswith("https://www.wowhead.com/")
    assert isinstance(meta.get("title"), str)

    page_meta = parse_page_meta_json(html)
    assert isinstance(page_meta, dict)
    data_env = page_meta.get("dataEnv")
    assert isinstance(data_env, dict)
    # An inactive in-development environment has no dataset of its own, so Wowhead still serves the
    # page under the profile's path but from retail data. Everything else on the page must still parse.
    expected_env = profile.data_env if _profile_env_is_active(profile_key) else _profile_for("retail").data_env
    assert data_env.get("env") == expected_env

    linked = extract_linked_entities_from_href(html, source_url=meta["canonical_url"])
    assert len(linked) > 0

    comments = extract_comments_dataset(html)
    assert isinstance(comments, list)
    if comments:
        assert isinstance(comments[0].get("id"), int)


@pytest.mark.parametrize(
    ("entity_type", "query"),
    [
        ("quest", ENTITY_DISCOVERY_QUERIES["quest"]),
        ("npc", ENTITY_DISCOVERY_QUERIES["npc"]),
        ("spell", ENTITY_DISCOVERY_QUERIES["spell"]),
    ],
)
def test_live_entity_page_parser_contract_retail_discovered_entity_types(entity_type: str, query: str) -> None:
    _require_live()
    profile_key = "retail"
    profile = next(profile for profile in list_profiles() if profile.key == profile_key)
    entity_id = _discover_entity_id(profile_key, entity_type=entity_type, query=query)
    url = build_entity_url(profile, entity_type, entity_id)
    html = _http_get_text(url)

    meta = parse_page_metadata(html, fallback_url=url)
    assert isinstance(meta.get("canonical_url"), str)
    assert meta["canonical_url"].startswith(f"{profile.wowhead_base}/{entity_type}={entity_id}")

    linked = extract_linked_entities_from_href(html, source_url=meta["canonical_url"])
    assert len(linked) > 0

    comments = extract_comments_dataset(html)
    assert isinstance(comments, list)
    if comments:
        assert isinstance(comments[0].get("id"), int)


@pytest.mark.parametrize("profile_key", PROFILE_KEYS)
def test_live_comment_reply_endpoint_contract(profile_key: str) -> None:
    _require_live()
    profile = next(profile for profile in list_profiles() if profile.key == profile_key)
    page_url = build_entity_url(profile, ENTITY_TYPE, ENTITY_ID)
    html = _http_get_text(page_url)
    comments = extract_comments_dataset(html)

    # Pick any known comment id; endpoint should always return a JSON list.
    comment_id = comments[0].get("id")
    assert isinstance(comment_id, int)

    reply_url = build_comment_replies_url(profile)
    payload = _http_get_json(reply_url, params={"id": comment_id})
    assert isinstance(payload, list)
    if payload:
        assert isinstance(payload[0], dict)
