from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner
from warcraft_core.envelope import ENVELOPE_KEYS, REQUIRED_KEYS, envelope_violations
from warcraft_core.provider import ProviderError
from warcraft_wiki_cli.client import WarcraftWikiAPIError, WarcraftWikiClient
from warcraft_wiki_cli.main import app as warcraft_wiki_app
from warcraft_wiki_cli.provider import (
    _typed_allowed_families,
    _typed_article_payload,
    _typed_direct_article_result,
    _typed_direct_refs,
    _typed_search_match,
    _typed_search_queries,
)
from warcraft_wiki_cli.search import is_confident_match, normalize_wiki_query, score_wiki_match, title_names_query

runner = CliRunner()

# Captured warcraft.wiki.gg API responses; see docs/architecture/FIXTURE_MAINTENANCE.md.
CAPTURED_DIR = Path(__file__).parent / "fixtures" / "warcraft_wiki"


def _captured(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((CAPTURED_DIR / name).read_text())
    return payload


class _CapturedTransport:
    """Stands in for ``request_with_retries`` so the real request params and parsers both run."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    def __call__(self, client: Any, url: str, *, params: dict[str, Any], retry_attempts: int) -> httpx.Response:
        self.calls.append({"url": url, **params})
        return httpx.Response(200, json=self._payload, request=httpx.Request("GET", url))


def _event_page_payload() -> dict[str, object]:
    return {
        "article": {
            "title": "Event:PLAYER LOGIN",
            "slug": "event-player-login",
            "display_title": "PLAYER_LOGIN",
            "page_url": "https://warcraft.wiki.gg/wiki/Event:PLAYER_LOGIN",
            "section_slug": "event-player-login",
            "section_title": "PLAYER_LOGIN",
            "page_count": 1,
            "content_family": "event_reference",
        },
        "page": {
            "title": "PLAYER_LOGIN",
            "description": "Triggered immediately before PLAYER_ENTERING_WORLD on login.",
            "canonical_url": "https://warcraft.wiki.gg/wiki/Event:PLAYER_LOGIN",
        },
        "navigation": {"count": 0, "items": []},
        "article_content": {
            "html": "<p>Triggered immediately before PLAYER_ENTERING_WORLD on login.</p>",
            "text": "Triggered immediately before PLAYER_ENTERING_WORLD on login.",
            "headings": [],
            "sections": [
                {
                    "title": "Introduction",
                    "level": 1,
                    "ordinal": 1,
                    "anchor": "Introduction",
                    "text": "Triggered immediately before PLAYER_ENTERING_WORLD on login.",
                    "html": "<p>Triggered immediately before PLAYER_ENTERING_WORLD on login.</p>",
                }
            ],
        },
        "reference": {
            "content_family": "event_reference",
            "programming_reference": True,
            "summary": "Triggered immediately before PLAYER_ENTERING_WORLD on login.",
        },
        "linked_entities": [],
        "citations": {"page": "https://warcraft.wiki.gg/wiki/Event:PLAYER_LOGIN"},
    }


def _page_payload() -> dict[str, object]:
    return {
        "article": {
            "title": "World of Warcraft API",
            "slug": "world-of-warcraft-api",
            "display_title": "World of Warcraft API",
            "page_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
            "section_slug": "world-of-warcraft-api",
            "section_title": "World of Warcraft API",
            "page_count": 1,
            "content_family": "framework_page",
        },
        "page": {
            "title": "World of Warcraft API",
            "description": "Programming reference",
            "canonical_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
        },
        "navigation": {
            "count": 2,
            "items": [
                {"title": "API systems", "url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API#API_systems",
                    "section_slug": "API_systems", "active": True, "ordinal": 1},
                {"title": "Object APIs", "url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API#Object_APIs",
                    "section_slug": "Object_APIs", "active": True, "ordinal": 2},
            ],
        },
        "article_content": {
            "html": "<h2><span class='mw-headline' id='API_systems'>API systems</span></h2><p>FrameXML reference.</p>",
            "text": "API systems FrameXML reference.",
            "headings": [{"title": "API systems", "level": 2, "ordinal": 1, "anchor": "API_systems"}],
            "sections": [{"title": "API systems", "level": 2, "ordinal": 1, "anchor": "API_systems", "text": "FrameXML reference.", "html": "<p>FrameXML reference.</p>"}],
        },
        "reference": {
            "content_family": "framework_page",
            "programming_reference": True,
            "summary": "FrameXML reference.",
        },
        "linked_entities": [
            {"type": "wiki_article", "id": "UIOBJECT Frame", "name": "UIOBJECT Frame", "url": "https://warcraft.wiki.gg/wiki/UIOBJECT_Frame"},
        ],
        "citations": {"page": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API"},
    }


def _ui_handler_payload(*, content_family: str = "ui_handler") -> dict[str, object]:
    """The parsed OnKeyDown page; ``content_family`` overrides let a test fetch a page of the wrong family."""
    return {
        "article": {
            "title": "UIHANDLER OnKeyDown",
            "slug": "uihandler-onkeydown",
            "display_title": "UIHANDLER OnKeyDown",
            "page_url": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnKeyDown",
            "section_slug": "uihandler-onkeydown",
            "section_title": "UIHANDLER OnKeyDown",
            "page_count": 1,
            "content_family": content_family,
        },
        "page": {
            "title": "UIHANDLER OnKeyDown",
            "description": "Handler reference",
            "canonical_url": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnKeyDown",
        },
        "navigation": {"count": 0, "items": []},
        "article_content": {
            "html": "<p>Fires when a key is pressed.</p>",
            "text": "Fires when a key is pressed.",
            "headings": [],
            "sections": [{"title": "Introduction", "level": 1, "ordinal": 1, "anchor": "Introduction", "text": "Fires when a key is pressed.", "html": "<p>Fires when a key is pressed.</p>"}],
        },
        "reference": {
            "content_family": content_family,
            "programming_reference": True,
            "summary": "Fires when a key is pressed.",
        },
        "linked_entities": [],
        "citations": {"page": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnKeyDown"},
    }


def _api_payload() -> dict[str, object]:
    return {
        "article": {
            "title": "API CreateFrame",
            "slug": "api-createframe",
            "display_title": "API CreateFrame",
            "page_url": "https://warcraft.wiki.gg/wiki/API_CreateFrame",
            "section_slug": "api-createframe",
            "section_title": "API CreateFrame",
            "page_count": 1,
            "content_family": "api_function",
        },
        "page": {
            "title": "API CreateFrame",
            "description": "Creates a Frame object.",
            "canonical_url": "https://warcraft.wiki.gg/wiki/API_CreateFrame",
        },
        "navigation": {"count": 0, "items": []},
        "article_content": {
            "html": "<p>Creates a Frame object.</p>",
            "text": "Creates a Frame object.",
            "headings": [],
            "sections": [{"title": "Introduction", "level": 1, "ordinal": 1, "anchor": "Introduction", "text": "Creates a Frame object.", "html": "<p>Creates a Frame object.</p>"}],
        },
        "reference": {
            "content_family": "api_function",
            "programming_reference": True,
            "summary": "Creates a Frame object.",
            "signature": "frame = CreateFrame(frameType)",
        },
        "linked_entities": [],
        "citations": {"page": "https://warcraft.wiki.gg/wiki/API_CreateFrame"},
    }


def test_warcraft_wiki_doctor_reports_ready_capabilities() -> None:
    result = runner.invoke(warcraft_wiki_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraft-wiki"
    assert payload["data"]["capabilities"]["search"] == "ready"
    assert payload["data"]["capabilities"]["api"] == "ready"
    assert payload["data"]["capabilities"]["event"] == "ready"
    assert payload["data"]["capabilities"]["article_query"] == "ready"


def test_warcraft_wiki_search_and_resolve(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            2,
            [
                {"title": "World of Warcraft API", "pageid": 1, "snippet": "API systems and FrameXML.",
                    "url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API"},
                {"title": "API", "pageid": 2, "snippet": "General API page.", "url": "https://warcraft.wiki.gg/wiki/API"},
            ],
        ),
    )

    search_result = runner.invoke(warcraft_wiki_app, ["search", "world of warcraft api"])
    assert search_result.exit_code == 0
    search_payload = json.loads(search_result.stdout)["data"]
    assert search_payload["results"][0]["entity_type"] == "article"
    assert search_payload["results"][0]["metadata"]["content_family"] == "framework_page"

    resolve_result = runner.invoke(warcraft_wiki_app, ["resolve", "world of warcraft api"])
    assert resolve_result.exit_code == 0
    resolve_payload = json.loads(resolve_result.stdout)["data"]
    assert resolve_payload["resolved"] is True
    assert resolve_payload["next_command"] == "warcraft-wiki article 'World of Warcraft API'"


def test_search_articles_requests_the_event_namespace_and_parses_the_captured_response(monkeypatch) -> None:
    transport = _CapturedTransport(_captured("search_player_login.json"))
    monkeypatch.setattr("warcraft_wiki_cli.client.request_with_retries", transport)

    with WarcraftWikiClient() as client:
        total_hits, rows = client.search_articles("PLAYER_LOGIN", limit=25)

    assert transport.calls == [
        {
            "url": "https://warcraft.wiki.gg/api.php",
            "action": "query",
            "list": "search",
            "srsearch": "PLAYER_LOGIN",
            "srlimit": 25,
            # Namespace 3000 holds "API:" pages and 3004 holds "Event:" pages; without them
            # list=search cannot see an API function or a game event at all.
            "srnamespace": "0|3000|3004",
            "format": "json",
        }
    ]
    assert total_hits == 472
    assert rows[0]["title"] == "Event:PLAYER LOGIN"
    assert rows[0]["url"] == "https://warcraft.wiki.gg/wiki/Event:PLAYER_LOGIN"


def test_search_ranks_the_event_page_above_rows_that_only_match_upstream(monkeypatch) -> None:
    transport = _CapturedTransport(_captured("search_player_login.json"))
    monkeypatch.setattr("warcraft_wiki_cli.client.request_with_retries", transport)

    result = runner.invoke(warcraft_wiki_app, ["search", "PLAYER_LOGIN", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    # Exact scores, because the gap is the contract: none of the runners-up carry PLAYER_LOGIN in
    # their title, so their score is MediaWiki's full-text rank (<= 10) plus family/snippet bonuses.
    assert [(row["id"], row["ranking"]["score"]) for row in payload["results"]] == [
        ("Event:PLAYER LOGIN", 82),
        ("UIHANDLER OnEvent", 31),
        ("API:Frame IsEventRegistered", 30),
        ("API:Frame SetMovable", 29),
        ("API:Frame IsUserPlaced", 28),
    ]
    top = payload["results"][0]
    assert top["metadata"]["content_family"] == "event_reference"
    assert "exact_event_title" in top["ranking"]["match_reasons"]
    # Every contribution is named, so a consumer can tell "upstream put it first" from "it matched".
    assert payload["results"][1]["ranking"]["match_reasons"] == [
        "upstream_rank_2",
        "all_terms_match",
        "snippet_match",
        "family_ui_handler",
    ]


def test_search_keeps_rows_that_only_ride_the_upstream_rank(monkeypatch) -> None:
    # Silently dropping weak rows would make data.count disagree with data.results; they stay,
    # scored at (or near) zero and labelled with the upstream rank that is their only evidence.
    transport = _CapturedTransport(_captured("search_player_login.json"))
    monkeypatch.setattr("warcraft_wiki_cli.client.request_with_retries", transport)

    result = runner.invoke(warcraft_wiki_app, ["search", "PLAYER_LOGIN", "--limit", "25"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    ranking = {row["id"]: (row["ranking"]["score"], row["ranking"]["match_reasons"]) for row in payload["results"]}
    assert len(payload["results"]) == 25
    assert ranking["With Hope in Hand"] == (0, ["upstream_rank_20"])
    assert ranking["AddOn loading process"] == (2, ["upstream_rank_9"])


def test_search_ranks_the_article_a_qualified_query_names_above_pages_that_mention_it(monkeypatch) -> None:
    # Captured: MediaWiki ranks "Sha of Anger" fifth for this query, behind pages whose snippets merely
    # list it ("WoW's 20th Anniversary", "Tap", "Armor set", "Bonus roll"). Every row's snippet carries
    # all five words, so only the title can tell the subject from a mention of it.
    transport = _CapturedTransport(_captured("search_world_boss_sha_of_anger.json"))
    monkeypatch.setattr("warcraft_wiki_cli.client.request_with_retries", transport)

    result = runner.invoke(warcraft_wiki_app, ["search", "world boss sha of anger", "--limit", "2"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert [(row["id"], row["ranking"]["score"]) for row in payload["results"]] == [
        ("Sha of Anger", 38),
        ("World boss", 28),
    ]
    assert "query_contains_title" in payload["results"][0]["ranking"]["match_reasons"]


def test_resolve_picks_the_disambiguated_page_a_query_spells_out_over_its_base_page(monkeypatch) -> None:
    # Captured rows: MediaWiki has "Sha of Anger" 5th and "Sha of Anger (Anniversary)" 20th, and the base
    # page's snippet names the Anniversary version, so it carries every query word too.
    transport = _CapturedTransport(_captured("search_world_boss_sha_of_anger.json"))
    monkeypatch.setattr("warcraft_wiki_cli.client.request_with_retries", transport)

    result = runner.invoke(warcraft_wiki_app, ["resolve", "sha of anger anniversary"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "Sha of Anger (Anniversary)"
    # The title spells out the whole query, so it is an exact title rather than a partial one.
    assert "exact_title" in payload["match"]["ranking"]["match_reasons"]
    assert "query_contains_title" not in payload["match"]["ranking"]["match_reasons"]
    assert payload["candidates"][1]["id"] == "Sha of Anger"


@pytest.mark.parametrize(
    ("query", "title", "exact"),
    [
        pytest.param("xuen tactics", "Xuen (tactics)", True, id="parenthetical_counts_as_words"),
        pytest.param("patch 1.12", "Patch 1.1.2", False, id="digit_groups_stay_apart"),
    ],
)
def test_exact_title_compares_the_words_of_title_and_query(query: str, title: str, exact: bool) -> None:
    _, reasons, _ = score_wiki_match(query, query, title, "", ordinal=0)

    assert ("exact_title" in reasons) is exact


@pytest.mark.parametrize(
    ("query", "title"),
    [
        pytest.param("shadow priest", "Sha", id="letters_inside_a_query_word"),
        pytest.param("world boss sha of anger", "World Sha", id="words_not_adjacent_in_the_query"),
    ],
)
def test_query_contains_title_needs_the_title_as_a_whole_word_phrase_of_the_query(query: str, title: str) -> None:
    _, reasons, _ = score_wiki_match(query, query, title, "", ordinal=0)

    assert "query_contains_title" not in reasons


def test_score_wiki_match_caps_the_upstream_rank_baseline() -> None:
    # A row that matches nothing rides MediaWiki's order alone. The cap is pinned to its literal
    # value: it has to stay small enough that no unexplained row can outrank a real title match.
    first, first_reasons, family = score_wiki_match("PLAYER_LOGIN", "player_login", "With Hope in Hand", "", ordinal=0)
    tenth, tenth_reasons, _ = score_wiki_match("PLAYER_LOGIN", "player_login", "With Hope in Hand", "", ordinal=9)

    assert family == "general_article"
    assert (first, first_reasons) == (10, ["upstream_rank_1"])
    assert (tenth, tenth_reasons) == (1, ["upstream_rank_10"])


@pytest.mark.parametrize(
    ("query", "expected_query", "expected_excluded"),
    [
        ("faction argent dawn", "argent dawn", ["faction"]),
        ("lore jaina proudmoore", "jaina proudmoore", ["lore"]),
        ("zone elwynn forest", "elwynn forest", ["zone"]),
        ("profession alchemy", "alchemy", ["profession"]),
        ("class druid", "druid", ["class"]),
        ("expansion legion", "legion", ["expansion"]),
        ("guide lore jaina", "jaina", ["guide", "lore"]),
        # "Zone scaling" is a page title, so the leading "zone" is the subject, not a hint.
        ("zone scaling", "zone scaling", []),
        # A hint word that is not leading names the page ("... Guide"), so it stays.
        ("mistweaver monk guide", "mistweaver monk guide", []),
        # Nothing would be left, so the query survives whole rather than becoming empty.
        ("lore", "lore", []),
        # "wiki" is provider noise: stripped by the shared normalizer, never reported as excluded.
        ("wiki createframe", "createframe", []),
    ],
)
def test_normalize_wiki_query_drops_only_leading_family_hints(query: str, expected_query: str, expected_excluded: list[str]) -> None:
    assert normalize_wiki_query(query) == (expected_query, expected_excluded)


def test_warcraft_wiki_score_wiki_match_boosts_programming_pages() -> None:
    score, reasons, family = score_wiki_match(
        "API CreateFrame",
        "createframe",
        "API CreateFrame",
        "Creates a Frame object.",
        ordinal=0,
    )

    assert family == "api_function"
    assert "exact_api_title" in reasons
    assert "intent_programming" in reasons
    assert score >= 100


def test_warcraft_wiki_score_wiki_match_handles_expansion_alias() -> None:
    score, reasons, family = score_wiki_match(
        "expansion Legion",
        "legion",
        "World of Warcraft: Legion",
        "Expansion overview.",
        ordinal=0,
    )

    assert family == "expansion_reference"
    assert "expansion_alias_match" in reasons
    assert "intent_systems" in reasons


def test_typed_search_queries_add_surface_prefixes() -> None:
    assert _typed_search_queries("CreateFrame", surface="api") == ["CreateFrame", "API CreateFrame"]
    assert _typed_search_queries("OnKeyDown", surface="event") == ["OnKeyDown", "UIHANDLER OnKeyDown"]
    assert _typed_search_queries("API CreateFrame", surface="api") == ["API CreateFrame"]


def test_typed_direct_refs_try_the_namespaced_title_first() -> None:
    assert _typed_direct_refs("PLAYER_LOGIN", surface="event") == [
        "Event:PLAYER LOGIN",
        "UIHANDLER PLAYER LOGIN",
        "PLAYER LOGIN",
    ]
    assert _typed_direct_refs("CreateFrame", surface="api") == ["API:CreateFrame", "API CreateFrame", "CreateFrame"]
    assert _typed_direct_refs("Event:PLAYER LOGIN", surface="event") == ["Event:PLAYER LOGIN"]


def test_typed_direct_article_result_returns_supported_family() -> None:
    class FakeClient:
        def fetch_article_page(self, ref: str) -> dict[str, object]:
            if ref == "API CreateFrame":
                return _api_payload()
            return _page_payload()

    result = _typed_direct_article_result(
        FakeClient(),
        direct_refs=["World of Warcraft API", "API CreateFrame"],
        allowed_families=_typed_allowed_families("api"),
    )

    assert result is not None
    assert result["article"]["content_family"] == "framework_page"


@pytest.mark.parametrize(
    ("title", "query", "expected"),
    [
        # Separators on either side are noise, so the event's own page still names PLAYER_LOGIN.
        pytest.param("Event:PLAYER LOGIN", "PLAYER_LOGIN", True, id="separators_collapse"),
        pytest.param("UIHANDLER OnEvent", "PLAYER_LOGIN", False, id="unrelated_handler_page"),
        # A phrase query lands on the camel-case handler title, MediaWiki's namespace word included.
        pytest.param("UIHANDLER OnKeyDown", "key down handler", True, id="phrase_matches_components"),
        # Every query word has to land, not just one of them.
        pytest.param("UIHANDLER OnKeyDown", "key up handler", False, id="one_query_word_missing"),
        # Letters sitting inside an identifier are not its name, however short the query is.
        pytest.param("API UnitIsPlayer", "is", False, id="fragment_is_not_a_name"),
        pytest.param("API UnitHealthMax", "UnitHealth", False, id="head_of_a_longer_identifier"),
        pytest.param("API:UnitHealth", "unit health", True, id="query_spells_out_the_identifier"),
    ],
)
def test_title_names_query_matches_whole_words_not_substrings(title: str, query: str, expected: bool) -> None:
    assert title_names_query(title, query) is expected


def _ranked_row(ref: str, score: int, reasons: list[str]) -> dict[str, Any]:
    return {"id": ref, "name": ref, "ranking": {"score": score, "match_reasons": reasons}}


def test_typed_search_match_requires_clear_winner() -> None:
    match = _typed_search_match(
        [
            _ranked_row("API CreateFrame", 70, ["upstream_rank_1", "exact_api_title"]),
            _ranked_row("API CreateTexture", 40, ["upstream_rank_2", "title_prefix"]),
        ],
        query="CreateFrame",
        surface="api",
    )
    assert match["id"] == "API CreateFrame"


@pytest.mark.parametrize(
    ("surface", "query", "rows"),
    [
        # The residual bb-2 shape: a single allowed-family row that rode MediaWiki's order and shares
        # one incidental word with the query. Rank + family + snippet must never add up to "confident".
        pytest.param(
            "event",
            "OnEvent",
            [_ranked_row("UIHANDLER OnEvent", 42, ["upstream_rank_1", "snippet_match", "intent_programming", "family_ui_handler"])],
            id="no_query_coverage",
        ),
        # Two pages that both carry the queried name, neither clearly better: ask the caller.
        pytest.param(
            "event",
            "LOOT",
            [
                _ranked_row("Event:LOOT OPENED", 40, ["upstream_rank_1", "all_terms_match"]),
                _ranked_row("Event:LOOT CLOSED", 30, ["upstream_rank_2", "all_terms_match"]),
            ],
            id="no_clear_winner",
        ),
        # A high-scoring row whose title never names the query: it matched in a page body we cannot
        # see, so it is not the page the caller asked for however confident the ranking looks.
        pytest.param(
            "event",
            "OnEvent",
            [_ranked_row("Jaina Proudmoore", 62, ["upstream_rank_1", "exact_title", "all_terms_match"])],
            id="title_does_not_name_the_query",
        ),
        # The query is only the head of the title's identifier: `api UnitHealth` must not answer with
        # `API UnitHealthMax`, however confidently the ranker scored the one row left standing.
        pytest.param(
            "api",
            "UnitHealth",
            [_ranked_row("API UnitHealthMax", 62, ["upstream_rank_1", "title_contains_query", "all_terms_match"])],
            id="title_names_a_longer_identifier",
        ),
    ],
)
def test_typed_search_match_fails_not_found_instead_of_guessing(surface: str, query: str, rows: list[dict[str, Any]]) -> None:
    with pytest.raises(ProviderError) as excinfo:
        _typed_search_match(rows, query=query, surface=surface)

    assert excinfo.value.code == "not_found"
    assert excinfo.value.exit_code == 4
    # The rejected candidates are reported, so the caller can see what was on offer.
    assert excinfo.value.details["candidates"] == [row["id"] for row in rows]


def test_is_confident_match_requires_the_top_row_to_cover_the_query() -> None:
    floor = ["upstream_rank_1", "snippet_match", "intent_programming", "family_ui_handler"]
    covering_top = _ranked_row("UIHANDLER OnKeyDown", 52, [*floor, "all_terms_match"])

    assert is_confident_match([]) is False
    # Rank + family + one shared word is the floor every allowed-family row collects, not a match.
    assert is_confident_match([_ranked_row("UIHANDLER OnEvent", 42, floor)]) is False
    assert is_confident_match([covering_top]) is True
    # A rival that does not cover the query is not a rival, however close its score sits.
    assert is_confident_match([covering_top, _ranked_row("UIHANDLER OnEvent", 50, floor)]) is True
    # Two rows that both cover the query and score within 18 of each other are genuinely ambiguous.
    assert is_confident_match([covering_top, _ranked_row("UIHANDLER OnEvent", 50, [*floor, "all_terms_match"])]) is False


def test_is_confident_match_does_not_count_the_query_contains_title_bonus() -> None:
    # 46 clears 25 + 18 only because of the title-in-query bonus; the title "Sha of Anger" is part of
    # "sha of anger anniversary", which is no evidence it is the page the query asks for.
    top = _ranked_row("Sha of Anger", 46, ["upstream_rank_1", "all_terms_match", "snippet_match", "query_contains_title"])
    rival = _ranked_row("Sha of Anger (Anniversary)", 25, ["upstream_rank_10", "normalized_title_match", "all_terms_match"])

    assert is_confident_match([top, rival]) is False


def test_api_payload_prefers_direct_fetch_before_search() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.search_calls: list[str] = []

        def fetch_article_page(self, ref: str) -> dict[str, object]:
            if ref != "API CreateFrame":
                raise WarcraftWikiAPIError("invalid_article_ref", "not found")
            return _api_payload()

        def search_articles(self, query: str, limit: int) -> tuple[int, list[dict[str, Any]]]:
            self.search_calls.append(query)
            return 0, []

    client = FakeClient()
    result = _typed_article_payload(client, "CreateFrame", surface="api", full=False)

    assert result["resolved_from"] == "direct_fetch"
    assert result["resolved_surface"] == "api"
    assert result["search_queries"] == ["API:CreateFrame", "API CreateFrame", "CreateFrame"]
    assert client.search_calls == []


def test_event_payload_fetches_the_event_namespace_page_before_searching() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.fetched: list[str] = []
            self.search_calls: list[str] = []

        def fetch_article_page(self, ref: str) -> dict[str, object]:
            self.fetched.append(ref)
            if ref != "Event:PLAYER LOGIN":
                raise WarcraftWikiAPIError("missingtitle", "The page you specified doesn't exist.")
            return _event_page_payload()

        def search_articles(self, query: str, limit: int) -> tuple[int, list[dict[str, Any]]]:
            self.search_calls.append(query)
            return 0, []

    client = FakeClient()
    result = _typed_article_payload(client, "PLAYER_LOGIN", surface="event", full=False)

    assert client.fetched == ["Event:PLAYER LOGIN"]
    assert client.search_calls == []
    assert result["article"]["title"] == "Event:PLAYER LOGIN"
    assert result["article"]["content_family"] == "event_reference"
    assert result["resolved_from"] == "direct_fetch"


class _SearchFallbackClient:
    """No direct title hits, so the typed surface has to fall back to ranked search."""

    def __init__(self, fetched_page: dict[str, object]) -> None:
        self._fetched_page = fetched_page
        self.fetched: list[str] = []

    def fetch_article_page(self, ref: str) -> dict[str, object]:
        self.fetched.append(ref)
        if ref != "UIHANDLER OnKeyDown":
            raise WarcraftWikiAPIError("missingtitle", "The page you specified doesn't exist.")
        return self._fetched_page

    def search_articles(self, query: str, limit: int) -> tuple[int, list[dict[str, Any]]]:
        return 2, [
            {
                "title": "UIHANDLER OnKeyDown",
                "pageid": 1,
                "snippet": "Fires when a key is pressed.",
                "url": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnKeyDown",
            },
            # Same family, same surface, no overlap with the query: it must lose on coverage, not luck.
            {
                "title": "UIHANDLER OnEvent",
                "pageid": 2,
                "snippet": "Fires when the frame receives a registered event.",
                "url": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnEvent",
            },
        ]


def test_event_payload_falls_back_to_the_candidate_that_covers_the_query() -> None:
    client = _SearchFallbackClient(_ui_handler_payload())

    result = _typed_article_payload(client, "key down handler", surface="event", full=False)

    assert client.fetched[:3] == ["Event:key down handler", "UIHANDLER key down handler", "key down handler"]
    assert result["resolved_from"] == "search"
    assert result["article"]["title"] == "UIHANDLER OnKeyDown"
    match = result["resolution"]["match"]
    assert match["id"] == "UIHANDLER OnKeyDown"
    assert "all_terms_match" in match["ranking"]["match_reasons"]
    # The runner-up is kept in the payload, and carries no reason tying it to the query.
    runner_up = result["resolution"]["candidates"][1]
    assert runner_up["id"] == "UIHANDLER OnEvent"
    assert runner_up["ranking"]["match_reasons"] == ["upstream_rank_2", "intent_programming", "family_ui_handler"]


class _UnrelatedRowsClient:
    """MediaWiki answers a nonsense event name with pages that do not carry it in their title."""

    def __init__(self, handler_snippet: str) -> None:
        self._handler_snippet = handler_snippet
        self.fetched: list[str] = []

    def fetch_article_page(self, ref: str) -> dict[str, object]:
        self.fetched.append(ref)
        if ref == "UIHANDLER OnEvent":
            # A real, fetchable handler page stands ready: if the confidence gate lets the row
            # through, the surface answers ok:true with this page instead of failing.
            return _ui_handler_payload()
        raise WarcraftWikiAPIError("missingtitle", "The page you specified doesn't exist.")

    def search_articles(self, query: str, limit: int) -> tuple[int, list[dict[str, Any]]]:
        return 3, [
            {"title": "UIHANDLER OnEvent", "pageid": 1, "snippet": self._handler_snippet,
                "url": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnEvent"},
            {"title": "Jaina Proudmoore", "pageid": 2, "snippet": "Archmage of the Kirin Tor.",
                "url": "https://warcraft.wiki.gg/wiki/Jaina_Proudmoore"},
            {"title": "Mage", "pageid": 3, "snippet": "Class overview.", "url": "https://warcraft.wiki.gg/wiki/Mage"},
        ]


@pytest.mark.parametrize(
    "handler_snippet",
    [
        # bb-2 in its residual form: one allowed-family row survives the family filter on upstream
        # rank, family bonus and the stray word "event" in the query.
        pytest.param("Fires when a frame receives an event.", id="snippet_ignores_the_query"),
        # The narrower residual: MediaWiki's snippet quotes the queried name, which earns
        # all_terms_match + snippet_match and used to make this lone row "confident". The handler
        # page only talks about the event; it is not the event's page.
        pytest.param("Fires for zzz_not_an_event and other events.", id="snippet_quotes_the_query"),
    ],
)
def test_event_payload_fails_not_found_when_no_candidate_title_names_the_query(handler_snippet: str) -> None:
    client = _UnrelatedRowsClient(handler_snippet)

    with pytest.raises(ProviderError) as excinfo:
        _typed_article_payload(client, "ZZZ_NOT_AN_EVENT", surface="event", full=False)

    assert excinfo.value.code == "not_found"
    assert excinfo.value.exit_code == 4
    assert excinfo.value.details["candidates"] == ["UIHANDLER OnEvent"]
    # The unrelated page was never fetched for output, only probed as a direct title.
    assert "UIHANDLER OnEvent" not in client.fetched


class _LongerFunctionRowClient:
    """The typed name has no page of its own, and search offers one function whose name extends it."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def fetch_article_page(self, ref: str) -> dict[str, object]:
        self.fetched.append(ref)
        if ref == "API UnitHealthMax":
            # A real, fetchable API page: if the floor lets the row through, the surface answers
            # ok:true with the wrong function instead of failing.
            return _api_payload()
        raise WarcraftWikiAPIError("missingtitle", "The page you specified doesn't exist.")

    def search_articles(self, query: str, limit: int) -> tuple[int, list[dict[str, Any]]]:
        return 1, [
            {"title": "API UnitHealthMax", "pageid": 1, "snippet": "Returns the maximum health of a unit.",
                "url": "https://warcraft.wiki.gg/wiki/API_UnitHealthMax"},
        ]


def test_api_payload_fails_not_found_when_the_only_row_names_a_longer_function() -> None:
    client = _LongerFunctionRowClient()

    with pytest.raises(ProviderError) as excinfo:
        _typed_article_payload(client, "UnitHealth", surface="api", full=False)

    assert excinfo.value.code == "not_found"
    assert excinfo.value.exit_code == 4
    assert excinfo.value.details["candidates"] == ["API UnitHealthMax"]
    # The longer function's page was never fetched for output, only probed as a direct title.
    assert "API UnitHealthMax" not in client.fetched


def test_event_payload_rejects_a_search_hit_whose_page_is_the_wrong_family() -> None:
    # The ranker liked the title, but the fetched page is not an event reference: fail, never return it.
    client = _SearchFallbackClient(_ui_handler_payload(content_family="lore_reference"))

    with pytest.raises(ProviderError) as excinfo:
        _typed_article_payload(client, "key down handler", surface="event", full=False)

    assert excinfo.value.code == "not_found"
    assert excinfo.value.exit_code == 4


def test_warcraft_wiki_article_and_export(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page", lambda self, article_ref: _page_payload())

    article_result = runner.invoke(warcraft_wiki_app, ["article", "World of Warcraft API"])
    assert article_result.exit_code == 0
    article_payload = json.loads(article_result.stdout)["data"]
    assert article_payload["article"]["title"] == "World of Warcraft API"
    assert article_payload["content"]["section_count"] == 1
    assert article_payload["reference"]["content_family"] == "framework_page"
    assert article_payload["reference"]["programming_reference"] is True

    export_dir = tmp_path / "wiki-article"
    export_result = runner.invoke(warcraft_wiki_app, ["article-export", "World of Warcraft API", "--out", str(export_dir)])
    assert export_result.exit_code == 0
    export_payload = json.loads(export_result.stdout)["data"]
    assert export_payload["article"]["slug"] == "world-of-warcraft-api"
    assert export_payload["counts"]["sections"] == 1
    manifest = json.loads((export_dir / "manifest.json").read_text())
    assert datetime.fromisoformat(manifest["exported_at"].replace("Z", "+00:00")).tzinfo is not None

    article_full_result = runner.invoke(warcraft_wiki_app, ["article-full", "World of Warcraft API"])
    assert article_full_result.exit_code == 0
    article_full_payload = json.loads(article_full_result.stdout)["data"]
    assert article_full_payload["reference"]["content_family"] == "framework_page"
    assert article_full_payload["pages"][0]["reference"]["programming_reference"] is True


def test_warcraft_wiki_api_and_event_commands(monkeypatch) -> None:
    def fake_fetch(self: object, article_ref: str) -> dict[str, object]:
        normalized = str(article_ref)
        if "OnKeyDown" in normalized:
            return _ui_handler_payload()
        return _api_payload()

    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page", fake_fetch)
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            2,
            [
                {"title": "API CreateFrame", "pageid": 1, "snippet": "Creates a Frame object.",
                    "url": "https://warcraft.wiki.gg/wiki/API_CreateFrame"},
                {"title": "UIHANDLER OnKeyDown", "pageid": 2, "snippet": "Fires when a key is pressed.",
                    "url": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnKeyDown"},
            ],
        ),
    )

    api_result = runner.invoke(warcraft_wiki_app, ["api", "CreateFrame"])
    assert api_result.exit_code == 0
    api_payload = json.loads(api_result.stdout)["data"]
    assert api_payload["article"]["title"] == "API CreateFrame"
    assert api_payload["resolved_surface"] == "api"
    assert api_payload["resolved_from"] == "direct_fetch"

    api_full_result = runner.invoke(warcraft_wiki_app, ["api-full", "CreateFrame"])
    assert api_full_result.exit_code == 0
    api_full_payload = json.loads(api_full_result.stdout)["data"]
    assert api_full_payload["article"]["title"] == "API CreateFrame"
    assert api_full_payload["resolved_surface"] == "api"

    event_result = runner.invoke(warcraft_wiki_app, ["event", "OnKeyDown"])
    assert event_result.exit_code == 0
    event_payload = json.loads(event_result.stdout)["data"]
    assert event_payload["article"]["title"] == "UIHANDLER OnKeyDown"
    assert event_payload["article"]["content_family"] == "ui_handler"
    assert event_payload["resolved_surface"] == "event"

    event_full_result = runner.invoke(warcraft_wiki_app, ["event-full", "OnKeyDown"])
    assert event_full_result.exit_code == 0
    event_full_payload = json.loads(event_full_result.stdout)["data"]
    assert event_full_payload["article"]["title"] == "UIHANDLER OnKeyDown"
    assert event_full_payload["reference"]["content_family"] == "ui_handler"


def test_warcraft_wiki_typed_commands_reject_wrong_family(monkeypatch) -> None:
    lore_payload = {
        "article": {
            "title": "Jaina Proudmoore",
            "slug": "jaina-proudmoore",
            "display_title": "Jaina Proudmoore",
            "page_url": "https://warcraft.wiki.gg/wiki/Jaina_Proudmoore",
            "section_slug": "jaina-proudmoore",
            "section_title": "Jaina Proudmoore",
            "page_count": 1,
            "content_family": "lore_reference",
        },
        "page": {
            "title": "Jaina Proudmoore",
            "description": "Lore page.",
            "canonical_url": "https://warcraft.wiki.gg/wiki/Jaina_Proudmoore",
        },
        "navigation": {"count": 0, "items": []},
        "article_content": {
            "html": "<p>Archmage of the Kirin Tor.</p>",
            "text": "Archmage of the Kirin Tor.",
            "headings": [],
            "sections": [{"title": "Introduction", "level": 1, "ordinal": 1, "anchor": "Introduction", "text": "Archmage of the Kirin Tor.", "html": "<p>Archmage of the Kirin Tor.</p>"}],
        },
        "reference": {"content_family": "lore_reference", "summary": "Archmage of the Kirin Tor."},
        "linked_entities": [],
        "citations": {"page": "https://warcraft.wiki.gg/wiki/Jaina_Proudmoore"},
    }
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page",
        lambda self, article_ref: lore_payload,
    )
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            1,
            [
                {
                    "title": "Jaina Proudmoore",
                    "pageid": 1,
                    "snippet": "Lore reference page.",
                    "url": "https://warcraft.wiki.gg/wiki/Jaina_Proudmoore",
                }
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["event", "Jaina Proudmoore"])
    assert result.exit_code == 4

    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["details"]["surface"] == "event"
    assert result.stdout == ""


