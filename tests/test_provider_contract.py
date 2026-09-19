from __future__ import annotations

import json

import pytest
from warcraft_cli.provider_contract import (
    _load_wrapper_ranking_policy_cached,
    candidate_score,
    compact_wrapper_candidate,
    confidence_rank,
    decorate_resolve_payload,
    decorate_search_result,
    load_wrapper_ranking_policy,
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
    assert policy["provider_kind_boosts"]["raiderio"]["character"] == 16
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
