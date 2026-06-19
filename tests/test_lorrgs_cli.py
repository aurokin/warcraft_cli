from __future__ import annotations

import json

import httpx
from lorrgs_cli.main import app
from typer.testing import CliRunner
from warcraft_cli.main import app as warcraft_app

runner = CliRunner()


class FakeLorrgsClient:
    calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self) -> FakeLorrgsClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self.calls.append(("close", {}))

    def specs(self) -> dict[str, object]:
        self.calls.append(("specs", {}))
        return {
            "payload": {"specs": [{"full_name_slug": "mage-frost", "role": "rdps"}]},
            "source_url": "https://api2.lorrgs.io/api/specs",
        }

    def bosses(self) -> dict[str, object]:
        self.calls.append(("bosses", {}))
        return {
            "payload": {
                "bosses": [
                    {
                        "name": "Chimaerus",
                        "full_name": "Chimaerus the Undreamt God",
                        "full_name_slug": "chimaerus-the-undreamt-god",
                    },
                    {
                        "name": "Lura",
                        "full_name": "Loom'ithar",
                        "full_name_slug": "lura",
                    },
                ]
            },
            "source_url": "https://api2.lorrgs.io/api/bosses",
        }

    def season(self, season_slug: str = "current") -> dict[str, object]:
        self.calls.append(("season", {"season_slug": season_slug}))
        return {
            "payload": {"name": "Midnight Season 1", "slug": "midnight_s1", "raids": [46.1, 46.2, 46.3, 50]},
            "source_url": f"https://api2.lorrgs.io/api/seasons/{season_slug}",
        }

    def spec_ranking_info(
        self,
        *,
        spec_slug: str,
        boss_slug: str,
        difficulty: str = "mythic",
        metric: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "spec_ranking_info",
                {
                    "spec_slug": spec_slug,
                    "boss_slug": boss_slug,
                    "difficulty": difficulty,
                    "metric": metric,
                },
            )
        )
        return {
            "payload": {
                "spec_slug": spec_slug,
                "boss_slug": boss_slug,
                "difficulty": difficulty,
                "metric": metric or "dps",
                "reports": [],
            },
            "source_url": f"https://api2.lorrgs.io/api/spec_ranking/{spec_slug}/{boss_slug}/info",
        }

    def comp_ranking(
        self,
        *,
        boss_slug: str,
        limit: int = 20,
        roles: list[str] | None = None,
        specs: list[str] | None = None,
        killtime_min: int = 0,
        killtime_max: int = 0,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "comp_ranking",
                {
                    "boss_slug": boss_slug,
                    "limit": limit,
                    "roles": roles or [],
                    "specs": specs or [],
                    "killtime_min": killtime_min,
                    "killtime_max": killtime_max,
                },
            )
        )
        return {
            "payload": {"reports": []},
            "source_url": f"https://api2.lorrgs.io/api/comp_ranking/{boss_slug}?limit={limit}",
        }

    def report_overview(self, report_id: str, *, refresh: bool = False) -> dict[str, object]:
        self.calls.append(("report_overview", {"report_id": report_id, "refresh": refresh}))
        return {
            "payload": {
                "report_id": report_id,
                "title": "Jun 18 Lura rekill? kek",
                "fights": [{"fight_id": 22, "boss": {"boss_slug": "lura"}, "kill": True}],
            },
            "source_url": f"https://api2.lorrgs.io/api/user_reports/{report_id}/load_overview",
        }

    def user_report_fights(
        self,
        *,
        report_id: str,
        fight: str,
        player: str | None = None,
        data_type: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("user_report_fights", {"report_id": report_id, "fight": fight, "player": player, "data_type": data_type}))
        return {
            "payload": {"fights": [{"fight_id": int(fight), "players": []}]},
            "source_url": f"https://api2.lorrgs.io/api/user_reports/{report_id}/fights?fight={fight}",
        }

    def boss(self, boss_slug: str) -> dict[str, object]:
        self.calls.append(("boss", {"boss_slug": boss_slug}))
        request = httpx.Request("GET", f"https://api2.lorrgs.io/api/bosses/{boss_slug}")
        response = httpx.Response(404, json={"detail": "Invalid Boss."}, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)


def _patch_client(monkeypatch) -> None:
    FakeLorrgsClient.calls = []
    monkeypatch.setattr("lorrgs_cli.main.LorrgsClient", FakeLorrgsClient)


def test_doctor_reports_lorrgs_capabilities() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "lorrgs"
    assert payload["status"] == "partial"
    assert payload["auth"]["required"] is False
    assert payload["capabilities"]["spec_ranking"] == "ready"
    assert payload["capabilities"]["comp_ranking"] == "ready"
    assert payload["capabilities"]["search"] == "ready"
    assert payload["capabilities"]["resolve"] == "ready"
    assert payload["capabilities"]["report_overview"] == "ready"
    assert payload["capabilities"]["current_season"] == "ready"


