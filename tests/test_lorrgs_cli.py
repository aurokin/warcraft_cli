from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import httpx
import pytest
from lorrgs_cli.main import app
from typer.testing import CliRunner
from warcraft_cli.main import app as warcraft_app
from warcraft_core.envelope import envelope_violations

runner = CliRunner()

# Captured Lorrgs responses (see docs/architecture/FIXTURE_MAINTENANCE.md). Search/resolve ranking is
# only meaningful against Lorrgs' real roster: it holds two "Frost" specs and two encounters whose
# short name is "Salhadaar", and hand-written rows hid both collisions.
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "lorrgs"


@cache
def _captured(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


class FakeLorrgsClient:
    calls: list[tuple[str, dict[str, object]]] = []
    # Status the fake `spec` route refuses with, so one test can walk both refusal statuses.
    spec_status: int = 403

    def __enter__(self) -> FakeLorrgsClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self.calls.append(("close", {}))

    def specs(self) -> dict[str, object]:
        self.calls.append(("specs", {}))
        return {"payload": _captured("specs"), "source_url": "https://api2.lorrgs.io/api/specs"}

    def bosses(self) -> dict[str, object]:
        self.calls.append(("bosses", {}))
        return {"payload": _captured("bosses"), "source_url": "https://api2.lorrgs.io/api/bosses"}

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

    def spec(self, spec_slug: str) -> dict[str, object]:
        self.calls.append(("spec", {"spec_slug": spec_slug}))
        request = httpx.Request("GET", f"https://api2.lorrgs.io/api/specs/{spec_slug}")
        response = httpx.Response(self.spec_status, json={"detail": "Refused."}, request=request)
        raise httpx.HTTPStatusError("refused", request=request, response=response)


def _patch_client(monkeypatch) -> None:
    FakeLorrgsClient.calls = []
    monkeypatch.setattr("lorrgs_cli.provider.LorrgsClient", FakeLorrgsClient)


def _patch_transport_error(monkeypatch, exc: Exception) -> None:
    """Break the shared HTTP seam the Lorrgs client calls, without touching the client's own logic."""

    def raise_transport_error(*args: object, **kwargs: object) -> httpx.Response:
        raise exc

    monkeypatch.setattr("lorrgs_cli.client.request_with_retries", raise_transport_error)


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
    assert payload["data"]["capabilities"]["report_overview"] == "ready_cached_only"
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
    assert any(row["full_name_slug"] == "mage-frost" for row in payload["data"]["specs"])
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
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["provider"] == "lorrgs"
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Invalid Boss."


@pytest.mark.parametrize("status", [401, 403])
def test_refusal_status_is_not_reported_as_an_auth_failure(monkeypatch, status: int) -> None:
    # Lorrgs takes no credentials, so neither refusal status can mean "bad credentials" and neither
    # must send an agent to fix auth (exit 3) it can never configure: the resource is not readable.
    # 401 is the status Lorrgs actually returns for a report it has not loaded.
    _patch_client(monkeypatch)
    monkeypatch.setattr(FakeLorrgsClient, "spec_status", status)
    result = runner.invoke(app, ["spec", "mage-frost"])
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert "takes no credentials" in payload["error"]["message"]
    assert payload["error"]["details"] == {"status_code": status, "url": "https://api2.lorrgs.io/api/specs/mage-frost"}


def test_search_ranks_the_named_spec_ranking_above_every_weaker_candidate(monkeypatch) -> None:
    # Against Lorrgs' real 43 specs and 100 bosses, "frost mage chimaerus" must put the Frost Mage
    # ranking first; Frost Death Knight matches "frost" too and must not outrank it.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["search", "frost mage chimaerus", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    results = data["results"]
    assert results[0]["kind"] == "spec_ranking"
    assert results[0]["spec_slug"] == "mage-frost"
    assert results[0]["boss_slug"] == "chimaerus-the-undreamt-god"
    assert results[0]["follow_up"]["command"] == "lorrgs spec-ranking mage-frost chimaerus-the-undreamt-god"
    scores = [row["ranking"]["score"] for row in results]
    assert scores == sorted(scores, reverse=True)
    assert all(score < scores[0] for score in scores[1:])
    assert data["count"] == len(results)
    assert data["truncated"] is False


def test_resolve_promotes_the_unambiguous_spec_ranking_at_high_confidence(monkeypatch) -> None:
    # The positive counterpart of the tie tests: "frost mage chimaerus" names exactly one spec and
    # one encounter, so it must hand back the ranking command. `match_level` is "short_name" because
    # the query used the encounter's short name ("Chimaerus", not "Chimaerus, the Undreamt God"),
    # and a query that names rows only partially must not reach "high".
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "frost mage chimaerus", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is True
    assert data["confidence"] == "high"
    assert data["match"]["kind"] == "spec_ranking"
    assert data["match"]["ranking"]["match_level"] == "short_name"
    assert data["match"]["ranking"]["unmatched_terms"] == []
    assert data["next_command"] == "lorrgs spec-ranking mage-frost chimaerus-the-undreamt-god"


def test_resolve_reads_an_encounter_whose_name_is_mostly_stop_words(monkeypatch) -> None:
    # "The Eye of the Jailer" is spelled out in full by this query, but every word except "eye" and
    # "jailer" is filler. Counting the filler against the row makes "The Jailer, Zovaal" — matched on
    # its short name alone — look like the stronger match, which is the wrong encounter.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "the eye of the jailer", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is True
    assert data["confidence"] == "high"
    assert data["match"]["ranking"]["match_level"] == "named"
    assert data["next_command"] == "lorrgs comp-ranking the-eye-of-the-jailer"


def test_resolve_downgrades_an_unrivalled_but_only_partial_match_to_medium(monkeypatch) -> None:
    # "undreamt" is one word out of "Chimaerus, the Undreamt God" — not the slug, not the short name.
    # Nothing rivals it, so the handoff is still useful and goes out, but it must say how thin the
    # match was: there is no strength floor on resolving, so `confidence` is the only honest signal.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "undreamt", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is True
    assert data["confidence"] == "medium"
    assert data["match"]["ranking"]["match_level"] == "partial"
    assert data["next_command"] == "lorrgs comp-ranking chimaerus-the-undreamt-god"


def test_resolve_refuses_a_candidate_that_drops_a_word_lorrgs_recognised(monkeypatch) -> None:
    # Lorrgs knows "paladin", so "fire mage paladin" is a question about two classes. The only
    # candidate is the Fire Mage spec, which answers a narrower question than the caller asked:
    # that leftover word must block the handoff instead of being silently dropped.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "fire mage paladin", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is False
    assert data["confidence"] == "none"
    assert data["next_command"] is None
    assert data["results"][0]["ranking"]["unmatched_terms"] == ["paladin"]


def test_bare_encounter_name_resolves_to_the_composition_ranking_not_the_boss_row(monkeypatch) -> None:
    # A boss name alone produces a composition ranking and a bare encounter row with identical
    # scores. They are different kinds, so this is a preference rather than ambiguity, and the
    # preference is fixed: the ranking is the useful surface, the metadata row is not.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "chimaerus", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is True
    assert data["next_command"] == "lorrgs comp-ranking chimaerus-the-undreamt-god"
    kinds = [row["kind"] for row in data["results"]]
    assert kinds[:2] == ["comp_ranking", "boss"]
    assert data["results"][0]["ranking"]["score"] == data["results"][1]["ranking"]["score"]


def test_resolve_refuses_to_pick_between_two_specs_that_share_a_name(monkeypatch) -> None:
    # "frost <boss>" fits Frost Mage and Frost Death Knight equally well. Resolving it to one of them
    # answered a question nobody asked; the tie must surface as both candidates and no next command.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "frost chimaerus", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is False
    assert data["confidence"] == "none"
    assert data["match"] is None
    assert data["next_command"] is None
    tied = {row["spec_slug"] for row in data["results"] if row["kind"] == "spec_ranking"}
    assert tied == {"mage-frost", "deathknight-frost"}


def test_resolve_refuses_to_pick_between_two_bosses_that_share_a_short_name(monkeypatch) -> None:
    # Lorrgs lists two encounters named "Salhadaar" (Fallen-King and Nexus-King). The spec side is
    # unambiguous here, so this pins that a maximum-score candidate no longer skips the tie check.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "frost mage salhadaar", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is False
    assert data["next_command"] is None
    tied = {row["boss_slug"] for row in data["results"] if row["kind"] == "spec_ranking"}
    assert tied == {"fallenking-salhadaar", "nexusking-salhadaar"}


def test_resolve_limit_cannot_hide_the_rival_that_makes_a_query_ambiguous(monkeypatch) -> None:
    # `--limit 1` keeps one candidate in `results`, and judging ambiguity from that slice made the
    # tie invisible: the same ambiguous query resolved to Frost Death Knight at high confidence.
    # The verdict comes from every candidate, and the payload says the list was truncated.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "frost chimaerus", "--limit", "1"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is False
    assert data["next_command"] is None
    assert len(data["results"]) == 1
    assert data["count"] > 1
    assert data["truncated"] is True


def test_current_season_emits_public_season_metadata(monkeypatch) -> None:
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["current-season"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"] == {"season_slug": "current"}
    assert payload["data"]["slug"] == "midnight_s1"
    assert payload["data"]["raids"] == [46.1, 46.2, 46.3, 50]
    assert ("season", {"season_slug": "current"}) in FakeLorrgsClient.calls


def test_resolve_matches_warcraftlogs_report_url_without_promising_availability(monkeypatch) -> None:
    # The reference parses exactly, so the next command is right — but nothing checked that Lorrgs
    # will serve the report (it answers 401 for reports it has not loaded, and private ones), so the
    # handoff must not claim high confidence or an "overview_available" match reason.
    _patch_client(monkeypatch)
    url = "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
    result = runner.invoke(app, ["resolve", url])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is True
    assert data["confidence"] == "medium"
    assert data["match"]["kind"] == "report_overview"
    assert data["match"]["report_id"] == "bG3xDYPqKjLm8XaR"
    assert data["match"]["fight_id"] == 22
    assert data["match"]["report_type"] == "damage-done"
    assert data["match"]["ranking"]["match_reasons"] == ["explicit_report_reference"]
    assert "not loaded" in data["match"]["caveat"]
    assert data["next_command"] == "lorrgs report-overview bG3xDYPqKjLm8XaR"
    assert data["results"][1]["follow_up"]["command"] == "lorrgs user-report-fights bG3xDYPqKjLm8XaR --fight 22 --type damage-done"


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


@pytest.mark.parametrize(
    "argv",
    [
        ["specs"],
        ["spec-ranking", "mage-frost", "chimaerus-the-undreamt-god"],
        ["search", "frost mage chimaerus"],
        ["resolve", "frost mage chimaerus"],
    ],
)
def test_connect_error_emits_error_envelope_with_network_exit_code(monkeypatch, argv: list[str]) -> None:
    _patch_transport_error(monkeypatch, httpx.ConnectError("connection refused"))
    result = runner.invoke(app, argv)
    assert result.exit_code == 5
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["provider"] == "lorrgs"
    assert payload["schema_version"] == "1"
    assert payload["error"]["code"] == "network_error"


def test_timeout_emits_error_envelope_with_network_exit_code(monkeypatch) -> None:
    _patch_transport_error(monkeypatch, httpx.ReadTimeout("timed out"))
    result = runner.invoke(app, ["specs"])
    assert result.exit_code == 5
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "timeout"


def test_invalid_report_reference_is_a_structured_usage_failure() -> None:
    result = runner.invoke(app, ["report-overview", "not a report"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_report_ref"


def test_list_shaped_route_is_keyed_under_its_payload_kind(monkeypatch) -> None:
    # /api/zones answers with a bare JSON array, but the shared envelope requires `data` to be an
    # object (docs/foundation/ERROR_CONTRACT.md). Key it the way the wrapped routes already are.
    zones = [{"id": 53.1, "name_slug": "the-venomous-abyss", "bosses": []}]
    monkeypatch.setattr(
        "lorrgs_cli.client.LorrgsClient.zones",
        lambda self: {"payload": zones, "source_url": "https://api2.lorrgs.io/api/zones"},
    )
    result = runner.invoke(app, ["zones"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert not envelope_violations(payload)
    assert payload["data"] == {"zones": zones}
