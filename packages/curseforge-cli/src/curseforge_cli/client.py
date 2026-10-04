from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, CachedHttpClient, request_with_retries
from warcraft_core.exit_codes import error_code_for_http_status
from warcraft_core.paths import provider_cache_root

from curseforge_cli.auth import CurseForgeAuthConfig, load_curseforge_auth_config

# Public CurseForge Core API. The WoW game id is 1; the API key is a static per-app header.
API_HOST = "https://api.curseforge.com"
WOW_GAME_ID = 1

# The one statement of the verification posture: `curseforge --help` (and docs/reference/
# curseforge.md, generated from it), doctor's notes, and every payload's
# provenance.verification_note all print this, so they cannot contradict provenance.verified.
# Slug search needs an API key with search access; keys without it get an actionable auth_failed
# from _resolve_mod that points at the numeric-mod-id path.
_VERIFICATION_NOTE = (
    "Host, x-api-key auth, slug search, mod lookup, and file changelog are confirmed against live "
    "CurseForge traffic, so addon payloads report provenance.verified=true."
)


def load_curseforge_cache_settings_from_env() -> tuple[CacheSettings, int]:
    """Resolve cache settings plus the response TTL; CurseForge's API key is rate-limited."""
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="CURSEFORGE",
        # Resolved per call, not at import, so the cache root follows HOME/XDG as they are now.
        default_cache_dir=provider_cache_root("curseforge") / "http",
        default_redis_prefix="curseforge_cli",
        ttl_defaults=CacheTTLConfig(entity_response=3600),
        ttl_env_overrides={"entity_response": "CURSEFORGE_CACHE_TTL_SECONDS"},
    )
    return settings, settings.ttls.entity_response


