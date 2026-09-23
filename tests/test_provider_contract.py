from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from warcraft_cli.provider_contract import (
    _load_wrapper_ranking_policy_cached,
    candidate_score,
    compact_wrapper_candidate,
    confidence_rank,
    decorate_resolve_payload,
    decorate_search_result,
    load_wrapper_ranking_policy,
    merged_search_page,
    name_match_strength,
    normalized_provider_score,
    provider_max_candidate_score,
    query_intents,
    resolve_payload_sort_key,
    search_result_sort_key,
    wrapper_search_ranking,
)
from warcraft_core.exit_codes import EXIT_USAGE
from warcraft_core.provider import ProviderError


@pytest.fixture
def ranking_config_root(tmp_path, monkeypatch):
    """Point ``config_root()`` at a scratch XDG config dir and return the override file's path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _load_wrapper_ranking_policy_cached.cache_clear()
    yield tmp_path / "warcraft" / "wrapper_ranking.json"
    _load_wrapper_ranking_policy_cached.cache_clear()


def test_confidence_rank_orders_known_values() -> None:
    assert confidence_rank("high") > confidence_rank("medium") > confidence_rank("low") > confidence_rank("none")
    assert confidence_rank(None) == 0


def test_candidate_score_reads_ranking_score() -> None:
    assert candidate_score({"ranking": {"score": 27}}) == 27
    assert candidate_score({"ranking": {"score": "12"}}) == 12
    assert candidate_score({"ranking": {}}) == 0
    assert candidate_score(None) == 0


def test_search_result_sort_key_prefers_higher_scores() -> None:
    rows = [
        {"provider": "method", "name": "B", "id": "b", "ranking": {"score": 10}},
        {"provider": "wowhead", "name": "A", "id": "a", "ranking": {"score": 30}},
    ]

    rows.sort(key=search_result_sort_key)

    assert rows[0]["provider"] == "wowhead"


def test_query_intents_detect_structured_profile_and_reference() -> None:
    assert "structured_profile" in query_intents("guild us illidan Liquid")
    assert "guild_profile" in query_intents("guild us illidan Liquid")
    assert "reference" in query_intents("world of warcraft api")
    assert "structured_profile" not in query_intents("world of warcraft api")
    assert "structured_profile" in query_intents("us illidan liquid")


def test_world_is_not_a_profile_region() -> None:
    # `world` is a leaderboard scope, not a region a character or guild lives in.
    assert "structured_profile" not in query_intents("world boss sha of anger")
    assert "structured_profile" not in query_intents("world boss ragnaros")


def test_a_multi_word_realm_is_still_a_structured_profile_query() -> None:
    # Raider.IO reads `<region> <realm...> <name>` with realms of up to three words.
    assert "structured_profile" in query_intents("eu tarren mill Cotti")
    assert "structured_profile" in query_intents("us area 52 Roguecane")


def test_wrapper_search_ranking_boosts_reference_provider_for_api_queries() -> None:
    wiki = wrapper_search_ranking(
        "world of warcraft api",
        {
            "provider": "warcraft-wiki",
            "name": "World of Warcraft API",
            "entity_type": "article",
            "ranking": {"score": 18},
        },
    )
    guide = wrapper_search_ranking(
        "world of warcraft api",
        {
            "provider": "method",
            "name": "API Guide",
            "entity_type": "guide",
            "ranking": {"score": 24},
        },
    )

    assert wiki["score"] > guide["score"]
    assert any("intent:reference:family:reference" in reason for reason in wiki["reasons"])


def test_search_result_sort_key_prefers_wrapper_ranking_when_present() -> None:
    rows = [
        decorate_search_result(
            "guild us illidan Liquid",
            {"provider": "method", "name": "Liquid Guide", "entity_type": "guide", "ranking": {"score": 40}},
        ),
        decorate_search_result(
            "guild us illidan Liquid",
            {"provider": "raiderio", "name": "Liquid", "kind": "guild", "ranking": {"score": 20}},
        ),
    ]

    rows.sort(key=search_result_sort_key)

    assert rows[0]["provider"] == "raiderio"
    assert rows[0]["wrapper_ranking"]["score"] > rows[1]["wrapper_ranking"]["score"]


def test_provider_kind_boosts_decide_the_order_of_otherwise_identical_candidates() -> None:
    """The shipped provider_kind_boosts table must change ordering, not just exist as data."""
    # "thunderfury" matches no intent keyword, so intent boosts are zero and only the
    # provider/kind table separates these two rows.
    spell = wrapper_search_ranking(
        "thunderfury",
        {"provider": "wowhead", "name": "Thunderfury", "kind": "spell", "ranking": {"score": 40}},
        provider_max_score=40,
    )
    achievement = wrapper_search_ranking(
        "thunderfury",
        {"provider": "wowhead", "name": "Thunderfury", "kind": "object", "ranking": {"score": 40}},
        provider_max_score=40,
    )

    assert spell["intents"] == []
    assert spell["score"] - achievement["score"] == 6  # provider_kind_boosts["wowhead"]["spell"]
    assert "provider_kind:wowhead:spell:+6" in spell["reasons"]


def test_wrapper_ranking_json_override_flips_the_winner(ranking_config_root) -> None:
    """The documented ~/.config/warcraft/wrapper_ranking.json override changes real ordering."""
    row_a = {"provider": "wowhead", "name": "X", "kind": "spell", "ranking": {"score": 40}}
    row_b = {"provider": "wowhead", "name": "X", "kind": "object", "ranking": {"score": 40}}
    assert wrapper_search_ranking("thunderfury", row_a)["score"] > wrapper_search_ranking("thunderfury", row_b)["score"]

    ranking_config_root.parent.mkdir(parents=True, exist_ok=True)
    ranking_config_root.write_text(
        json.dumps({"provider_kind_boosts": {"wowhead": {"object": 50}}}),
        encoding="utf-8",
    )
    _load_wrapper_ranking_policy_cached.cache_clear()

    policy = load_wrapper_ranking_policy()
    assert policy["provider_kind_boosts"]["wowhead"]["object"] == 50
    # The override is a deep merge: sibling sections keep their shipped defaults.
    assert policy["provider_kind_boosts"]["wowhead"]["spell"] == 6
    assert policy["intent_provider_boosts"]["character_profile"]["raiderio"] == 28
    assert wrapper_search_ranking("thunderfury", row_b)["score"] > wrapper_search_ranking("thunderfury", row_a)["score"]


def test_malformed_ranking_override_fails_as_invalid_config_naming_the_file(ranking_config_root) -> None:
    ranking_config_root.parent.mkdir(parents=True, exist_ok=True)
    ranking_config_root.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(ProviderError) as excinfo:
        load_wrapper_ranking_policy()

    assert excinfo.value.code == "invalid_config"
    assert excinfo.value.exit_code == EXIT_USAGE
    assert str(ranking_config_root) in excinfo.value.message


def test_normalized_provider_score_rescales_against_the_provider_own_best_row() -> None:
    assert normalized_provider_score(47, provider_max_score=47) == 100
    assert normalized_provider_score(24, provider_max_score=48) == 50
    assert normalized_provider_score(0, provider_max_score=48) == 0
    assert normalized_provider_score(10, provider_max_score=0) == 0
    assert provider_max_candidate_score([{"ranking": {"score": 12}}, {"ranking": {"score": 40}}]) == 40
    assert provider_max_candidate_score([]) == 0


def test_a_provider_whose_best_row_is_weak_is_not_rescaled_to_a_full_match() -> None:
    """Rescaling must not crown every provider's top row: a junk best row stays junk.

    Raider.IO's free-text rows score in the single digits, so dividing by the provider's own best
    row alone would hand a 3-point text match the same 100 as a Wowhead exact-name item.
    """
    assert normalized_provider_score(3, provider_max_score=3) == 8

    junk = wrapper_search_ranking(
        "thunderfury",
        {"provider": "raiderio", "name": "Thunder", "kind": "guild", "ranking": {"score": 3}},
        provider_max_score=3,
    )
    exact = wrapper_search_ranking(
        "thunderfury",
        {"provider": "wowhead", "name": "Thunderfury", "kind": "item", "ranking": {"score": 47}},
        provider_max_score=47,
    )

    assert junk["score"] < exact["score"]


def test_normalization_keeps_a_small_scale_provider_ahead_of_a_large_scale_one() -> None:
    """Wowhead's exact-name match must outrank a wiki filler row whose raw score is simply bigger."""
    wowhead = decorate_search_result(
        "un'goro crater",
        {"provider": "wowhead", "name": "Un'Goro Crater", "kind": "zone", "id": 490, "ranking": {"score": 47}},
        provider_max_score=47,
    )
    wiki_filler = decorate_search_result(
        "un'goro crater",
        {"provider": "warcraft-wiki", "name": "Diemetradon", "entity_type": "article", "ranking": {"score": 58}},
        provider_max_score=154,
    )

    rows = sorted([wiki_filler, wowhead], key=search_result_sort_key)

    assert rows[0]["provider"] == "wowhead"
    assert wowhead["wrapper_ranking"]["provider_score"] == 47
    assert wowhead["wrapper_ranking"]["score"] > wiki_filler["wrapper_ranking"]["score"]


