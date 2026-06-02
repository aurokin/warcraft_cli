from __future__ import annotations

import json
from typing import Any

import httpx
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, request_with_retries

from curseforge_cli.auth import CurseForgeAuthConfig, load_curseforge_auth_config

# Public CurseForge Core API. The WoW game id is 1; the API key is a static per-app header.
API_HOST = "https://api.curseforge.com"
WOW_GAME_ID = 1

# Host, endpoints, and response shapes follow the documented public CurseForge Core API and are
# pending one-time live confirmation (run CURSEFORGE_LIVE_TESTS=1 with a CURSEFORGE_API_KEY). doctor
# and every command payload carry provenance.verified=false to keep that posture honest.
_VERIFICATION_NOTE = (
    "Host, endpoints, and response shapes follow the documented public CurseForge Core API and are "
    "pending one-time live confirmation (run CURSEFORGE_LIVE_TESTS=1 with a CURSEFORGE_API_KEY)."
)


class CurseForgeClientError(RuntimeError):
    """Typed client error so the command layer can emit a structured ok:false envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CurseForgeClient:
    def __init__(
        self,
        *,
        auth: CurseForgeAuthConfig | None = None,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        auth = auth if auth is not None else load_curseforge_auth_config()
        self._api_key = auth.api_key or ""
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._http_client: httpx.Client | None = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self._timeout_seconds, follow_redirects=True)
        return self._http_client

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
        self._require_key()
        url = f"{API_HOST}{path}"
        response = request_with_retries(
            self._client(),
            url,
            method="GET",
            params=params,
            headers=self._headers(),
            retry_attempts=self._retry_attempts,
        )
        return {"payload": self._decode_json(response), "source_url": str(response.request.url)}

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
        # The `slug` search param follows the documented CurseForge Core API but is pending live
        # confirmation. Rather than trust the server to filter, match the exact slug client-side: an
        # ignored or renamed filter param can then never bind the wrong mod under an `ok:true` envelope
        # (it degrades to addon_not_found instead).
        search = self._get("/v1/mods/search", params={"gameId": WOW_GAME_ID, "slug": text})
        rows = search["payload"].get("data")
        # A non-list `data` is a malformed/unexpected payload (schema drift, or an error wrapped in
        # `data`), distinct from a well-formed empty result set. Keep those two codes separate so
        # callers branching on the error code can tell an integration failure from a real miss.
        if not isinstance(rows, list):
            raise CurseForgeClientError("invalid_response", "CurseForge search response had no data list.")
        if not rows:
            raise CurseForgeClientError("addon_not_found", f"No CurseForge WoW addon matched slug {text!r}.")
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
            raise CurseForgeClientError("addon_not_found", f"No CurseForge WoW addon had the exact slug {text!r}.")
        mod_id = match.get("id")
        if not isinstance(mod_id, int):
            raise CurseForgeClientError("invalid_response", "CurseForge search result had no integer mod id.")
        return mod_id, "slug_search", search["source_url"]

    @staticmethod
    def _newest_file(latest_files: list[Any]) -> dict[str, Any] | None:
        candidates = [f for f in latest_files if isinstance(f, dict) and isinstance(f.get("id"), int)]
        if not candidates:
            return None
        # Newest by file date, then by id as a stable tiebreak. fileDate is an ISO-8601 string, which
        # sorts lexicographically in chronological order.
        return max(candidates, key=lambda f: (str(f.get("fileDate") or ""), f["id"]))

    def _fetch_latest_changelog(self, mod_id: int, latest_files: list[Any]) -> dict[str, Any] | None:
        newest = self._newest_file(latest_files)
        if newest is None:
            return None
        file_id = newest.get("id")
        if not isinstance(file_id, int):
            return None
        try:
            result = self._get(f"/v1/mods/{mod_id}/files/{file_id}/changelog")
        except (httpx.HTTPError, CurseForgeClientError) as exc:
            # Changelog is a best-effort enrichment: never fail the whole addon lookup over it, but make
            # the failure explicit (an `error` marker, not a silent null) so callers can distinguish a
            # failed fetch from "no files to fetch". Covers HTTPStatusError, a post-retry network
            # RequestError, and a malformed/non-JSON changelog body (CurseForgeClientError).
            return {"file_id": file_id, "error": _changelog_error(exc)}
        body = result["payload"].get("data")
        # Documented changelog `data` is an HTML string (or null/absent when a file has none). A
        # present-but-non-string `data` is schema drift, surfaced as an explicit marker rather than
        # silently flattened to a null body that looks like "no changelog".
        if body is not None and not isinstance(body, str):
            return {"file_id": file_id, "error": {"code": "invalid_response", "message": "changelog data was not a string."}}
        return {
            "file_id": file_id,
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
        # Numeric CurseForge ids are global across games; reject a mod that belongs to another game so a
        # non-WoW id can't be returned as a WoW addon while provenance claims gameId=1.
        game_id = metadata.get("gameId")
        if isinstance(game_id, int) and game_id != WOW_GAME_ID:
            raise CurseForgeClientError(
                "addon_not_found",
                f"CurseForge mod {mod_id} is not a World of Warcraft addon (gameId={game_id}).",
            )
        latest_files = metadata.get("latestFiles") if isinstance(metadata.get("latestFiles"), list) else []
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


def _changelog_error(exc: httpx.HTTPError | CurseForgeClientError) -> dict[str, str]:
    if isinstance(exc, CurseForgeClientError):
        return {"code": exc.code, "message": exc.message}
    if isinstance(exc, httpx.HTTPStatusError):
        return {"code": "http_error", "message": f"changelog request returned HTTP {exc.response.status_code}."}
    return {"code": "network_error", "message": f"changelog request failed: {exc}."}


def verification_note() -> str:
    return _VERIFICATION_NOTE