class CurseForgeClientError(RuntimeError):
    """Typed client error so the command layer can emit a structured ok:false envelope."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class CurseForgeClient(CachedHttpClient):
    def __init__(
        self,
        *,
        auth: CurseForgeAuthConfig | None = None,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        auth = auth if auth is not None else load_curseforge_auth_config()
        self._api_key = auth.api_key or ""
        settings, ttl = load_curseforge_cache_settings_from_env()
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._ttl = ttl
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)

    def _require_key(self) -> None:
        if not self._api_key:
            raise CurseForgeClientError(
                "missing_api_key",
                "CurseForge commands need CURSEFORGE_API_KEY. Set it in .env.local, the provider env "
                "file, or the environment.",
            )

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "Accept": "application/json"}

    @staticmethod
    def _decode_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise CurseForgeClientError("invalid_response", "CurseForge response was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise CurseForgeClientError("invalid_response", "CurseForge response was not a JSON object.")
        return payload

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET one API path, replaying a cached answer first; the API key never reaches the cache key."""
        self._require_key()
        key = f"curseforge:{hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()}"
        cached = self._cache_store.get(key) if self._cache_store is not None else None
        if isinstance(cached, dict) and "payload" in cached:
            return cached
        url = f"{API_HOST}{path}"
        response = request_with_retries(
            self._client(),
            url,
            method="GET",
            params=params,
            headers=self._headers(),
            retry_attempts=self._retry_attempts,
        )
        result = {"payload": self._decode_json(response), "source_url": str(response.request.url)}
        if self._cache_store is not None:
            self._cache_store.set(key, result, ttl_seconds=self._ttl)
        return result

    @staticmethod
    def _data_object(payload: dict[str, Any], *, context: str) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CurseForgeClientError("invalid_response", f"CurseForge {context} response had no data object.")
        return data

    def _resolve_mod(self, addon: str) -> tuple[int, str, str | None]:
        """Return (mod_id, resolved_by, search_source_url). Numeric input is an id; else search by slug."""
        text = addon.strip()
        if text.isdigit():
            return int(text), "id", None
        # Rather than trust the server to filter, match the exact slug client-side: an ignored or
        # renamed filter param can then never bind the wrong mod under an `ok:true` envelope (it
        # degrades to addon_not_found instead).
        try:
            search = self._get("/v1/mods/search", params={"gameId": WOW_GAME_ID, "slug": text})
        except httpx.HTTPStatusError as exc:
            # Search is a separately scoped CurseForge capability: a key that reads /v1/mods fine can
            # still be rejected here. Say which endpoint refused and point at the path that works,
            # instead of a bare "HTTP 403" the caller cannot act on.
            if exc.response.status_code not in (401, 403):
                raise
            raise CurseForgeClientError(
                "auth_failed",
                f"CurseForge rejected the slug search endpoint {exc.request.url} with HTTP "
                f"{exc.response.status_code}: this API key has no search access. Look the addon up by "
                "its numeric mod id instead, e.g. `curseforge addon 3358` for deadly-boss-mods.",
            ) from exc
        rows = search["payload"].get("data")
        # A non-list `data` is a malformed/unexpected payload (schema drift, or an error wrapped in
        # `data`), distinct from a well-formed empty result set. Keep those two codes separate so
        # callers branching on the error code can tell an integration failure from a real miss.
        if not isinstance(rows, list):
            raise CurseForgeClientError("invalid_response", "CurseForge search response had no data list.")
        if not rows:
            raise self._slug_miss(text, f"No CurseForge WoW addon matched slug {text!r}.")
        # Require an exact slug AND a WoW gameId (when present): if the server ignored/broadened the
        # gameId filter and returned same-slug projects from multiple games, this skips the non-WoW
        # rows instead of binding the first slug match.
        match = next(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("slug", "")).lower() == text.lower()
                and (not isinstance(row.get("gameId"), int) or row.get("gameId") == WOW_GAME_ID)
            ),
            None,
        )
        if match is None:
            raise self._slug_miss(text, f"No CurseForge WoW addon had the exact slug {text!r}.")
        mod_id = match.get("id")
        if not isinstance(mod_id, int):
            raise CurseForgeClientError("invalid_response", "CurseForge search result had no integer mod id.")
        return mod_id, "slug_search", search["source_url"]

    def _slug_miss(self, text: str, message: str) -> CurseForgeClientError:
        """The ``addon_not_found`` for a slug, with the addons a free-text search finds for it (``weakauras`` -> ``weakauras-2``).

        The candidates are best effort: a failed search leaves them empty rather than hiding the miss.
        """
        try:
            # sortField 2 is popularity, so the addon people mean comes first.
            params = {"gameId": WOW_GAME_ID, "searchFilter": text, "sortField": 2, "sortOrder": "desc", "pageSize": 5}
            rows = self._get("/v1/mods/search", params=params)["payload"].get("data")
        except (httpx.HTTPError, CurseForgeClientError):
            rows = None
        candidates = [
            {"slug": row["slug"], "id": row.get("id"), "name": row.get("name")}
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and isinstance(row.get("slug"), str)
        ]
        if candidates:
            slugs = ", ".join(row["slug"] for row in candidates)
            message += f" Name matches, most popular first: {slugs} (retry with `curseforge addon <slug>`)."
        return CurseForgeClientError("addon_not_found", message, details={"candidates": candidates})

    def _fetch_latest_changelog(self, mod_id: int, latest_files: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The changelog of the newest file; ``latest_files`` is newest first."""
        newest = next((f for f in latest_files if isinstance(f.get("id"), int)), None)
        if newest is None:
            return None
        file_id = newest["id"]
        # The newest file can be an alpha or beta (releaseType 3 / 2), so say which file the notes cover.
        file_ref = {"file_id": file_id, "display_name": newest.get("displayName"), "release_type": newest.get("releaseType")}
        try:
            result = self._get(f"/v1/mods/{mod_id}/files/{file_id}/changelog")
        except (httpx.HTTPError, CurseForgeClientError) as exc:
            # Changelog is a best-effort enrichment: never fail the whole addon lookup over it, but make
            # the failure explicit (an `error` marker, not a silent null) so callers can distinguish a
            # failed fetch from "no files to fetch". Covers HTTPStatusError, a post-retry network
            # RequestError, and a malformed/non-JSON changelog body (CurseForgeClientError).
            return {**file_ref, "error": _changelog_error(exc)}
        body = result["payload"].get("data")
        # Documented changelog `data` is an HTML string (or null/absent when a file has none). A
        # present-but-non-string `data` is schema drift, surfaced as an explicit marker rather than
        # silently flattened to a null body that looks like "no changelog".
        if body is not None and not isinstance(body, str):
            return {**file_ref, "error": {"code": "invalid_response", "message": "changelog data was not a string."}}
        # A successful fetch always keeps the object form (file_id + source_url) even when the file
        # exposes no notes (`body: null`). Top-level `changelog is None` is reserved for "no file to
        # fetch"; an empty `body` means "checked this file, it has none" — kept distinct on purpose so a
        # successful-but-empty fetch is never conflated with "addon has no files", and so it stays
        # consistent with the error-marker form above (a failed fetch must not return *more* structure
        # than a successful one). Callers detect empty notes via `changelog.body`, not `changelog is None`.
        return {
            **file_ref,
            "source_url": result["source_url"],
            "body": body,
        }

    def fetch_addon(self, addon: str) -> dict[str, Any]:
        self._require_key()
        mod_id, resolved_by, search_url = self._resolve_mod(addon)
        try:
            mod_result = self._get(f"/v1/mods/{mod_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise CurseForgeClientError("addon_not_found", f"CurseForge has no WoW addon with mod id {mod_id}.") from exc
            raise
        metadata = self._data_object(mod_result["payload"], context="mod")
        # Numeric CurseForge ids are global across games, and provenance hardcodes game_id=1, so gameId
        # is the only signal that the resolved record is actually a WoW addon (the slug path also lands
        # here after re-fetching the full mod). An absent or non-int gameId means we cannot confirm WoW
        # — treat it as schema drift (invalid_response) rather than emit ok:true claiming game_id=1,
        # mirroring the non-string changelog body. A present int that isn't WoW is a real miss.
        game_id = metadata.get("gameId")
        if not isinstance(game_id, int):
            raise CurseForgeClientError(
                "invalid_response",
                f"CurseForge mod {mod_id} response had no integer gameId to confirm it is a WoW addon.",
            )
        if game_id != WOW_GAME_ID:
            raise CurseForgeClientError(
                "addon_not_found",
                f"CurseForge mod {mod_id} is not a World of Warcraft addon (gameId={game_id}).",
            )
        raw_files = metadata.get("latestFiles")
        # CurseForge lists latestFiles in no useful order (years-old betas first), so newest first by
        # file date, then id. fileDate is an ISO-8601 string, which sorts chronologically.
        latest_files = sorted(
            (f for f in raw_files if isinstance(f, dict)) if isinstance(raw_files, list) else (),
            key=lambda f: (str(f.get("fileDate") or ""), f["id"] if isinstance(f.get("id"), int) else 0),
            reverse=True,
        )
        changelog = self._fetch_latest_changelog(mod_id, latest_files)
        source_urls: dict[str, str] = {"mod": mod_result["source_url"]}
        if search_url is not None:
            source_urls["search"] = search_url
        if isinstance(changelog, dict) and "source_url" in changelog:
            source_urls["changelog"] = changelog["source_url"]
        slug = metadata.get("slug")
        return {
            "mod_id": mod_id,
            "slug": slug if isinstance(slug, str) else None,
            "resolved_by": resolved_by,
            "source_urls": source_urls,
            "data": {
                "metadata": metadata,
                "latest_files": latest_files,
                "changelog": changelog,
            },
        }


def transport_error_code(exc: httpx.HTTPError) -> str:
    """The ERROR_CONTRACT code for an httpx failure: the status mapping, ``timeout``, or ``network_error``."""
    if isinstance(exc, httpx.HTTPStatusError):
        return error_code_for_http_status(exc.response.status_code)
    return "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"


def _changelog_error(exc: httpx.HTTPError | CurseForgeClientError) -> dict[str, str]:
    if isinstance(exc, CurseForgeClientError):
        return {"code": exc.code, "message": exc.message}
    if isinstance(exc, httpx.HTTPStatusError):
        return {"code": transport_error_code(exc), "message": f"changelog request returned HTTP {exc.response.status_code}."}
    return {"code": transport_error_code(exc), "message": f"changelog request failed: {exc}."}


def verification_note() -> str:
    return _VERIFICATION_NOTE