def test_wrapper_search_ranking_prefers_raiderio_for_character_profile_queries() -> None:
    raiderio = wrapper_search_ranking(
        "character us illidan Roguecane",
        {
            "provider": "raiderio",
            "name": "Roguecane",
            "kind": "character",
            "ranking": {"score": 60},
        },
    )
    guide = wrapper_search_ranking(
        "character us illidan Roguecane",
        {
            "provider": "method",
            "name": "Roguecane",
            "entity_type": "guide",
            "ranking": {"score": 80},
        },
    )

    assert raiderio["score"] > guide["score"]
    assert any("intent:character_profile:provider:raiderio" in reason for reason in raiderio["reasons"])


def test_wrapper_search_ranking_prefers_raiderio_for_guild_profile_queries() -> None:
    """Raider.IO is the only guild provider, so guild intents must boost it rather than penalize it."""
    raiderio = wrapper_search_ranking(
        "guild us illidan Liquid",
        {
            "provider": "raiderio",
            "name": "Liquid",
            "kind": "guild",
            "ranking": {"score": 60},
        },
    )
    guide = wrapper_search_ranking(
        "guild us illidan Liquid",
        {
            "provider": "method",
            "name": "Liquid Guide",
            "entity_type": "guide",
            "ranking": {"score": 80},
        },
    )

    assert raiderio["score"] > guide["score"]
    assert any(reason.startswith("intent:guild_profile:provider:raiderio:+") for reason in raiderio["reasons"])


