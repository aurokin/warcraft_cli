from __future__ import annotations

import json
from typing import Any

import httpx
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, request_with_retries

PROVIDER = "lorrgs"
API_HOST = "https://api2.lorrgs.io"
SITE_HOST = "https://lorrgs.io"
OPENAPI_URL = f"{API_HOST}/api/openapi.json"


class LorrgsClientError(RuntimeError):
    """Typed client error so the command layer can emit structured failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LorrgsClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        api_host: str = API_HOST,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._api_host = api_host.rstrip("/")
        self._http_client: httpx.Client | None = None

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> LorrgsClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self._timeout_seconds, follow_redirects=True)
        return self._http_client

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LorrgsClientError("invalid_response", "Lorrgs response was not valid JSON.") from exc

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._api_host}{path}"
        response = request_with_retries(
            self._client(),
            url,
            params=_clean_params(params),
            headers={"Accept": "application/json", "User-Agent": "warcraft-cli/lorrgs"},
            retry_attempts=self._retry_attempts,
        )
        payload = self._decode_json(response)
        return {"payload": payload, "source_url": str(response.request.url)}

    def roles(self) -> dict[str, Any]:
        return self._get("/api/roles")

    def classes(self) -> dict[str, Any]:
        return self._get("/api/classes")

    def specs(self) -> dict[str, Any]:
        return self._get("/api/specs")

    def spec(self, spec_slug: str) -> dict[str, Any]:
        return self._get(f"/api/specs/{spec_slug}")

    def spec_spells(self, spec_slug: str) -> dict[str, Any]:
        return self._get(f"/api/specs/{spec_slug}/spells")

    def zones(self) -> dict[str, Any]:
        return self._get("/api/zones")

    def season(self, season_slug: str = "current") -> dict[str, Any]:
        return self._get(f"/api/seasons/{season_slug}")

    def zone(self, zone_id: float) -> dict[str, Any]:
        return self._get(f"/api/zones/{zone_id:g}")

    def zone_bosses(self, zone_id: float) -> dict[str, Any]:
        return self._get(f"/api/zones/{zone_id:g}/bosses")

    def bosses(self) -> dict[str, Any]:
        return self._get("/api/bosses")

    def boss(self, boss_slug: str) -> dict[str, Any]:
        return self._get(f"/api/bosses/{boss_slug}")

    def boss_spells(self, boss_slug: str) -> dict[str, Any]:
        return self._get(f"/api/bosses/{boss_slug}/spells")

    def spell(self, spell_id: int) -> dict[str, Any]:
        return self._get(f"/api/spells/{spell_id}")

    def trinkets(self) -> dict[str, Any]:
        return self._get("/api/trinkets")

    def spec_ranking(
        self,
        *,
        spec_slug: str,
        boss_slug: str,
        difficulty: str = "mythic",
        metric: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"/api/spec_ranking/{spec_slug}/{boss_slug}",
            params={"difficulty": difficulty, "metric": metric},
        )

    def spec_ranking_info(
        self,
        *,
        spec_slug: str,
        boss_slug: str,
        difficulty: str = "mythic",
        metric: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"/api/spec_ranking/{spec_slug}/{boss_slug}/info",
            params={"difficulty": difficulty, "metric": metric},
        )

    def comp_ranking(
        self,
        *,
        boss_slug: str,
        limit: int = 20,
        roles: list[str] | None = None,
        specs: list[str] | None = None,
        killtime_min: int = 0,
        killtime_max: int = 0,
    ) -> dict[str, Any]:
        return self._get(
            f"/api/comp_ranking/{boss_slug}",
            params={
                "limit": limit,
                "role": roles or None,
                "spec": specs or None,
                "killtime_min": killtime_min or None,
                "killtime_max": killtime_max or None,
            },
        )

    def user_report(self, report_id: str) -> dict[str, Any]:
        return self._get(f"/api/user_reports/{report_id}")

    def report_overview(self, report_id: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._get(f"/api/user_reports/{report_id}/load_overview", params={"refresh": refresh})

    def user_report_fights(
        self,
        *,
        report_id: str,
        fight: str,
        player: str | None = None,
        data_type: str | None = None,
    ) -> dict[str, Any]:
        return self._get(f"/api/user_reports/{report_id}/fights", params={"fight": fight, "player": player, "type": data_type})


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    cleaned = {key: value for key, value in params.items() if value not in (None, "")}
    return cleaned or None