def test_specs_emits_standard_success_envelope(monkeypatch) -> None:
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["specs"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "lorrgs"
    assert payload["kind"] == "specs"
    assert payload["provenance"]["source"] == "lorrgs_public_api"
    assert payload["data"]["specs"][0]["full_name_slug"] == "mage-frost"
    assert FakeLorrgsClient.calls[-1] == ("close", {})


def test_spec_ranking_info_passes_query(monkeypatch) -> None:
    _patch_client(monkeypatch)
    result = runner.invoke(
        app,
        [
            "spec-ranking-info",
            "mage-frost",
            "chimaerus-the-undreamt-god",
            "--difficulty",
            "heroic",
            "--metric",
            "dps",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"] == {
        "spec_slug": "mage-frost",
        "boss_slug": "chimaerus-the-undreamt-god",
        "difficulty": "heroic",
        "metric": "dps",
    }
    assert ("spec_ranking_info", payload["query"]) in FakeLorrgsClient.calls


def test_comp_ranking_repeatable_filters(monkeypatch) -> None:
    _patch_client(monkeypatch)
    result = runner.invoke(
        app,
        [
            "comp-ranking",
            "chimaerus-the-undreamt-god",
            "--limit",
            "12",
            "--role",
            "heal>=4",
            "--spec",
            "mage-frost>=1",
            "--killtime-min",
            "120",
            "--killtime-max",
            "180",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["roles"] == ["heal>=4"]
    assert payload["query"]["specs"] == ["mage-frost>=1"]
    assert (
        "comp_ranking",
        {
            "boss_slug": "chimaerus-the-undreamt-god",
            "limit": 12,
            "roles": ["heal>=4"],
            "specs": ["mage-frost>=1"],
            "killtime_min": 120,
            "killtime_max": 180,
        },
    ) in FakeLorrgsClient.calls


def test_http_404_is_structured_not_found(monkeypatch) -> None:
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["boss", "not-a-boss"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["provider"] == "lorrgs"
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Invalid Boss."


def test_search_matches_spec_and_boss(monkeypatch) -> None:
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["search", "frost mage chimaerus"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "lorrgs"
    assert payload["count"] >= 1
    assert payload["results"][0]["kind"] == "spec_ranking"
    assert payload["results"][0]["spec_slug"] == "mage-frost"
    assert payload["results"][0]["boss_slug"] == "chimaerus-the-undreamt-god"
    assert payload["results"][0]["follow_up"]["command"] == "lorrgs spec-ranking mage-frost chimaerus-the-undreamt-god"


def test_current_season_emits_public_season_metadata(monkeypatch) -> None:
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["current-season"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"] == {"season_slug": "current"}
    assert payload["data"]["slug"] == "midnight_s1"
    assert payload["data"]["raids"] == [46.1, 46.2, 46.3, 50]
    assert ("season", {"season_slug": "current"}) in FakeLorrgsClient.calls


def test_resolve_matches_warcraftlogs_report_url_with_query_fight(monkeypatch) -> None:
    _patch_client(monkeypatch)
    url = "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
    result = runner.invoke(app, ["resolve", url])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    assert payload["match"]["kind"] == "report_overview"
    assert payload["match"]["report_id"] == "bG3xDYPqKjLm8XaR"
    assert payload["match"]["fight_id"] == 22
    assert payload["match"]["report_type"] == "damage-done"
    assert payload["next_command"] == "lorrgs report-overview bG3xDYPqKjLm8XaR"
    assert payload["results"][1]["follow_up"]["command"] == "lorrgs user-report-fights bG3xDYPqKjLm8XaR --fight 22 --type damage-done"


def test_report_overview_accepts_warcraftlogs_url(monkeypatch) -> None:
    _patch_client(monkeypatch)
    url = "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
    result = runner.invoke(app, ["report-overview", url])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["report_id"] == "bG3xDYPqKjLm8XaR"
    assert payload["query"]["fight_id"] == 22
    assert payload["query"]["report_type"] == "damage-done"
    assert payload["data"]["fights"][0]["fight_id"] == 22
    assert ("report_overview", {"report_id": "bG3xDYPqKjLm8XaR", "refresh": False}) in FakeLorrgsClient.calls


def test_report_overview_accepts_plural_lorrgs_user_reports_url(monkeypatch) -> None:
    _patch_client(monkeypatch)
    url = "https://lorrgs.io/user_reports/bG3xDYPqKjLm8XaR/fights?fight=22&type=damage-done"
    result = runner.invoke(app, ["report-overview", url])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["report_id"] == "bG3xDYPqKjLm8XaR"
    assert payload["query"]["fight_id"] == 22
    assert payload["query"]["report_type"] == "damage-done"
    assert ("report_overview", {"report_id": "bG3xDYPqKjLm8XaR", "refresh": False}) in FakeLorrgsClient.calls


def test_user_report_fights_can_take_fight_from_url(monkeypatch) -> None:
    _patch_client(monkeypatch)
    url = "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
    result = runner.invoke(app, ["user-report-fights", url])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["report_id"] == "bG3xDYPqKjLm8XaR"
    assert payload["query"]["fight"] == "22"
    assert payload["query"]["type"] == "damage-done"
    assert (
        "user_report_fights",
        {"report_id": "bG3xDYPqKjLm8XaR", "fight": "22", "player": None, "data_type": "damage-done"},
    ) in FakeLorrgsClient.calls


def test_warcraft_lorrgs_doctor_routes_through_wrapper() -> None:
    result = runner.invoke(warcraft_app, ["lorrgs", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "lorrgs"
    assert payload["capabilities"]["spec_ranking"] == "ready"


def test_warcraft_lorrgs_resolve_routes_warcraftlogs_url_through_wrapper(monkeypatch) -> None:
    _patch_client(monkeypatch)
    url = "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
    result = runner.invoke(warcraft_app, ["lorrgs", "resolve", url])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "lorrgs"
    assert payload["resolved"] is True
    assert payload["match"]["kind"] == "report_overview"
