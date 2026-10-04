from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlencode

import httpx
from warcraft_api.cache import build_cache_store, load_cache_settings_from_env
from warcraft_api.http import CachedHttpClient, hashed_cache_key, json_cache_key, request_with_retries

from wowhead_cli.classic_talents import compact_talent_data, is_compact_talent_data
from wowhead_cli.entity_types import suggestion_entity_type_from_type_id
from wowhead_cli.expansion_profiles import (
    ExpansionProfile,
    build_blue_tracker_url,
    build_comment_replies_url,
    build_entity_url,
    build_guide_category_url,
    build_guide_lookup_url,
    build_news_url,
    build_search_suggestions_url,
    build_search_url,
    build_tooltip_url,
    resolve_expansion,
)

WOWHEAD_BASE_URL = "https://www.wowhead.com"
# Bump when the cached entity payload changes shape, so older entries stop being served.
ENTITY_RESPONSE_CACHE_VERSION = 4
# A talent data file URL pins its version (dv/db), so its trimmed copy keeps for a month.
TALENT_CALC_DATA_TTL_SECONDS = 30 * 24 * 3600
# Bump when compact_talent_data changes shape, so older trimmed copies stop being served.
TALENT_CALC_DATA_CACHE_VERSION = 1


class WowheadClient(CachedHttpClient):
    def __init__(self, *, expansion: str | ExpansionProfile | None = None) -> None:
        self._http_client: httpx.Client | None = None
        cache_settings = load_cache_settings_from_env()
        self._cache_enabled = cache_settings.enabled
        self._cache_ttls = cache_settings.ttls
        self._cache_store = build_cache_store(cache_settings) if self._cache_enabled else None
        self._session_json_cache: dict[str, Any] = {}
        self._session_text_cache: dict[str, str] = {}
        self.expansion = expansion if isinstance(expansion, ExpansionProfile) else resolve_expansion(expansion)

    def __del__(self) -> None:
        self.close()

    def _request_with_retries(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return request_with_retries(self._client(), url, params=params)

    def _cache_key(self, namespace: str, url: str, params: dict[str, Any] | None) -> str:
        encoded = urlencode(sorted(params.items()), doseq=True) if params else ""
        raw = f"{namespace}|{url}|{encoded}".encode()
        return hashed_cache_key(namespace, raw)

    def _entity_response_cache_key(
        self,
        *,
        requested_type: str,
        requested_id: int,
        data_env: int | None,
        include_comments: bool,
        include_all_comments: bool,
        linked_entity_preview_limit: int,
    ) -> str:
        return json_cache_key(
            "entity_response",
            {
                "v": ENTITY_RESPONSE_CACHE_VERSION,
                "expansion": self.expansion.key,
                "type": requested_type,
                "id": requested_id,
                "data_env": data_env,
                "include_comments": include_comments,
                "include_all_comments": include_all_comments,
                "linked_entity_preview_limit": linked_entity_preview_limit,
            },
        )

    def _read_cache(self, key: str) -> Any | None:
        if not self._cache_enabled:
            return None
        return super()._read_cache(key)

    def _write_cache(self, key: str, payload: Any, *, ttl_seconds: int) -> None:
        if not self._cache_enabled:
            return
        super()._write_cache(key, payload, ttl_seconds=ttl_seconds)

    def get_cached_entity_response(
        self,
        *,
        requested_type: str,
        requested_id: int,
        data_env: int | None,
        include_comments: bool,
        include_all_comments: bool,
        linked_entity_preview_limit: int,
    ) -> dict[str, Any] | None:
        key = self._entity_response_cache_key(
            requested_type=requested_type,
            requested_id=requested_id,
            data_env=data_env,
            include_comments=include_comments,
            include_all_comments=include_all_comments,
            linked_entity_preview_limit=linked_entity_preview_limit,
        )
        cached = self._read_cache(key)
        return cached if isinstance(cached, dict) else None

    def set_cached_entity_response(
        self,
        payload: dict[str, Any],
        *,
        requested_type: str,
        requested_id: int,
        data_env: int | None,
        include_comments: bool,
        include_all_comments: bool,
        linked_entity_preview_limit: int,
    ) -> None:
        key = self._entity_response_cache_key(
            requested_type=requested_type,
            requested_id=requested_id,
            data_env=data_env,
            include_comments=include_comments,
            include_all_comments=include_all_comments,
            linked_entity_preview_limit=linked_entity_preview_limit,
        )
        self._write_cache(key, payload, ttl_seconds=self._cache_ttls.entity_response)

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        cache_ttl_seconds: int | None = None,
        cache_namespace: str = "json",
    ) -> Any:
        session_key = self._cache_key(cache_namespace, url, params)
        if session_key in self._session_json_cache:
            return copy.deepcopy(self._session_json_cache[session_key])

        ttl_seconds = cache_ttl_seconds or 0
        cache_key = session_key if ttl_seconds > 0 else None
        if cache_key is not None:
            cached = self._read_cache(cache_key)
            if cached is not None:
                self._session_json_cache[session_key] = cached
                return copy.deepcopy(cached)

        response = self._request_with_retries(url, params=params)
        payload = response.json()

        self._session_json_cache[session_key] = payload
        if cache_key is not None:
            self._write_cache(cache_key, payload, ttl_seconds=ttl_seconds)
        return copy.deepcopy(payload)

    def _get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        cache_ttl_seconds: int | None = None,
        cache_namespace: str = "text",
    ) -> str:
        session_key = self._cache_key(cache_namespace, url, params)
        if session_key in self._session_text_cache:
            return self._session_text_cache[session_key]

        ttl_seconds = cache_ttl_seconds or 0
        cache_key = session_key if ttl_seconds > 0 else None
        if cache_key is not None:
            cached = self._read_cache(cache_key)
            if isinstance(cached, str):
                self._session_text_cache[session_key] = cached
                return cached

        response = self._request_with_retries(url, params=params)
        payload = response.text

        self._session_text_cache[session_key] = payload
        if cache_key is not None:
            self._write_cache(cache_key, payload, ttl_seconds=ttl_seconds)
        return payload

    def search_suggestions(self, query: str) -> dict[str, Any]:
        url = build_search_suggestions_url(self.expansion)
        payload = self._get_json(
            url,
            params={"q": query},
            cache_ttl_seconds=self._cache_ttls.search_suggestions,
            cache_namespace="search_suggestions",
        )
        if isinstance(payload, dict):
            return payload
        raise ValueError("Unexpected response shape for search endpoint.")

    def tooltip(
        self,
        entity_type: str,
        entity_id: int,
        *,
        data_env: int | None = None,
    ) -> dict[str, Any]:
        payload, _ = self.tooltip_with_metadata(entity_type, entity_id, data_env=data_env)
        return payload

    def tooltip_with_metadata(
        self,
        entity_type: str,
        entity_id: int,
        *,
        data_env: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        url = build_tooltip_url(self.expansion, entity_type, entity_id)
        params = {"dataEnv": data_env or self.expansion.data_env}
        cache_key = self._cache_key("tooltip_meta", url, params)
        cached = self._read_cache(cache_key)
        if isinstance(cached, dict):
            payload = cached.get("payload")
            final_url = cached.get("final_url")
            if isinstance(payload, dict) and isinstance(final_url, str):
                return payload, final_url

        response = self._request_with_retries(url, params=params)
        payload = response.json()
        final_url = str(response.url)

        self._write_cache(
            cache_key,
            {"payload": payload, "final_url": final_url},
            ttl_seconds=self._cache_ttls.tooltip_meta,
        )
        if isinstance(payload, dict):
            return payload, final_url
        raise ValueError("Unexpected response shape for tooltip endpoint.")

    def entity_page_html(self, entity_type: str, entity_id: int) -> str:
        return self._get_text(
            entity_url(entity_type, entity_id, expansion=self.expansion),
            cache_ttl_seconds=self._cache_ttls.entity_page_html,
            cache_namespace="entity_page_html",
        )

    def guide_page_html(self, guide_id: int) -> str:
        return self._get_text(
            guide_url(guide_id, expansion=self.expansion),
            cache_ttl_seconds=self._cache_ttls.guide_page_html,
            cache_namespace="guide_page_html",
        )

    def page_html(self, page_url: str) -> str:
        return self._get_text(
            page_url,
            cache_ttl_seconds=self._cache_ttls.page_html,
            cache_namespace="page_html",
        )

    def talent_calc_data(self, data_url: str) -> dict[str, Any]:
        """A classic calculator's talent data file, trimmed to what the build decoder reads.

        The file is about 1.3 MB; only the trimmed trees, tier lists and talent names are cached.
        """
        key = self._cache_key("talent_calc_data", f"v{TALENT_CALC_DATA_CACHE_VERSION}|{data_url}", None)
        cached = self._session_json_cache.get(key)
        if cached is None:
            cached = self._read_cache(key)
        if isinstance(cached, dict) and is_compact_talent_data(cached):
            self._session_json_cache[key] = cached
            return cached
        data = compact_talent_data(self._request_with_retries(data_url).text)
        self._session_json_cache[key] = data
        self._write_cache(key, data, ttl_seconds=TALENT_CALC_DATA_TTL_SECONDS)
        return data

    def comment_replies(self, comment_id: int) -> list[dict[str, Any]]:
        url = build_comment_replies_url(self.expansion)
        payload = self._get_json(
            url,
            params={"id": comment_id},
            cache_ttl_seconds=self._cache_ttls.comment_replies,
            cache_namespace="comment_replies",
        )
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        return []

    def news_page_html(self, *, page: int = 1) -> str:
        return self._get_text(
            build_news_url(self.expansion, page=page),
            cache_ttl_seconds=self._cache_ttls.page_html,
            cache_namespace="page_html",
        )

    def blue_tracker_page_html(self, *, page: int = 1) -> str:
        return self._get_text(
            build_blue_tracker_url(self.expansion, page=page),
            cache_ttl_seconds=self._cache_ttls.page_html,
            cache_namespace="page_html",
        )

    def guide_category_page_html(self, category: str) -> str:
        return self._get_text(
            build_guide_category_url(self.expansion, category),
            cache_ttl_seconds=self._cache_ttls.page_html,
            cache_namespace="page_html",
        )


def suggestion_entity_type(result: dict[str, Any]) -> str | None:
    type_id = result.get("type")
    if not isinstance(type_id, int):
        return None
    return suggestion_entity_type_from_type_id(type_id)


def entity_url(
    entity_type: str,
    entity_id: int,
    expansion: str | ExpansionProfile | None = None,
) -> str:
    profile = expansion if isinstance(expansion, ExpansionProfile) else resolve_expansion(expansion)
    return build_entity_url(profile, entity_type, entity_id)


def guide_url(guide_id: int, expansion: str | ExpansionProfile | None = None) -> str:
    profile = expansion if isinstance(expansion, ExpansionProfile) else resolve_expansion(expansion)
    return build_guide_lookup_url(profile, guide_id)


def search_url(query: str, expansion: str | ExpansionProfile | None = None) -> str:
    profile = expansion if isinstance(expansion, ExpansionProfile) else resolve_expansion(expansion)
    return build_search_url(profile, query)


def news_url(*, page: int = 1, expansion: str | ExpansionProfile | None = None) -> str:
    profile = expansion if isinstance(expansion, ExpansionProfile) else resolve_expansion(expansion)
    return build_news_url(profile, page=page)


def blue_tracker_url(*, page: int = 1, expansion: str | ExpansionProfile | None = None) -> str:
    profile = expansion if isinstance(expansion, ExpansionProfile) else resolve_expansion(expansion)
    return build_blue_tracker_url(profile, page=page)


def guide_category_url(category: str, expansion: str | ExpansionProfile | None = None) -> str:
    profile = expansion if isinstance(expansion, ExpansionProfile) else resolve_expansion(expansion)
    return build_guide_category_url(profile, category)


def tool_url(path: str, expansion: str | ExpansionProfile | None = None) -> str:
    profile = expansion if isinstance(expansion, ExpansionProfile) else resolve_expansion(expansion)
    normalized = path.strip().lstrip("/")
    return f"{profile.wowhead_base}/{normalized}"
