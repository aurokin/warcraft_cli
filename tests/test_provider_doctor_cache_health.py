"""Every provider doctor pings a Redis cache backend and reports a broken one as degraded."""

from __future__ import annotations

from typing import Any

import pytest
from blizzard_api_cli.provider import PROVIDER as BLIZZARD
from curseforge_cli.provider import PROVIDER as CURSEFORGE
from icy_veins_cli.provider import PROVIDER as ICY_VEINS
from lorrgs_cli.provider import PROVIDER as LORRGS
from method_cli.provider import PROVIDER as METHOD
from raidbots_cli.provider import PROVIDER as RAIDBOTS
from raiderio_cli.provider import PROVIDER as RAIDERIO
from warcraft_core.provider import ProviderSurface
from warcraft_wiki_cli.provider import PROVIDER as WARCRAFT_WIKI
from warcraftlogs_cli.provider import PROVIDER as WARCRAFTLOGS

# Synthetic credentials, so a provider that needs them is not degraded for that reason instead.
CASES = [
    (BLIZZARD, "BLIZZARD", {"BLIZZARD_CLIENT_ID": "id", "BLIZZARD_CLIENT_SECRET": "secret"}),
    (CURSEFORGE, "CURSEFORGE", {"CURSEFORGE_API_KEY": "key"}),
    (ICY_VEINS, "ICY_VEINS", {}),
    (LORRGS, "LORRGS", {}),
    (METHOD, "METHOD", {}),
    (RAIDBOTS, "RAIDBOTS", {}),
    (RAIDERIO, "RAIDERIO", {}),
    (WARCRAFT_WIKI, "WARCRAFT_WIKI", {}),
    (WARCRAFTLOGS, "WARCRAFTLOGS", {"WARCRAFTLOGS_CLIENT_ID": "id", "WARCRAFTLOGS_CLIENT_SECRET": "secret"}),
]


class _Redis:
    def __init__(self, error: Exception | None) -> None:
        self._error = error

    def ping(self) -> bool:
        if self._error is not None:
            raise self._error
        return True


def _doctor_data(monkeypatch: pytest.MonkeyPatch, provider: ProviderSurface, error: Exception | None) -> dict[str, Any]:
    monkeypatch.setattr("warcraft_api.cache._build_redis_client", lambda url, **kwargs: _Redis(error))
    return provider.doctor(live=False)["data"]


@pytest.mark.parametrize(("provider", "prefix", "env"), CASES, ids=[prefix for _p, prefix, _e in CASES])
def test_doctor_reports_an_unreachable_redis_as_degraded(
    monkeypatch: pytest.MonkeyPatch, provider: ProviderSurface, prefix: str, env: dict[str, str]
) -> None:
    monkeypatch.setenv(f"{prefix}_CACHE_BACKEND", "redis")
    monkeypatch.setenv(f"{prefix}_REDIS_URL", "redis://127.0.0.1:1/0")
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    healthy = _doctor_data(monkeypatch, provider, None)
    broken = _doctor_data(monkeypatch, provider, ConnectionError("Error 61 connecting to 127.0.0.1:1."))

    assert (healthy["status"], healthy["cache"]["available"], healthy["cache"]["error"]) == ("ready", True, None)
    assert broken["status"] == "degraded"
    assert (broken["cache"]["available"], broken["cache"]["error"]) == (False, "Error 61 connecting to 127.0.0.1:1.")