def test_compact_wrapper_candidate_keeps_ranking_and_follow_up() -> None:
    compact = compact_wrapper_candidate(
        {
            "provider": "raiderio",
            "kind": "guild",
            "name": "Liquid",
            "id": "guild:1",
            "follow_up": {"command": "raiderio guild us illidan Liquid"},
            "wrapper_ranking": {
                "score": 88,
                "reasons": ["provider_score:20"],
                "intents": ["guild_profile"],
                "provider_family": "profile",
            },
        }
    )

    assert compact["provider"] == "raiderio"
    assert compact["follow_up_command"] == "raiderio guild us illidan Liquid"
    assert compact["wrapper_ranking"]["score"] == 88


def test_compact_wrapper_candidate_keeps_provider_expansion_support() -> None:
    compact = compact_wrapper_candidate(
        {
            "provider": "wowhead",
            "kind": "item",
            "name": "Thunderfury",
            "id": 19019,
            "provider_expansion": {
                "mode": "profiled",
                "requested_expansion": "wotlk",
                "allowed": True,
                "supported_expansions": ["retail", "classic", "wotlk"],
                "review_status": "reviewed",
                "policy_note": "Provider has first-class expansion profiles.",
            },
        }
    )

    assert compact["provider_expansion"]["mode"] == "profiled"
    assert compact["provider_expansion"]["requested_expansion"] == "wotlk"
    assert compact["provider_expansion"]["allowed"] is True
    assert compact["provider_expansion"]["review_status"] == "reviewed"
    assert "first-class expansion profiles" in compact["provider_expansion"]["policy_note"]


def test_resolve_payload_sort_key_prefers_resolved_then_confidence_then_wrapper_score() -> None:
    unresolved = ("wowhead", {"resolved": False, "confidence": "medium", "match": {"ranking": {"score": 90}}})
    medium = (
        "method",
        decorate_resolve_payload(
            "mistweaver monk guide",
            "method",
            {"resolved": True, "confidence": "medium", "match": {"entity_type": "guide", "ranking": {"score": 20}}},
        ),
    )
    high = (
        "icy-veins",
        decorate_resolve_payload(
            "mistweaver monk guide",
            "icy-veins",
            {"resolved": True, "confidence": "high", "match": {"entity_type": "guide", "ranking": {"score": 10}}},
        ),
    )

    ordered = sorted([medium, unresolved, high], key=lambda row: resolve_payload_sort_key(row[0], row[1]))

    assert ordered[0][0] == "icy-veins"
    assert ordered[-1][0] == "wowhead"


def test_none_expansion_providers_report_no_expansion_support_reason() -> None:
    # The none-expansion providers (simc, blizzard-api, curseforge) underpin the wrapper's
    # relax-to-passthrough rule (AUR-496): asking them for an expansion yields
    # `provider_has_no_expansion_support` because there is no expansion semantics
    # to violate, so the proxy passes the command through with an advisory note.
    from warcraft_cli.providers import PROVIDERS, get_provider, provider_expansion_exclusion_reason

    none_expansion = {registration.name for registration in PROVIDERS if registration.expansion_mode == "none"}
    assert none_expansion == {"simc", "blizzard-api", "curseforge"}
    for provider_name in sorted(none_expansion):
        registration = get_provider(provider_name)
        assert (
            provider_expansion_exclusion_reason(registration, requested_expansion="wotlk")
            == "provider_has_no_expansion_support"
        )


