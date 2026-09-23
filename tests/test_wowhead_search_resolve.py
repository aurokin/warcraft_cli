"""Search, resolve, ranking, and expansion-selection behavior for the wowhead CLI."""

from __future__ import annotations

import json

from wowhead_cli.main import app
from wowhead_cli.ranking import (
    ARTICLE_OVER_ENTITY_MARGIN,
    STALE_GUIDE_REASON,
    exact_match_score,
    is_filtered_high_confidence,
    is_high_confidence_exact_match,
    is_high_confidence_score,
    is_medium_confidence_score,
    merge_suggestion_lists,
    prefix_and_contains_score,
    resolve_confidence,
    search_result_score_and_reasons,
    term_match_score,
    type_hint_score,
    upstream_rank_bonuses,
    upstream_rank_score,
)

from tests.wowhead_testkit import runner


def test_expansions_command_exposes_profiles() -> None:
    result = runner.invoke(app, ["expansions"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["default"] == "retail"
    keys = {row["key"] for row in payload["data"]["profiles"]}
    assert "retail" in keys
    assert "wotlk" in keys



def test_search_respects_expansion_flag(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["--expansion", "wotlk", "search", "thunderfury", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["expansion"] == "wotlk"
    assert payload["data"]["search_url"].startswith("https://www.wowhead.com/wotlk/search?q=")



def test_search_guide_result_includes_guide_url(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 100, "id": 3143, "name": "Frost Death Knight DPS Guide - Midnight", "typeName": "Guide"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["--expansion", "wotlk", "search", "frost death knight guide", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["results"][0]["entity_type"] == "guide"
    assert payload["data"]["results"][0]["url"] == "https://www.wowhead.com/wotlk/guide=3143"



def test_search_faction_result_includes_faction_url(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 8, "id": 529, "name": "Argent Dawn", "typeName": "Faction"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["search", "argent dawn", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["results"][0]["entity_type"] == "faction"
    assert payload["data"]["results"][0]["url"] == "https://www.wowhead.com/faction=529"



def test_search_reranks_exact_name_match_ahead_of_noisy_popular_result(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "type": 3,
                    "id": 2,
                    "name": "Thunderfury Replica",
                    "typeName": "Item",
                    "popularity": 999999,
                },
                {
                    "type": 3,
                    "id": 19019,
                    "name": "Thunderfury",
                    "typeName": "Item",
                    "popularity": 5,
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["search", "thunderfury", "--limit", "2"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert [row["id"] for row in payload["data"]["results"]] == [19019, 2]
    assert "exact_name" in payload["data"]["results"][0]["ranking"]["match_reasons"]
    assert payload["data"]["results"][0]["ranking"]["score"] > payload["data"]["results"][1]["ranking"]["score"]



def test_search_type_hint_promotes_guides_for_guide_queries(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "type": 3,
                    "id": 19019,
                    "name": "Tabard of the Frost Death Knight Guide",
                    "typeName": "Item",
                    "popularity": 50,
                },
                {
                    "type": 100,
                    "id": 3143,
                    "name": "Frost Death Knight DPS Guide - Midnight",
                    "typeName": "Guide",
                    "popularity": 1,
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["search", "frost death knight guide", "--limit", "2"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert [row["entity_type"] for row in payload["data"]["results"]] == ["guide", "item"]
    assert "type_hint" in payload["data"]["results"][0]["ranking"]["match_reasons"]



def test_search_pet_result_includes_pet_url(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 9, "id": 39, "name": "Devilsaur", "typeName": "Hunter Pet"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["search", "devilsaur", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["results"][0]["entity_type"] == "pet"
    assert payload["data"]["results"][0]["url"] == "https://www.wowhead.com/pet=39"



def test_resolve_returns_high_confidence_match_and_next_command(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 5, "id": 86739, "name": "Fairbreeze Favors", "typeName": "Quest", "popularity": 10},
                {"type": 3, "id": 123, "name": "Fairbreeze Supplies", "typeName": "Item", "popularity": 50},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["resolve", "fairbreeze favors"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["confidence"] == "high"
    assert payload["data"]["search_query"] == "fairbreeze favors"
    assert payload["data"]["match"]["entity_type"] == "quest"
    assert payload["data"]["next_command"] == "wowhead entity quest 86739"
    assert payload["data"]["fallback_search_command"] is None



def test_resolve_falls_back_to_search_when_query_is_ambiguous(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 1, "name": "Frost Band", "typeName": "Item", "popularity": 3},
                {"type": 6, "id": 2, "name": "Frost Bolt", "typeName": "Spell", "popularity": 3},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["resolve", "frost", "--limit", "2"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is False
    assert payload["data"]["confidence"] == "low"
    assert payload["data"]["next_command"] is None
    assert payload["data"]["fallback_search_command"] == "wowhead search frost"
    assert payload["data"]["count"] == 2
    assert len(payload["data"]["candidates"]) == 2



def test_resolve_entity_type_filter_can_make_guide_resolution_confident(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Frost Death Knight", "typeName": "Item", "popularity": 50},
                {
                    "type": 100,
                    "id": 3143,
                    "name": "Frost Death Knight DPS Guide - Midnight",
                    "typeName": "Guide",
                    "popularity": 1,
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["--expansion", "wotlk", "resolve", "frost death knight", "--entity-type", "guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["filters"]["entity_types"] == ["guide"]
    assert payload["data"]["resolved"] is True
    assert payload["data"]["confidence"] == "high"
    assert payload["data"]["match"]["entity_type"] == "guide"
    assert payload["data"]["next_command"] == "wowhead --expansion wotlk guide 3143"



def test_search_results_include_follow_up_guidance(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        assert query == "thunderfury"
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item", "popularity": 5},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["search", "thunderfury", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["search_query"] == "thunderfury"
    assert payload["data"]["results"][0]["follow_up"] == {
        "recommended_surface": "entity",
        "command": "wowhead entity item 19019",
        "reason": "entity_summary",
        "alternatives": [
            "wowhead entity-page item 19019",
            "wowhead comments item 19019",
        ],
    }



def test_exact_match_score_prefers_exact_name_over_display_name() -> None:
    score, reasons = exact_match_score(
        "createframe",
        name_normalized="createframe",
        display_normalized="api createframe",
    )
    assert score == 30
    assert reasons == ["exact_name"]



def test_prefix_and_contains_score_pins_every_branch_weight() -> None:
    assert prefix_and_contains_score(
        "create", name_normalized="createframe", display_normalized="api createframe"
    ) == (10, ["name_prefix"])
    assert prefix_and_contains_score(
        "api", name_normalized="createframe", display_normalized="api createframe"
    ) == (8, ["display_name_prefix"])
    # A mid-name hit scores below either prefix hit: "Legion Remix Fury Warrior Guide" merely
    # contains "fury warrior guide", it is not named by it.
    assert prefix_and_contains_score(
        "frame", name_normalized="createframe", display_normalized="widget"
    ) == (6, ["name_contains_query"])
    assert prefix_and_contains_score(
        "frame", name_normalized="widget", display_normalized="api createframe"
    ) == (4, ["display_name_contains_query"])
    assert prefix_and_contains_score("gone", name_normalized="widget", display_normalized="api") == (0, [])



def test_term_match_score_requires_all_terms() -> None:
    score, reasons = term_match_score({"world", "api"}, haystacks=["world of warcraft api", "reference"])
    assert score == 6
    assert reasons == ["all_terms_match"]

    score, reasons = term_match_score({"world", "api", "dragonflight"}, haystacks=["world of warcraft api", "reference"])
    assert score == 0
    assert reasons == []



def test_type_hint_score_boosts_matching_entity_type() -> None:
    score, reasons = type_hint_score("quest thunderfury", entity_type="quest")
    assert score == 9
    assert reasons == ["type_hint"]

    score, reasons = type_hint_score("quest thunderfury", entity_type="item")
    assert score == 0
    assert reasons == []



def test_upstream_rank_score_adds_the_bonus_and_a_point_for_a_routable_row() -> None:
    assert upstream_rank_score(42, entity_type="item") == (43, ["upstream_database_rank"])
    assert upstream_rank_score(None, entity_type="item") == (1, [])
    assert upstream_rank_score(42, entity_type=None) == (42, ["upstream_database_rank"])
    assert upstream_rank_score(None, entity_type=None) == (0, [])


def test_upstream_rank_bonuses_follow_wowheads_own_relevance_order() -> None:
    bonuses = upstream_rank_bonuses(
        {
            "results": [],
            "categories": {
                "database": [
                    {"type": 3, "id": 19019, "name": "Thunderfury, Blessed Blade of the Windseeker"},
                    {"type": 6, "id": 21992, "name": "Thunderfury"},
                    {"name": "row without an addressable id"},
                    {"type": 3, "id": 128507, "name": "Inflatable Thunderfury"},
                ],
                "news": [{"type": 162, "id": 375994, "name": "Thunderfury news"}],
                "guides": [{"type": 100, "id": 7671, "name": "Obtaining Thunderfury"}],
            },
        }
    )
    # Only the first three rows of each list earn a bonus, keyed by Wowhead's (type, id) pair.
    assert bonuses == {(3, 19019): 42, (6, 21992): 28, (100, 7671): 21}
    assert upstream_rank_bonuses({"results": []}) == {}



def test_search_result_score_and_reasons_composes_helper_scores() -> None:
    score, reasons = search_result_score_and_reasons(
        {
            "type": 5,
            "id": 86739,
            "name": "Fairbreeze Favors",
            "displayName": "Fairbreeze Favors",
            "typeName": "Quest",
        },
        query="quest fairbreeze favors",
        ranking_query="quest fairbreeze favors",
        rank_bonus=42,
    )
    assert score > 0
    assert "all_terms_match" in reasons
    assert "type_hint" in reasons
    assert "upstream_database_rank" in reasons



def test_query_terms_match_whole_words_only() -> None:
    """"sha" is inside "Shadow" and "anger" inside "Angered"; neither row names the Sha of Anger."""
    for name in ("Shadowmourne Angered", "Shadow of Angerforge"):
        score, reasons = search_result_score_and_reasons(
            {"type": 3, "id": 49623, "name": name, "typeName": "Item"},
            query="sha of anger",
            ranking_query="sha of anger",
            rank_bonus=42,
        )
        assert (score, reasons) == (1, []), name

    score, reasons = search_result_score_and_reasons(
        {"type": 1, "id": 60491, "name": "Sha of Anger", "typeName": "NPC"},
        query="sha anger",
        ranking_query="sha anger",
        rank_bonus=42,
    )
    assert reasons == ["all_terms_match", "upstream_database_rank"]


def test_resolve_confidence_policy_helpers_cover_exact_filtered_and_medium_cases() -> None:
    assert is_high_confidence_exact_match({"exact_name"}, margin=4, second_score=20) is True
    assert is_high_confidence_exact_match({"exact_display_name"}, margin=0, second_score=0) is True
    assert is_high_confidence_exact_match({"all_terms_match"}, margin=10, second_score=0) is False

    assert is_high_confidence_score(24, margin=6) is True
    assert is_high_confidence_score(23, margin=6) is False

    assert is_filtered_high_confidence(("guide",), top_score=18, margin=4) is True
    assert is_filtered_high_confidence((), top_score=18, margin=4) is False

    assert is_medium_confidence_score(18, margin=4) is True
    assert is_medium_confidence_score(17, margin=4) is False


def test_resolve_confidence_never_calls_a_stale_guide_high() -> None:
    """An exact name with a clear margin is high confidence, unless the guide is marked stale."""
    fresh = {"ranking": {"score": 40, "match_reasons": ["exact_name"]}}
    stale = {"ranking": {"score": 40, "match_reasons": ["exact_name", STALE_GUIDE_REASON]}}
    assert resolve_confidence([fresh], entity_types=()) == "high"
    assert resolve_confidence([stale], entity_types=()) == "medium"



def test_resolve_comment_intent_uses_comment_surface_without_hurting_match_quality(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        assert query == "fairbreeze favors"
        return {
            "search": query,
            "results": [
                {"type": 5, "id": 86739, "name": "Fairbreeze Favors", "typeName": "Quest", "popularity": 10},
                {"type": 3, "id": 123, "name": "Commentary Logbook", "typeName": "Item", "popularity": 50},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["resolve", "fairbreeze favors comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["confidence"] == "high"
    assert payload["data"]["match"]["entity_type"] == "quest"
    assert payload["data"]["match"]["follow_up"]["recommended_surface"] == "comments"
    assert payload["data"]["next_command"] == "wowhead comments quest 86739"



def test_resolve_relation_intent_uses_entity_page_surface(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        assert query == "thunderfury"
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item", "popularity": 5},
                {"type": 3, "id": 2, "name": "Thunderfury Replica", "typeName": "Item", "popularity": 1000},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["resolve", "thunderfury links"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["confidence"] == "high"
    assert payload["data"]["match"]["entity_type"] == "item"
    assert payload["data"]["match"]["follow_up"]["recommended_surface"] == "entity-page"
    assert payload["data"]["next_command"] == "wowhead entity-page item 19019"



def test_resolve_guide_relation_intent_uses_guide_full(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "type": 100,
                    "id": 3143,
                    "name": "Frost Death Knight DPS Guide - Midnight",
                    "typeName": "Guide",
                    "popularity": 1,
                },
                {"type": 3, "id": 19019, "name": "Frost Death Knight", "typeName": "Item", "popularity": 50},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["resolve", "frost death knight guide full", "--entity-type", "guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["match"]["entity_type"] == "guide"
    assert payload["data"]["match"]["follow_up"]["recommended_surface"] == "guide-full"
    assert payload["data"]["next_command"] == "wowhead guide-full 3143"



def test_entity_page_mount_resolves_underlying_item_page(monkeypatch) -> None:
    page_calls = []
    html = """
    <html><head>
      <meta property="og:title" content="Reins of the Grand Expedition Yak">
      <meta name="description" content="Mount item">
      <link rel="canonical" href="https://www.wowhead.com/item=84101/reins-of-the-grand-expedition-yak">
    </head><body><a href="/npc=62809/grand-expedition-yak">Yak</a></body></html>
    """

    def fake_tooltip_with_metadata(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        assert (entity_type, entity_id) == ("mount", 460)
        return {"name": "Reins of the Grand Expedition Yak"}, "https://nether.wowhead.com/tooltip/item/84101?dataEnv=1"

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        page_calls.append((entity_type, entity_id))
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip_with_metadata", fake_tooltip_with_metadata)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity-page", "mount", "460", "--max-links", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert page_calls == [("item", 84101)]
    assert payload["data"]["entity"]["type"] == "mount"
    assert payload["data"]["entity"]["id"] == 460
    assert payload["data"]["entity"]["page_url"] == "https://www.wowhead.com/item=84101/reins-of-the-grand-expedition-yak"
    assert payload["data"]["linked_entities"]["count"] == 1



def test_comments_battle_pet_resolves_underlying_npc_page(monkeypatch) -> None:
    page_calls = []
    html = """
    <html><head>
      <meta property="og:title" content="Mechanical Squirrel">
      <meta name="description" content="Battle pet">
      <link rel="canonical" href="https://www.wowhead.com/npc=2671/mechanical-squirrel">
    </head><body>
      <a href="/item=4401/mechanical-squirrel-box">Mechanical Squirrel Box</a>
      <script>
        var lv_comments0 = [{"id": 11, "number": 0, "user": "A", "body": "Useful", "date": "2024-01-01T00:00:00-06:00", "rating": 7, "nreplies": 0, "replies": []}];
      </script>
    </body></html>
    """

    def fake_tooltip_with_metadata(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        assert (entity_type, entity_id) == ("battle-pet", 39)
        return {"name": "Mechanical Squirrel"}, "https://nether.wowhead.com/tooltip/npc/2671?dataEnv=1"

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        page_calls.append((entity_type, entity_id))
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip_with_metadata", fake_tooltip_with_metadata)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["comments", "battle-pet", "39", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert page_calls == [("npc", 2671)]
    assert payload["data"]["entity"]["type"] == "battle-pet"
    assert payload["data"]["entity"]["id"] == 39
    assert payload["data"]["entity"]["page_url"] == "https://www.wowhead.com/npc=2671/mechanical-squirrel"
    assert payload["data"]["comments"][0]["citation_url"].endswith("#comments:id=11")



def test_invalid_expansion_is_rejected() -> None:
    result = runner.invoke(app, ["--expansion", "not-a-real-expansion", "search", "defias"])
    assert result.exit_code != 0
    assert "Unknown expansion" in result.output




def test_resolve_rejects_entity_types_wowhead_suggestions_cannot_label() -> None:
    """Mounts, recipes and battle pets have no suggestion type, so the filter would match nothing."""
    for entity_type in ("mount", "recipe", "battle-pet"):
        result = runner.invoke(app, ["resolve", "thunderfury", "--entity-type", entity_type])
        assert result.exit_code == 2, entity_type
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "invalid_argument"
        assert entity_type not in payload["error"]["message"].split(": ", 1)[1]



def test_search_and_resolve_reject_a_blank_query_before_calling_wowhead(monkeypatch) -> None:
    def explode(self, query: str):  # noqa: ANN001
        raise AssertionError("a blank query must not reach Wowhead")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", explode)
    for argv in (["search", ""], ["resolve", "   "]):
        result = runner.invoke(app, argv)
        assert result.exit_code == 2, argv
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "invalid_query"



def test_search_reports_how_many_matches_the_limit_cut_off(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": index, "name": f"Thunderfury {index}", "typeName": "Item"}
                for index in range(1, 6)
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["search", "thunderfury", "--limit", "2"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["count"] == len(data["results"]) == 2
    assert data["total_matches"] == 5
    assert data["truncated"] is True

    resolved = runner.invoke(app, ["resolve", "thunderfury", "--limit", "2"])
    assert resolved.exit_code == 0
    resolve_data = json.loads(resolved.stdout)["data"]
    assert resolve_data["count"] == len(resolve_data["candidates"]) == 2
    assert resolve_data["total_matches"] == 5
    assert resolve_data["truncated"] is True


def test_resolve_recommends_news_post_when_the_best_match_is_a_news_row(monkeypatch) -> None:
    """A news row is routable, so `resolve` may answer with one and hand back `news-post`."""

    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "type": 162,
                    "id": 382931,
                    "name": "Midnight Hotfixes for September 18th",
                    "typeName": "News Post",
                    "popularity": 20,
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["resolve", "Midnight Hotfixes for September 18th"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["match"]["entity_type"] == "news"
    assert data["resolved"] is True
    assert data["next_command"] == "wowhead news-post https://www.wowhead.com/news=382931"


def test_resolve_answers_with_the_entity_when_a_news_headline_matches_the_text_better(monkeypatch) -> None:
    """A headline matches the query text better than the item it covers; the item is still the answer."""

    item = {"type": 3, "id": 19019, "name": "Thunderfury, Blessed Blade of the Windseeker", "typeName": "Item"}

    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 162, "id": 375994, "name": "Thunderfury Returns in Classic", "typeName": "News Post"},
                item,
            ],
            "categories": {"database": [item]},
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["resolve", "thunderfury returns in classic"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["match"]["entity_type"] == "item"
    assert data["match"]["id"] == 19019
    assert data["next_command"] == "wowhead entity item 19019"
    # The news row is ranked behind the entity, not dropped: it keeps its score and its follow-up.
    news_candidate = data["candidates"][-1]
    assert news_candidate["entity_type"] == "news"
    news_score = news_candidate["ranking"]["score"]
    match_score = data["match"]["ranking"]["score"]
    # It leads the item on text, but by less than an exact name match is worth.
    assert 0 < news_score - match_score < ARTICLE_OVER_ENTITY_MARGIN
    assert news_candidate["follow_up"]["recommended_surface"] == "news-post"
    assert data["count"] == data["total_matches"] == 2


def test_resolve_answers_with_the_news_post_a_query_names_outright(monkeypatch) -> None:
    """The entity preference is score-aware: a headline the query names beats a stray entity."""

    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "type": 6,
                    "id": 12345,
                    "name": "September 18th Midnight Hotfixes",
                    "typeName": "Spell",
                },
                {
                    "type": 162,
                    "id": 382931,
                    "name": "Midnight Hotfixes for September 18th",
                    "typeName": "News Post",
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["resolve", "Midnight Hotfixes for September 18th"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["match"]["entity_type"] == "news"
    assert data["match"]["id"] == 382931
    assert data["resolved"] is True
    assert data["confidence"] == "high"
    assert data["next_command"] == "wowhead news-post https://www.wowhead.com/news=382931"
    # The spell only shares the headline's words, so it trails the answer it could not beat by the margin.
    spell_candidate = data["candidates"][-1]
    assert spell_candidate["entity_type"] == "spell"
    assert data["match"]["ranking"]["score"] - spell_candidate["ranking"]["score"] >= ARTICLE_OVER_ENTITY_MARGIN


def test_merge_names_each_suggestion_list_once_per_row() -> None:
    row = {"type": 3, "id": 19019, "name": "Thunderfury"}
    merged, summary = merge_suggestion_lists({"results": [row, row], "categories": {"database": [row]}})

    assert [entry["suggestion_lists"] for entry in merged] == [["results", "database"]]
    assert summary["rows_received"] == 3
    assert summary["duplicates_merged"] == 2

