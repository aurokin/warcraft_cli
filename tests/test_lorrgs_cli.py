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

    def spec_ranking(self, *, spec_slug: str, boss_slug: str, difficulty: str = "mythic", metric: str | None = None) -> dict[str, object]:
        self.calls.append(("spec_ranking", {"spec_slug": spec_slug, "boss_slug": boss_slug}))
        return {
            "payload": {"spec_slug": spec_slug, "boss_slug": boss_slug, "difficulty": difficulty, "reports": []},
            "source_url": f"https://api2.lorrgs.io/api/spec_ranking/{spec_slug}/{boss_slug}",
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
    assert payload["data"]["status"] == "ready"
    assert payload["data"]["auth"]["required"] is False
    assert payload["data"]["cache"]["ttls"] == {"static_metadata": 43200, "rankings": 1800, "loaded_fights": 21600}
    assert payload["data"]["capabilities"]["spec_ranking"] == "ready"
    assert payload["data"]["capabilities"]["comp_ranking"] == "ready"
    assert payload["data"]["capabilities"]["search"] == "ready"
    assert payload["data"]["capabilities"]["resolve"] == "ready"
    assert payload["data"]["capabilities"]["report_overview"] == "ready"
    assert payload["data"]["capabilities"]["current_season"] == "ready"


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
            "heal.gte.4",
            "--spec",
            "mage-frost.gte.1",
            "--killtime-min",
            "120",
            "--killtime-max",
            "180",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["roles"] == ["heal.gte.4"]
    assert payload["query"]["specs"] == ["mage-frost.gte.1"]
    assert (
        "comp_ranking",
        {
            "boss_slug": "chimaerus-the-undreamt-god",
            "limit": 12,
            "roles": ["heal.gte.4"],
            "specs": ["mage-frost.gte.1"],
            "killtime_min": 120,
            "killtime_max": 180,
        },
    ) in FakeLorrgsClient.calls


def test_comp_ranking_says_when_lorrgs_returned_no_reports(monkeypatch) -> None:
    """Every current-tier boss answered ``reports: []`` with ok:true and nothing saying the ranking was empty."""
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["comp-ranking", "nekzali-the-soulcoiler"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["reports"] == []
    assert len(data["notes"]) == 1
    assert "no composition reports for nekzali-the-soulcoiler" in data["notes"][0]


def test_spec_ranking_says_when_lorrgs_returned_no_reports(monkeypatch) -> None:
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["spec-ranking", "mage-frost", "nekzali-the-soulcoiler"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["reports"] == []
    assert data["notes"] == [
        "Lorrgs returned no mage-frost reports for nekzali-the-soulcoiler on mythic: the upstream ranking is empty, "
        "so there is nothing to rank yet. It does not mean nobody plays or logs this."
    ]


def test_user_report_fights_without_a_fight_is_a_usage_error(monkeypatch) -> None:
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["user-report-fights", "AbCdEfGh12345678"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "missing_fight"
    assert FakeLorrgsClient.calls == []


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
    # 401 is the status Lorrgs actually returns for a report Warcraft Logs keeps private.
    _patch_client(monkeypatch)
    monkeypatch.setattr(FakeLorrgsClient, "spec_status", status)
    result = runner.invoke(app, ["spec", "mage-frost"])
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert "takes no credentials" in payload["error"]["message"]
    assert payload["error"]["details"] == {"status_code": status, "url": "https://api2.lorrgs.io/api/specs/mage-frost"}


def test_unmapped_http_status_is_an_upstream_error(monkeypatch) -> None:
    _patch_client(monkeypatch)
    monkeypatch.setattr(FakeLorrgsClient, "spec_status", 503)
    result = runner.invoke(app, ["spec", "mage-frost"])
    assert result.exit_code == 5
    assert json.loads(result.stderr)["error"]["code"] == "upstream_error"


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


def test_resolve_carries_a_named_difficulty_into_the_ranking_command(monkeypatch) -> None:
    # spec-ranking defaults to mythic, so a heroic question must hand back a heroic command, and
    # comp-ranking (which takes no difficulty) must not be offered as the answer to one.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "heroic frost mage chimaerus", "--limit", "10"])
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is True
    assert data["match"]["difficulty"] == "heroic"
    assert data["next_command"] == "lorrgs spec-ranking mage-frost chimaerus-the-undreamt-god --difficulty heroic"

    comp = json.loads(runner.invoke(app, ["resolve", "heroic chimaerus", "--limit", "10"]).stdout)["data"]
    assert comp["resolved"] is False
    assert comp["results"][0]["kind"] == "comp_ranking"
    assert comp["results"][0]["ranking"]["unmatched_terms"] == ["heroic"]


def test_resolve_refuses_a_word_lorrgs_has_no_answer_for(monkeypatch) -> None:
    # Lorrgs has cooldown timelines, not guides: dropping "guide" would answer a different question.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "frost mage guide", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is False
    assert data["next_command"] is None
    assert data["results"][0]["ranking"]["unmatched_terms"] == ["guide"]

    # Filler words are not a question of their own.
    result = runner.invoke(app, ["resolve", "frost mage on chimaerus", "--limit", "10"])
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is True
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


def test_resolve_does_not_hand_over_an_unrivalled_but_only_partial_match(monkeypatch) -> None:
    # "undreamt" is one word out of "Chimaerus, the Undreamt God" — not the slug, not the short name.
    # Every provider resolves only at high confidence, and `warcraft resolve` trusts `resolved`, so a
    # partial match ("storm" -> Raszageth the Storm-Eater) stays the match at medium, with no command.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["resolve", "undreamt", "--limit", "10"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is False
    assert data["confidence"] == "medium"
    assert data["match"]["ranking"]["match_level"] == "partial"
    assert data["match"]["follow_up"]["command"] == "lorrgs comp-ranking chimaerus-the-undreamt-god"
    assert data["next_command"] is None


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
    # The reference parses exactly, so the match's command is right — but nothing checked that Lorrgs
    # will serve the report (it answers 401 for a report Warcraft Logs keeps private), so the match
    # stays at medium and, like every medium match, is not resolved.
    _patch_client(monkeypatch)
    url = "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
    result = runner.invoke(app, ["resolve", url])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is False
    assert data["confidence"] == "medium"
    assert data["match"]["kind"] == "report_overview"
    assert data["match"]["report_id"] == "bG3xDYPqKjLm8XaR"
    assert data["match"]["fight_id"] == 22
    assert data["match"]["report_type"] == "damage-done"
    assert data["match"]["ranking"]["match_reasons"] == ["explicit_report_reference"]
    assert "private" in data["match"]["caveat"]
    assert data["next_command"] is None
    assert data["match"]["follow_up"]["command"] == "lorrgs report-overview bG3xDYPqKjLm8XaR"
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


@pytest.mark.parametrize(
    "reference",
    [
        "https://www.warcraftlogs.com/reports/JVFTxcKCqrvpaAzD#fight=4",
        "JVFTxcKCqrvpaAzD",
        "https://www.warcraftlogs.com/reports/DZzR9jwYmQA6tbV7#fight=4",
        "DZzR9jwYmQA6tbV7",
        # A random code can look like capitalised words; in a report URL it is still the code.
        "https://www.warcraftlogs.com/reports/QwErTyUiOpAsDfGh#fight=3",
    ],
)
def test_report_overview_accepts_real_report_codes_with_or_without_digits(monkeypatch, reference: str) -> None:
    # Real report codes are 16 letters and digits, and many carry no digit at all.
    _patch_client(monkeypatch)
    code = reference.split("/")[-1].split("#")[0]
    result = runner.invoke(app, ["report-overview", reference])
    assert result.exit_code == 0, result.stderr
    assert ("report_overview", {"report_id": code, "refresh": False}) in FakeLorrgsClient.calls


@pytest.mark.parametrize("word", ["frostdeathknight", "restorationdruid", "1234567890123456", "HavocDemonHunter"])
def test_report_overview_rejects_a_sixteen_character_word(monkeypatch, word: str) -> None:
    # Real 16-character codes mix upper and lower case; a spec slug of that length is not a report.
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["report-overview", word])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_report_ref"
    assert FakeLorrgsClient.calls == []


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


def test_user_report_fights_passes_the_player_filter_upstream(monkeypatch) -> None:
    # Lorrgs honours ?player= (checked live: fight 22 of bG3xDYPqKjLm8XaR narrows to the one player).
    _patch_client(monkeypatch)
    result = runner.invoke(app, ["user-report-fights", "bG3xDYPqKjLm8XaR", "--fight", "22", "--player", "89.117"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["query"]["player"] == "89.117"
    assert (
        "user_report_fights",
        {"report_id": "bG3xDYPqKjLm8XaR", "fight": "22", "player": "89.117", "data_type": None},
    ) in FakeLorrgsClient.calls


def test_warcraft_lorrgs_doctor_routes_through_wrapper() -> None:
    result = runner.invoke(warcraft_app, ["lorrgs", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "lorrgs"
    assert payload["data"]["capabilities"]["spec_ranking"] == "ready"


def test_warcraft_lorrgs_resolve_routes_warcraftlogs_url_through_wrapper(monkeypatch) -> None:
    _patch_client(monkeypatch)
    url = "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
    result = runner.invoke(warcraft_app, ["lorrgs", "resolve", url])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "lorrgs"
    assert payload["data"]["resolved"] is False
    assert payload["data"]["match"]["kind"] == "report_overview"


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
    assert result.exit_code == 2
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


def _count_requests(monkeypatch, payload_for_url) -> list[str]:
    """Answer the shared HTTP seam with ``payload_for_url(url)`` and record every URL requested."""
    urls: list[str] = []

    def answer(client: object, url: str, **kwargs: object) -> httpx.Response:
        urls.append(url)
        return httpx.Response(200, json=payload_for_url(url), request=httpx.Request("GET", url))

    monkeypatch.setattr("lorrgs_cli.client.request_with_retries", answer)
    return urls


def test_static_metadata_is_replayed_from_the_cache_with_its_fetch_time(monkeypatch) -> None:
    # Wrapper search/resolve read /api/specs and /api/bosses on every query; a warm cache must not.
    monkeypatch.setenv("LORRGS_CACHE_BACKEND", "file")
    urls = _count_requests(monkeypatch, lambda url: {"specs": []})
    first = json.loads(runner.invoke(app, ["specs"]).stdout)["provenance"]
    second = json.loads(runner.invoke(app, ["specs"]).stdout)["provenance"]
    assert urls == ["https://api2.lorrgs.io/api/specs"]
    assert (first["cache_hit"], second["cache_hit"]) == (False, True)
    assert second["fetched_at"] == first["fetched_at"]
    assert second["cache_ttl_seconds"] == 43200


def test_a_fight_lorrgs_has_not_loaded_is_never_cached(monkeypatch) -> None:
    # Lorrgs answers a fight it has not loaded yet with no players; replaying that would hide the
    # fight once Lorrgs loads it.
    monkeypatch.setenv("LORRGS_CACHE_BACKEND", "file")
    players: list[dict[str, object]] = []
    urls = _count_requests(monkeypatch, lambda url: {"fights": [{"fight_id": 4, "players": list(players)}]})
    argv = ["user-report-fights", "bG3xDYPqKjLm8XaR", "--fight", "4"]
    runner.invoke(app, argv)
    players.append({"name": "Cannicus", "source_id": 88})
    loaded = json.loads(runner.invoke(app, argv).stdout)
    replayed = json.loads(runner.invoke(app, argv).stdout)
    assert len(urls) == 2
    assert loaded["data"]["fights"][0]["players"] == [{"name": "Cannicus", "source_id": 88}]
    assert replayed["provenance"]["cache_hit"] is True


def test_lorrgs_requests_carry_the_shared_user_agent_with_the_contact_url() -> None:
    from lorrgs_cli.client import LorrgsClient
    from warcraft_api.http import DEFAULT_USER_AGENT

    with LorrgsClient() as client:
        assert client._client().headers["User-Agent"] == DEFAULT_USER_AGENT


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["spec-ranking", "mage-frost", "chimaerus-the-undreamt-god", "--difficulty", "normal"], "--difficulty must be one of"),
        (["comp-ranking", "chimaerus-the-undreamt-god", "--role", "heal>=4"], "heal.gte.4"),
        (["comp-ranking", "chimaerus-the-undreamt-god", "--role", "healer.gte.1"], "tank, heal, mdps, rdps"),
        (["comp-ranking", "chimaerus-the-undreamt-god", "--spec", "mage-frost.ne.1"], "eq, gt, gte, lt, lte"),
    ],
)
def test_values_lorrgs_cannot_answer_are_usage_errors_before_any_request(monkeypatch, argv: list[str], message: str) -> None:
    # Lorrgs answers an unranked difficulty with 404 "Not found." (exit 4) and a malformed
    # composition filter with HTTP 500 (exit 5); both are the caller's typo, so exit 2 and say why.
    urls = _count_requests(monkeypatch, lambda url: {})
    result = runner.invoke(app, argv)
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert message in payload["error"]["message"]
    assert urls == []


def test_search_and_resolve_offer_no_ranking_at_a_difficulty_lorrgs_does_not_rank(monkeypatch) -> None:
    # `spec-ranking --difficulty normal` exits 2, so neither surface may hand that command over.
    _patch_client(monkeypatch)
    search = json.loads(runner.invoke(app, ["search", "normal frost mage chimaerus", "--limit", "10"]).stdout)["data"]
    assert search["results"]
    assert all(row["kind"] != "spec_ranking" for row in search["results"])
    assert all("--difficulty" not in row["follow_up"]["command"] for row in search["results"])
    data = json.loads(runner.invoke(app, ["resolve", "normal frost mage chimaerus", "--limit", "10"]).stdout)["data"]
    assert data["resolved"] is False