# --- merged search page: the wrapper's ranking model over realistic provider score scales -------
#
# Provider scores are not comparable, and the table below uses each provider's real scale:
#   wowhead        exact name 30 + prefix 10 + all-terms + popularity, so ~47-50 top, ~17-24 partial
#   warcraft-wiki  exact title 50 + term 36 + intent/family credit, so ~106-118 top, ~40-58 filler
#   raiderio       exact structured match 70, free-text character rows 45-70
#   icy-veins      exact title 40 + content-family credit, so ~90-150
#   method         same shared article scorer, ~80-132
#   lorrgs         spec/comp ranking rows 68-99
#   warcraftlogs   explicit report references only, 92-96
#   simc           search is a deferred stub: it contributes no rows at all today
#
# Each case states the family that should own the top row and the one row an agent must find on the
# first page. `warcraft search` builds its page through exactly these two functions.


@dataclass(frozen=True)
class MergeCase:
    """One realistic query, the rows each provider returns for it, and what the page must show."""

    name: str
    query: str
    provider_rows: dict[str, list[dict[str, Any]]]
    expected_top_family: str
    required_row_id: Any
    limit: int = 5
    notes: str = field(default="")


def _decorated_rows(query: str, provider_rows: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Decorate each provider's rows the way `warcraft search` does: in provider order, top row marked."""
    return [
        decorate_search_result(
            query,
            {"provider": provider, **row},
            provider_max_score=provider_max_candidate_score(rows),
            provider_top_row=index == 0,
        )
        for provider, rows in provider_rows.items()
        for index, row in enumerate(rows)
    ]


def _merged_page(case: MergeCase) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return merged_search_page(_decorated_rows(case.query, case.provider_rows), limit=case.limit)


def _raiderio_characters(name: str, count: int) -> list[dict[str, Any]]:
    """Raider.IO answers a bare name with a page of identically scored characters."""
    return [
        {"id": 112537057 + index, "name": name, "kind": "character", "ranking": {"score": 70}}
        for index in range(count)
    ]


MERGE_CASES = [
    MergeCase(
        name="item_by_short_name",
        query="thunderfury",
        provider_rows={
            "wowhead": [
                {"id": 21992, "name": "Thunderfury", "entity_type": "spell", "ranking": {"score": 50}},
                {
                    "id": 19019,
                    "name": "Thunderfury, Blessed Blade of the Windseeker",
                    "entity_type": "item",
                    "ranking": {"score": 24},
                },
                {
                    "id": 346300,
                    "name": "Possible Thunderfury-Themed Cloak on Season of Discovery PTR",
                    "entity_type": "news",
                    "ranking": {"score": 20},
                },
            ],
            "warcraft-wiki": [
                {
                    "id": "Thunderfury, Blessed Blade of the Windseeker",
                    "name": "Thunderfury, Blessed Blade of the Windseeker",
                    "entity_type": "article",
                    "ranking": {"score": 108},
                },
                {"id": "Diemetradon", "name": "Diemetradon", "entity_type": "article", "ranking": {"score": 58}},
            ],
            "raiderio": _raiderio_characters("Thunderfury", 20),
        },
        expected_top_family="entity",
        required_row_id=19019,
        notes="the audit's red journey: twenty tied Raider.IO characters used to own all five slots",
    ),
    MergeCase(
        name="spell_by_exact_name",
        query="rejuvenation",
        provider_rows={
            "wowhead": [
                {"id": 774, "name": "Rejuvenation", "entity_type": "spell", "ranking": {"score": 50}},
                {"id": 4611, "name": "Rejuvenation Potion", "entity_type": "item", "ranking": {"score": 24}},
            ],
            "warcraft-wiki": [
                {"id": "Rejuvenation", "name": "Rejuvenation", "entity_type": "article", "ranking": {"score": 106}},
            ],
            "raiderio": _raiderio_characters("Rejuvenation", 6),
        },
        expected_top_family="entity",
        required_row_id=774,
    ),
    MergeCase(
        name="quest_by_exact_name",
        query="the missing diplomat",
        provider_rows={
            "wowhead": [
                {"id": 1324, "name": "The Missing Diplomat", "entity_type": "quest", "ranking": {"score": 47}},
                {"id": 1339, "name": "The Missing Diplomat (part 2)", "entity_type": "quest", "ranking": {"score": 20}},
            ],
            "warcraft-wiki": [
                {
                    "id": "The Missing Diplomat",
                    "name": "The Missing Diplomat",
                    "entity_type": "article",
                    "ranking": {"score": 96},
                },
            ],
        },
        expected_top_family="entity",
        required_row_id=1324,
    ),
    MergeCase(
        name="zone_by_exact_name",
        query="un'goro crater",
        provider_rows={
            "wowhead": [{"id": 490, "name": "Un'Goro Crater", "entity_type": "zone", "ranking": {"score": 47}}],
            "warcraft-wiki": [
                {"id": "Un'Goro Crater", "name": "Un'Goro Crater", "entity_type": "article", "ranking": {"score": 118}},
                {"id": "Diemetradon", "name": "Diemetradon", "entity_type": "article", "ranking": {"score": 58}},
                {"id": "Devilsaur", "name": "Devilsaur", "entity_type": "article", "ranking": {"score": 54}},
            ],
        },
        expected_top_family="entity",
        required_row_id=490,
        notes="a wiki filler row scoring 58 on a 118 scale must not outrank the zone itself",
    ),
    MergeCase(
        name="class_spec_guide",
        query="mistweaver monk guide",
        provider_rows={
            "icy-veins": [
                {
                    "id": "mistweaver-monk-pve-healing-guide",
                    "name": "Mistweaver Monk Healing Guide",
                    "entity_type": "guide",
                    "ranking": {"score": 150},
                },
                {
                    "id": "mistweaver-monk-pve-healing-rotation",
                    "name": "Mistweaver Monk Rotation",
                    "entity_type": "guide",
                    "ranking": {"score": 125},
                },
            ],
            "method": [
                {
                    "id": "mistweaver-monk-guide",
                    "name": "Mistweaver Monk Guide",
                    "entity_type": "guide",
                    "ranking": {"score": 132},
                },
            ],
            "wowhead": [
                {"id": 116680, "name": "Thunder Focus Tea", "entity_type": "spell", "ranking": {"score": 24}},
            ],
            "raiderio": _raiderio_characters("Mistweaver", 4),
        },
        expected_top_family="article",
        required_row_id="mistweaver-monk-pve-healing-guide",
    ),
    MergeCase(
        name="api_function_reference",
        query="wow api GetSpellInfo",
        provider_rows={
            "warcraft-wiki": [
                {"id": "API GetSpellInfo", "name": "API GetSpellInfo", "entity_type": "article", "ranking": {"score": 116}},
                {"id": "World of Warcraft API", "name": "World of Warcraft API", "entity_type": "article", "ranking": {"score": 74}},
            ],
            "wowhead": [
                {"id": 585, "name": "Smite", "entity_type": "spell", "ranking": {"score": 17}},
            ],
        },
        expected_top_family="reference",
        required_row_id="API GetSpellInfo",
    ),
    MergeCase(
        name="lore_article",
        query="war of the ancients lore",
        provider_rows={
            "warcraft-wiki": [
                {"id": "War of the Ancients", "name": "War of the Ancients", "entity_type": "article", "ranking": {"score": 110}},
            ],
            "wowhead": [
                {"id": 24501, "name": "War of the Ancients Tabard", "entity_type": "item", "ranking": {"score": 21}},
            ],
            "icy-veins": [
                {"id": "wow-lore-hub", "name": "WoW Lore Hub", "entity_type": "guide", "ranking": {"score": 44}},
            ],
        },
        expected_top_family="reference",
        required_row_id="War of the Ancients",
    ),
    MergeCase(
        name="structured_guild_query",
        query="guild us illidan Liquid",
        provider_rows={
            "raiderio": [
                {"id": "guild:us:illidan:liquid", "name": "Liquid", "kind": "guild", "ranking": {"score": 70}},
            ],
            "warcraft-wiki": [
                {"id": "Complexity Limit", "name": "Complexity Limit", "entity_type": "article", "ranking": {"score": 92}},
            ],
            "wowhead": [
                {"id": 20852, "name": "Liquid Fire", "entity_type": "item", "ranking": {"score": 18}},
            ],
        },
        expected_top_family="profile",
        required_row_id="guild:us:illidan:liquid",
    ),
    MergeCase(
        name="structured_character_query",
        query="character us malganis Aurow",
        provider_rows={
            "raiderio": [
                {"id": "character:us:malganis:aurow", "name": "Aurow", "kind": "character", "ranking": {"score": 70}},
            ],
            "warcraft-wiki": [
                {"id": "Mal'Ganis", "name": "Mal'Ganis", "entity_type": "article", "ranking": {"score": 88}},
            ],
        },
        expected_top_family="profile",
        required_row_id="character:us:malganis:aurow",
    ),
    MergeCase(
        name="bare_character_like_name",
        query="aurow",
        provider_rows={
            "raiderio": _raiderio_characters("Aurow", 8),
            # The wiki's full-text search answers almost any name with fuzzy rows.
            "warcraft-wiki": [
                {"id": f"Aurora {index}", "name": f"Aurora {index}", "entity_type": "article",
                 "ranking": {"score": 42 - index}}
                for index in range(5)
            ],
            "wowhead": [
                {"id": 90000 + index, "name": f"Aurowhatever {index}", "entity_type": "item",
                 "ranking": {"score": 17 - index}}
                for index in range(5)
            ],
        },
        expected_top_family="reference",
        required_row_id=112537057,
        notes="no entity is named exactly `aurow`, so the exact-name character keeps one slot",
    ),
    MergeCase(
        name="boss_name_for_logs",
        query="lura logs mythic",
        provider_rows={
            "lorrgs": [
                {"id": "lorrgs:spec_ranking:lura", "name": "Lura top parses", "kind": "spec_ranking", "ranking": {"score": 99}},
                {"id": "lorrgs:comp_ranking:lura", "name": "Lura comp ranking", "kind": "comp_ranking", "ranking": {"score": 96}},
            ],
            "wowhead": [
                {"id": 234899, "name": "Lura, the Bloodsoaked", "entity_type": "npc", "ranking": {"score": 44}},
            ],
            "warcraft-wiki": [
                {"id": "Lura", "name": "Lura", "entity_type": "article", "ranking": {"score": 98}},
            ],
        },
        expected_top_family="logs",
        required_row_id="lorrgs:spec_ranking:lura",
    ),
    MergeCase(
        name="simc_term",
        query="simc apl mistweaver monk",
        provider_rows={
            # simc's search surface is a deferred stub, so it contributes no rows for its own terms.
            "warcraft-wiki": [
                {"id": "SimulationCraft", "name": "SimulationCraft", "entity_type": "article", "ranking": {"score": 104}},
            ],
            "icy-veins": [
                {
                    "id": "mistweaver-monk-pve-healing-guide",
                    "name": "Mistweaver Monk Healing Guide",
                    "entity_type": "guide",
                    "ranking": {"score": 150},
                },
            ],
        },
        expected_top_family="reference",
        required_row_id="SimulationCraft",
    ),
]


@pytest.mark.parametrize("case", MERGE_CASES, ids=[case.name for case in MERGE_CASES])
def test_merged_search_page_answers_the_query_it_was_given(case: MergeCase) -> None:
    page, _policy = _merged_page(case)

    assert page, f"{case.name}: the merged page must not be empty"
    top_family = page[0]["wrapper_ranking"]["provider_family"]
    assert top_family == case.expected_top_family, (
        f"{case.name}: top row is {page[0]['name']!r} from {page[0]['provider']} ({top_family})"
    )
    assert case.required_row_id in [row["id"] for row in page], (
        f"{case.name}: {case.required_row_id!r} is missing from the first page: "
        f"{[(row['provider'], row['id']) for row in page]}"
    )


def test_no_single_provider_can_fill_the_merged_page() -> None:
    """Diversity: a provider whose rows all tie at its own best score cannot own every slot."""
    case = MERGE_CASES[0]
    page, policy = _merged_page(case)

    counts = policy["provider_row_counts"]
    assert counts == {"wowhead": 3, "warcraft-wiki": 2}
    assert max(counts.values()) <= policy["per_provider_cap"]
    # All twenty Raider.IO characters were withheld, and the payload says so.
    assert policy["withheld_off_intent_row_count"] == 20
    assert policy["candidate_row_count"] == 25
    assert len(page) == case.limit


def test_off_intent_profile_rows_are_a_minority_even_when_they_are_ranked_well() -> None:
    """Raider.IO rows may share the page for a bare name, but never more than a minority of it."""
    rows = [
        *[
            decorate_search_result(
                "thunderfury",
                {"provider": "raiderio", **row},
                provider_max_score=70,
            )
            for row in _raiderio_characters("Thunderfury", 20)
        ],
        decorate_search_result(
            "thunderfury",
            {"provider": "wowhead", "id": 19019, "name": "Thunderfury, Blessed Blade of the Windseeker",
             "entity_type": "item", "ranking": {"score": 24}},
            provider_max_score=24,
        ),
    ]

    page, policy = merged_search_page(rows, limit=5)

    assert page[0]["provider"] == "wowhead"
    assert policy["off_intent_provider_cap"] == 2
    assert len([row for row in page if row["provider"] == "raiderio"]) == 2
    assert len(page) == 3, "a short page beats five near-identical profiles the query never asked for"
    assert policy["withheld_off_intent_row_count"] == 18


def test_an_on_intent_provider_overflow_is_deferred_and_then_fills_the_page() -> None:
    """The per-provider cap is soft: it orders the page, it never leaves slots empty."""
    rows = [
        decorate_search_result(
            "mistweaver monk guide",
            {
                "provider": "icy-veins",
                "id": f"guide-{index}",
                "name": f"Mistweaver Monk Guide {index}",
                "entity_type": "guide",
                "ranking": {"score": 150 - index},
            },
            provider_max_score=150,
        )
        for index in range(6)
    ]

    page, policy = merged_search_page(rows, limit=5)

    assert len(page) == 5
    assert policy["deferred_row_count"] == 3
    assert policy["promoted_after_cap_count"] == 2
    assert [row["id"] for row in page] == ["guide-0", "guide-1", "guide-2", "guide-3", "guide-4"]


def test_merged_page_keeps_merge_order_and_gives_off_intent_rows_one_slot_at_most() -> None:
    """Promoted rows are not appended after the page, and an exact-name profile row takes one slot."""
    rows = [
        *[
            decorate_search_result(
                "thunderfury",
                {"provider": "wowhead", "id": index, "name": f"Thunderfury Replica {index}",
                 "entity_type": "item", "ranking": {"score": 50 - index}},
                provider_max_score=50,
            )
            for index in range(10)
        ],
        decorate_search_result(
            "thunderfury",
            {"provider": "warcraft-wiki", "id": "Diemetradon", "name": "Diemetradon",
             "entity_type": "article", "ranking": {"score": 20}},
            provider_max_score=20,
        ),
        *[
            decorate_search_result("thunderfury", {"provider": "raiderio", **row}, provider_max_score=70)
            for row in _raiderio_characters("Thunderfury", 20)
        ],
    ]

    page, policy = merged_search_page(rows, limit=6)

    # The promoted fourth Wowhead row keeps its interleaved place, ahead of the wiki row, instead of
    # being appended after it.
    assert [row["id"] for row in page[:5]] == [0, 1, 2, 3, "Diemetradon"]
    assert [row["provider"] for row in page[5:]] == ["raiderio"]
    assert policy["reserved_exact_profile_slot_count"] == 1
    assert policy["promoted_after_cap_count"] == 1
    assert policy["withheld_off_intent_row_count"] == 19


def test_the_merge_never_reorders_one_providers_rows() -> None:
    """A provider's own order is its ranking: wrapper boosts only decide between providers."""
    page, _policy = merged_search_page(
        _decorated_rows(
            "thunderfury",
            {
                "wowhead": [
                    {"id": 346300, "name": "Possible Thunderfury-Themed Cloak on the PTR", "entity_type": "news",
                     "ranking": {"score": 26}},
                    {"id": 19019, "name": "Thunderfury, Blessed Blade of the Windseeker", "entity_type": "item",
                     "ranking": {"score": 22}},
                ],
            },
        ),
        limit=5,
    )

    # The item's title and kind boosts outscore the news post, yet Wowhead listed the post first.
    assert page[1]["wrapper_ranking"]["score"] > page[0]["wrapper_ranking"]["score"]
    assert [row["id"] for row in page] == [346300, 19019]


def test_the_entity_providers_top_row_anchors_a_bare_query_even_under_a_title_prefix() -> None:
    """Live `thunderfury` shape: Wowhead leads with the item, and the item leads the page.

    Wowhead's same-named proc spells match the query exactly but sit below the item in Wowhead's
    own list, so they cannot anchor; the wiki's article about the item outscores it on raw boosts.
    """
    page, _policy = merged_search_page(
        _decorated_rows(
            "thunderfury",
            {
                "wowhead": [
                    {"id": 19019, "name": "Thunderfury, Blessed Blade of the Windseeker", "entity_type": "item",
                     "ranking": {"score": 56}},
                    {"id": 7787, "name": "Rise, Thunderfury!", "entity_type": "quest", "ranking": {"score": 46}},
                    {"id": 21992, "name": "Thunderfury", "entity_type": "spell", "ranking": {"score": 44}},
                    {"id": 27648, "name": "Thunderfury", "entity_type": "spell", "ranking": {"score": 44}},
                ],
                "warcraft-wiki": [
                    {"id": "Thunderfury, Blessed Blade of the Windseeker",
                     "name": "Thunderfury, Blessed Blade of the Windseeker", "entity_type": "article",
                     "ranking": {"score": 66}},
                    {"id": "Rise, Thunderfury!", "name": "Rise, Thunderfury!", "entity_type": "article",
                     "ranking": {"score": 36}},
                ],
            },
        ),
        limit=5,
    )

    assert page[0]["id"] == 19019
    assert page[0]["wrapper_ranking"]["anchor"] is True
    assert [row["wrapper_ranking"]["anchor"] for row in page[1:]] == [False] * 4
    assert [row["id"] for row in page if row["provider"] == "wowhead"] == [19019, 7787, 21992]


def test_an_exact_name_profile_slot_is_kept_only_for_an_exact_first_profile_row() -> None:
    """The reserved slot is for the character a bare name names, not for any profile row."""
    fillers = {
        "warcraft-wiki": [
            {"id": f"Aurora {index}", "name": f"Aurora {index}", "entity_type": "article", "ranking": {"score": 42 - index}}
            for index in range(5)
        ],
        "wowhead": [
            {"id": 90000 + index, "name": f"Aurowhatever {index}", "entity_type": "item", "ranking": {"score": 17 - index}}
            for index in range(5)
        ],
    }
    exact_first = {"raiderio": [
        {"id": 1, "name": "Aurow", "kind": "character", "ranking": {"score": 70}},
        {"id": 2, "name": "Aurowyn", "kind": "character", "ranking": {"score": 70}},
    ]}
    near_first = {"raiderio": [
        {"id": 2, "name": "Aurowyn", "kind": "character", "ranking": {"score": 70}},
        {"id": 1, "name": "Aurow", "kind": "character", "ranking": {"score": 70}},
    ]}

    page, policy = merged_search_page(_decorated_rows("aurow", {**fillers, **exact_first}), limit=5)
    assert policy["reserved_exact_profile_slot_count"] == 1
    assert [row["id"] for row in page if row["provider"] == "raiderio"] == [1]

    page, policy = merged_search_page(_decorated_rows("aurow", {**fillers, **near_first}), limit=5)
    assert policy["reserved_exact_profile_slot_count"] == 0
    assert [row for row in page if row["provider"] == "raiderio"] == []


def test_a_stale_guide_flag_from_the_provider_reaches_the_merged_and_brief_rows() -> None:
    """Wowhead marks guides its own response shows are long superseded; the wrapper passes that on."""
    rows = _decorated_rows(
        "fury warrior guide",
        {
            "wowhead": [
                {"id": 3087, "name": "Fury Warrior Guide", "entity_type": "guide",
                 "ranking": {"score": 40, "match_reasons": ["name_prefix"]}},
                {"id": 23867, "name": "Fury Warrior DF Season 4 Guide", "entity_type": "guide",
                 "ranking": {"score": 30, "match_reasons": ["name_contains_query", "stale_guide"]}},
            ],
        },
    )
    page, _policy = merged_search_page(rows, limit=5)

    assert [row["wrapper_ranking"]["stale_guide"] for row in page] == [False, True]
    assert [compact_wrapper_candidate(row)["wrapper_ranking"]["stale_guide"] for row in page] == [False, True]


def test_name_match_strength_separates_a_title_from_a_mention() -> None:
    assert name_match_strength("thunderfury", "Thunderfury") == "exact"
    assert name_match_strength("thunderfury", "Thunderfury, Blessed Blade of the Windseeker") == "title_prefix"
    assert name_match_strength("thunderfury", "Possible Thunderfury-Themed Cloak on the PTR") is None
    assert name_match_strength("", "Thunderfury") is None


def test_merged_rows_carry_the_normalized_kind_the_compact_row_reports() -> None:
    """`--brief` must not invent a field the full row lacks: providers name the type differently."""
    wowhead_row = decorate_search_result(
        "thunderfury",
        {"provider": "wowhead", "id": 19019, "name": "Thunderfury", "entity_type": "item", "ranking": {"score": 40}},
        provider_max_score=40,
    )

    assert wowhead_row["kind"] == "item"
    compact = compact_wrapper_candidate(wowhead_row)
    assert set(compact) - {"follow_up_command"} <= set(wowhead_row)
    # A provider that already names its own kind keeps it verbatim.
    raiderio_row = decorate_search_result(
        "character us illidan Roguecane",
        {"provider": "raiderio", "id": "c:1", "name": "Roguecane", "kind": "character", "ranking": {"score": 70}},
    )
    assert raiderio_row["kind"] == "character"