def test_warcraft_wiki_api_command_supports_framework_pages(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page",
        lambda self, article_ref: _page_payload(),
    )
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            2,
            [
                {
                    "title": "World of Warcraft API",
                    "pageid": 1,
                    "snippet": "Framework overview.",
                    "url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
                },
                {
                    "title": "API CreateFrame",
                    "pageid": 2,
                    "snippet": "Function reference.",
                    "url": "https://warcraft.wiki.gg/wiki/API_CreateFrame",
                },
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["api", "World of Warcraft API"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["article"]["title"] == "World of Warcraft API"
    assert payload["data"]["article"]["content_family"] == "framework_page"
    assert payload["data"]["resolved_surface"] == "api"


def test_warcraft_wiki_article_query(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page", lambda self, article_ref: _page_payload())
    export_dir = tmp_path / "wiki-article"
    export_result = runner.invoke(warcraft_wiki_app, ["article-export", "World of Warcraft API", "--out", str(export_dir)])
    assert export_result.exit_code == 0

    query_result = runner.invoke(warcraft_wiki_app, ["article-query", str(export_dir), "framexml"])
    assert query_result.exit_code == 0
    payload = json.loads(query_result.stdout)
    assert payload["data"]["article"]["title"] == "World of Warcraft API"
    assert payload["data"]["match_counts"]["sections"] >= 1


def test_warcraft_wiki_search_prefers_api_page_for_function_query(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            3,
            [
                {"title": "API CreateFrame", "pageid": 1, "snippet": "Creates a Frame object.",
                    "url": "https://warcraft.wiki.gg/wiki/API_CreateFrame"},
                {"title": "Widget script handlers", "pageid": 2, "snippet": "OnClick and OnKeyDown handlers.",
                    "url": "https://warcraft.wiki.gg/wiki/Widget_script_handlers"},
                {"title": "Create a WoW AddOn in 15 Minutes", "pageid": 3, "snippet": "AddOn tutorial.",
                    "url": "https://warcraft.wiki.gg/wiki/Create_a_WoW_AddOn_in_15_Minutes"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "CreateFrame"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["match"]["id"] == "API CreateFrame"
    assert payload["data"]["next_command"] == "warcraft-wiki article 'API CreateFrame'"


def test_warcraft_wiki_search_prefers_api_changes_page_for_patch_query(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            3,
            [
                {"title": "API change summaries/Historical", "pageid": 1, "snippet": "Summary of older API changes.",
                    "url": "https://warcraft.wiki.gg/wiki/API_change_summaries/Historical"},
                {"title": "Patch 2.1.0/API changes", "pageid": 2, "snippet": "Changes in patch 2.1.0.",
                    "url": "https://warcraft.wiki.gg/wiki/Patch_2.1.0/API_changes"},
                {"title": "Hyperlinks", "pageid": 3, "snippet": "Programming reference.", "url": "https://warcraft.wiki.gg/wiki/Hyperlinks"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "patch 2.1.0 api changes"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["match"]["id"] == "Patch 2.1.0/API changes"
    assert payload["data"]["match"]["metadata"]["content_family"] == "api_changes"


def test_warcraft_wiki_search_prefers_handler_page_for_handler_query(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            3,
            [
                {"title": "Widget script handlers", "pageid": 1, "snippet": "OnClick and OnKeyDown handlers.",
                    "url": "https://warcraft.wiki.gg/wiki/Widget_script_handlers"},
                {"title": "UIHANDLER OnKeyDown", "pageid": 2, "snippet": "Fires when a key is pressed.",
                    "url": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnKeyDown"},
                {"title": "OnUpdate", "pageid": 3, "snippet": "Widget update handler.",
                    "url": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnUpdate"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "OnKeyDown"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["match"]["id"] == "UIHANDLER OnKeyDown"
    assert payload["data"]["match"]["metadata"]["content_family"] == "ui_handler"


def test_warcraft_wiki_search_prefers_system_reference_for_system_query(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            3,
            [
                {"title": "Renown", "pageid": 1, "snippet": "Reputation-like progression system.", "url": "https://warcraft.wiki.gg/wiki/Renown"},
                {"title": "Expansion", "pageid": 2, "snippet": "Game expansion overview.", "url": "https://warcraft.wiki.gg/wiki/Expansion"},
                {"title": "World of Warcraft API", "pageid": 3, "snippet": "Programming reference.",
                    "url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["search", "renown"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["results"][0]["id"] == "Renown"
    assert payload["data"]["results"][0]["metadata"]["content_family"] == "system_reference"


def test_warcraft_wiki_search_excludes_family_hint_terms(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            2,
            [
                {"title": "Argent Dawn", "pageid": 1, "snippet": "The Argent Dawn is a faction.", "url": "https://warcraft.wiki.gg/wiki/Argent_Dawn"},
                {"title": "Faction", "pageid": 2, "snippet": "General faction article.", "url": "https://warcraft.wiki.gg/wiki/Faction"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["search", "faction argent dawn"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["search_query"] == "argent dawn"
    assert payload["data"]["excluded_terms"] == ["faction"]
    assert payload["data"]["normalization_hint"] == "excluded_family_hint_terms"
    assert payload["data"]["results"][0]["id"] == "Argent Dawn"


def test_warcraft_wiki_search_keeps_trailing_guide_term(monkeypatch) -> None:
    seen_queries: list[str] = []

    def fake_search(self: object, query: str, limit: int) -> tuple[int, list[dict[str, Any]]]:
        seen_queries.append(query)
        return (
            1,
            [
                {"title": "Mistweaver Monk PvE Healing Guide", "pageid": 1, "snippet": "Guide page.",
                    "url": "https://warcraft.wiki.gg/wiki/Mistweaver_Monk_PvE_Healing_Guide"},
            ],
        )

    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", fake_search)

    result = runner.invoke(warcraft_wiki_app, ["search", "mistweaver monk guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert seen_queries == ["mistweaver monk guide"]
    assert payload["data"]["search_query"] == "mistweaver monk guide"
    assert "excluded_terms" not in payload["data"]


def test_warcraft_wiki_resolve_prefers_lore_result_after_hint_cleanup(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            2,
            [
                {"title": "Jaina Proudmoore", "pageid": 1, "snippet": "Leader of the Kirin Tor.",
                    "url": "https://warcraft.wiki.gg/wiki/Jaina_Proudmoore"},
                {"title": "Jaina Proudmoore: Tides of War", "pageid": 2, "snippet": "Novel.",
                    "url": "https://warcraft.wiki.gg/wiki/Jaina_Proudmoore:_Tides_of_War"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "lore jaina proudmoore"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["search_query"] == "jaina proudmoore"
    assert payload["data"]["excluded_terms"] == ["lore"]
    assert payload["data"]["resolved"] is True
    assert payload["data"]["match"]["id"] == "Jaina Proudmoore"


def test_warcraft_wiki_search_prefers_programming_howto_for_addon_query(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            3,
            [
                {"title": "Create a WoW AddOn in 15 Minutes", "pageid": 1, "snippet": "This guide describes how to make a simple HelloWorld addon.",
                    "url": "https://warcraft.wiki.gg/wiki/Create_a_WoW_AddOn_in_15_Minutes"},
                {"title": "Druid", "pageid": 2, "snippet": "A shapeshifting class.", "url": "https://warcraft.wiki.gg/wiki/Druid"},
                {"title": "World of Warcraft API", "pageid": 3, "snippet": "Programming reference.",
                    "url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "create addon"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["match"]["id"] == "Create a WoW AddOn in 15 Minutes"
    assert payload["data"]["match"]["metadata"]["content_family"] == "howto_programming"


def test_warcraft_wiki_search_prefers_specific_programming_guide_title(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            3,
            [
                {"title": "HOWTOs", "pageid": 1, "snippet": "Programming howto index.", "url": "https://warcraft.wiki.gg/wiki/HOWTOs"},
                {"title": "User interface", "pageid": 2, "snippet": "General UI page.", "url": "https://warcraft.wiki.gg/wiki/User_interface"},
                {"title": "User interface customization guide", "pageid": 3, "snippet": "Customize the WoW user interface.",
                    "url": "https://warcraft.wiki.gg/wiki/User_interface_customization_guide"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "guide interface customization"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["search_query"] == "interface customization"
    assert payload["data"]["excluded_terms"] == ["guide"]
    assert payload["data"]["resolved"] is True
    assert payload["data"]["match"]["id"] == "User interface customization guide"
    assert payload["data"]["match"]["metadata"]["content_family"] == "howto_programming"


def _connect_error(*_args, **_kwargs):
    raise httpx.ConnectError("connection refused", request=httpx.Request("GET", "https://warcraft.wiki.gg/api.php"))


@pytest.mark.parametrize(
    "args",
    [
        ["search", "createframe"],
        ["resolve", "createframe"],
        ["article", "API CreateFrame"],
        ["article-full", "API CreateFrame"],
        ["api", "CreateFrame"],
        ["api-full", "CreateFrame"],
        ["event", "OnKeyDown"],
        ["event-full", "OnKeyDown"],
        ["article-export", "API CreateFrame"],
    ],
)
def test_warcraft_wiki_transport_failure_returns_error_envelope(monkeypatch, args) -> None:
    monkeypatch.setattr("warcraft_wiki_cli.client.request_with_retries", _connect_error)

    result = runner.invoke(warcraft_wiki_app, args)

    assert result.exit_code == 5
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert set(payload) == ENVELOPE_KEYS
    assert payload["ok"] is False
    assert payload["provider"] == "warcraft-wiki"
    assert payload["schema_version"] == "1"
    assert payload["error"]["code"] == "network_error"


def test_warcraft_wiki_missing_article_exits_not_found(monkeypatch) -> None:
    # MediaWiki answers an unknown page with HTTP 200 and an error body (confirmed live against
    # warcraft.wiki.gg), so the transport is stubbed rather than the client: nothing between the
    # response body and the exit code is faked.
    transport = _CapturedTransport({"error": {"code": "missingtitle", "info": "The page you specified doesn't exist."}})
    monkeypatch.setattr("warcraft_wiki_cli.client.request_with_retries", transport)

    result = runner.invoke(warcraft_wiki_app, ["article", "No Such Page"])

    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "The page you specified doesn't exist."


def test_warcraft_wiki_article_query_missing_bundle_is_not_found(tmp_path) -> None:
    # Same answer as icy-veins and method: the shared bundle loader owns this check.
    result = runner.invoke(warcraft_wiki_app, ["article-query", str(tmp_path / "absent"), "framexml"])

    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"


def test_warcraft_wiki_article_query_unsupported_kind_is_a_usage_error(tmp_path) -> None:
    result = runner.invoke(warcraft_wiki_app, ["article-query", str(tmp_path), "framexml", "--kind", "pages"])

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize(
    "args",
    [
        ["doctor"],
        ["search", "world of warcraft api"],
        ["resolve", "world of warcraft api"],
        ["article", "World of Warcraft API"],
        ["api", "World of Warcraft API"],
    ],
)
def test_warcraft_wiki_payloads_conform_to_envelope(monkeypatch, args) -> None:
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page", lambda self, article_ref: _page_payload())
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            1,
            [
                {
                    "title": "World of Warcraft API",
                    "pageid": 1,
                    "snippet": "API systems and FrameXML.",
                    "url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
                }
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, args)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert envelope_violations(payload) == []
    assert set(payload) == REQUIRED_KEYS
    assert payload["provider"] == "warcraft-wiki"
    assert payload["schema_version"] == "1"
