from __future__ import annotations

import json
from datetime import datetime

from typer.testing import CliRunner
from warcraft_wiki_cli.client import WarcraftWikiAPIError
from warcraft_wiki_cli.main import (
    _score_text_match,
    _typed_allowed_families,
    _typed_article_payload,
    _typed_direct_article_result,
    _typed_search_match,
    _typed_search_queries,
)
from warcraft_wiki_cli.main import (
    app as warcraft_wiki_app,
)

runner = CliRunner()


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


def _ui_handler_payload() -> dict[str, object]:
    return {
        "article": {
            "title": "UIHANDLER OnKeyDown",
            "slug": "uihandler-onkeydown",
            "display_title": "UIHANDLER OnKeyDown",
            "page_url": "https://warcraft.wiki.gg/wiki/UIHANDLER_OnKeyDown",
            "section_slug": "uihandler-onkeydown",
            "section_title": "UIHANDLER OnKeyDown",
            "page_count": 1,
            "content_family": "ui_handler",
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
            "content_family": "ui_handler",
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
    assert payload["capabilities"]["search"] == "ready"
    assert payload["capabilities"]["api"] == "ready"
    assert payload["capabilities"]["event"] == "ready"
    assert payload["capabilities"]["article_query"] == "ready"


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
    search_payload = json.loads(search_result.stdout)
    assert search_payload["results"][0]["entity_type"] == "article"
    assert search_payload["results"][0]["metadata"]["content_family"] == "framework_page"

    resolve_result = runner.invoke(warcraft_wiki_app, ["resolve", "world of warcraft api"])
    assert resolve_result.exit_code == 0
    resolve_payload = json.loads(resolve_result.stdout)
    assert resolve_payload["resolved"] is True
    assert resolve_payload["next_command"] == "warcraft-wiki article 'World of Warcraft API'"


def test_warcraft_wiki_score_text_match_boosts_programming_pages() -> None:
    score, reasons, family = _score_text_match(
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


def test_warcraft_wiki_score_text_match_handles_expansion_alias() -> None:
    score, reasons, family = _score_text_match(
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
    assert _typed_search_queries("OnKeyDown", surface="event") == [
        "OnKeyDown",
        "UIHANDLER OnKeyDown",
        "event OnKeyDown",
    ]


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


def test_typed_search_match_requires_clear_winner() -> None:
    match = _typed_search_match(
        [
            {"id": "API_CreateFrame", "name": "API CreateFrame", "ranking": {"score": 70}},
            {"id": "API_CreateTexture", "name": "API CreateTexture", "ranking": {"score": 40}},
        ],
        query="CreateFrame",
        surface="api",
    )
    assert match["id"] == "API_CreateFrame"


def test_api_payload_prefers_direct_fetch_before_search() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.search_calls: list[str] = []

        def fetch_article_page(self, ref: str) -> dict[str, object]:
            if ref != "API CreateFrame":
                raise WarcraftWikiAPIError("invalid_article_ref", "not found")
            return _api_payload()

        def search_articles(self, query: str, limit: int):  # noqa: ANN001
            self.search_calls.append(query)
            return 0, []

    client = FakeClient()
    result = _typed_article_payload(client, "CreateFrame", surface="api", full=False)

    assert result["resolved_from"] == "direct_fetch"
    assert result["resolved_surface"] == "api"
    assert result["search_queries"] == ["CreateFrame", "API CreateFrame"]
    assert client.search_calls == []


def test_warcraft_wiki_article_and_export(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page", lambda self, article_ref: _page_payload())

    article_result = runner.invoke(warcraft_wiki_app, ["article", "World of Warcraft API"])
    assert article_result.exit_code == 0
    article_payload = json.loads(article_result.stdout)
    assert article_payload["article"]["title"] == "World of Warcraft API"
    assert article_payload["content"]["section_count"] == 1
    assert article_payload["reference"]["content_family"] == "framework_page"
    assert article_payload["reference"]["programming_reference"] is True

    export_dir = tmp_path / "wiki-article"
    export_result = runner.invoke(warcraft_wiki_app, ["article-export", "World of Warcraft API", "--out", str(export_dir)])
    assert export_result.exit_code == 0
    export_payload = json.loads(export_result.stdout)
    assert export_payload["article"]["slug"] == "world-of-warcraft-api"
    assert export_payload["counts"]["sections"] == 1
    manifest = json.loads((export_dir / "manifest.json").read_text())
    assert datetime.fromisoformat(manifest["exported_at"].replace("Z", "+00:00")).tzinfo is not None

    article_full_result = runner.invoke(warcraft_wiki_app, ["article-full", "World of Warcraft API"])
    assert article_full_result.exit_code == 0
    article_full_payload = json.loads(article_full_result.stdout)
    assert article_full_payload["reference"]["content_family"] == "framework_page"
    assert article_full_payload["pages"][0]["reference"]["programming_reference"] is True


def test_warcraft_wiki_api_and_event_commands(monkeypatch) -> None:
    def fake_fetch(self, article_ref):  # noqa: ANN001
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
    api_payload = json.loads(api_result.stdout)
    assert api_payload["article"]["title"] == "API CreateFrame"
    assert api_payload["resolved_surface"] == "api"
    assert api_payload["resolved_from"] == "direct_fetch"

    api_full_result = runner.invoke(warcraft_wiki_app, ["api-full", "CreateFrame"])
    assert api_full_result.exit_code == 0
    api_full_payload = json.loads(api_full_result.stdout)
    assert api_full_payload["article"]["title"] == "API CreateFrame"
    assert api_full_payload["resolved_surface"] == "api"

    event_result = runner.invoke(warcraft_wiki_app, ["event", "OnKeyDown"])
    assert event_result.exit_code == 0
    event_payload = json.loads(event_result.stdout)
    assert event_payload["article"]["title"] == "UIHANDLER OnKeyDown"
    assert event_payload["article"]["content_family"] == "ui_handler"
    assert event_payload["resolved_surface"] == "event"

    event_full_result = runner.invoke(warcraft_wiki_app, ["event-full", "OnKeyDown"])
    assert event_full_result.exit_code == 0
    event_full_payload = json.loads(event_full_result.stdout)
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
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_event_ref"


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
    assert payload["article"]["title"] == "World of Warcraft API"
    assert payload["article"]["content_family"] == "framework_page"
    assert payload["resolved_surface"] == "api"


def test_warcraft_wiki_article_query(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page", lambda self, article_ref: _page_payload())
    export_dir = tmp_path / "wiki-article"
    export_result = runner.invoke(warcraft_wiki_app, ["article-export", "World of Warcraft API", "--out", str(export_dir)])
    assert export_result.exit_code == 0

    query_result = runner.invoke(warcraft_wiki_app, ["article-query", str(export_dir), "framexml"])
    assert query_result.exit_code == 0
    payload = json.loads(query_result.stdout)
    assert payload["article"]["title"] == "World of Warcraft API"
    assert payload["match_counts"]["sections"] >= 1


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
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "API CreateFrame"
    assert payload["next_command"] == "warcraft-wiki article 'API CreateFrame'"


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
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "Patch 2.1.0/API changes"
    assert payload["match"]["metadata"]["content_family"] == "api_changes"


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
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "UIHANDLER OnKeyDown"
    assert payload["match"]["metadata"]["content_family"] == "ui_handler"


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
    assert payload["results"][0]["id"] == "Renown"
    assert payload["results"][0]["metadata"]["content_family"] == "system_reference"


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
    assert payload["search_query"] == "argent dawn"
    assert payload["excluded_terms"] == ["faction"]
    assert payload["normalization_hint"] == "excluded_family_hint_terms"
    assert payload["results"][0]["id"] == "Argent Dawn"


def test_warcraft_wiki_search_keeps_trailing_guide_term(monkeypatch) -> None:
    seen_queries: list[str] = []

    def fake_search(self, query, limit):  # noqa: ANN001
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
    assert payload["search_query"] == "mistweaver monk guide"
    assert "excluded_terms" not in payload


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
    assert payload["search_query"] == "jaina proudmoore"
    assert payload["excluded_terms"] == ["lore"]
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "Jaina Proudmoore"


def test_warcraft_wiki_search_excludes_zone_hint_terms(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            2,
            [
                {"title": "Elwynn Forest", "pageid": 1, "snippet": "Alliance starting zone in the Eastern Kingdoms.",
                    "url": "https://warcraft.wiki.gg/wiki/Elwynn_Forest"},
                {"title": "Elwyn", "pageid": 2, "snippet": "Separate article.", "url": "https://warcraft.wiki.gg/wiki/Elwyn"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "zone elwynn forest"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["search_query"] == "elwynn forest"
    assert payload["excluded_terms"] == ["zone"]
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "Elwynn Forest"


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
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "Create a WoW AddOn in 15 Minutes"
    assert payload["match"]["metadata"]["content_family"] == "howto_programming"


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
    assert payload["search_query"] == "interface customization"
    assert payload["excluded_terms"] == ["guide"]
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "User interface customization guide"
    assert payload["match"]["metadata"]["content_family"] == "howto_programming"


def test_warcraft_wiki_search_excludes_profession_hint_terms(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            2,
            [
                {"title": "Alchemy", "pageid": 1, "snippet": "Primary profession page.", "url": "https://warcraft.wiki.gg/wiki/Alchemy"},
                {"title": "Profession", "pageid": 2, "snippet": "General profession system page.", "url": "https://warcraft.wiki.gg/wiki/Profession"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "profession alchemy"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["search_query"] == "alchemy"
    assert payload["excluded_terms"] == ["profession"]
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "Alchemy"


def test_warcraft_wiki_search_excludes_class_hint_terms(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            2,
            [
                {"title": "Druid", "pageid": 1, "snippet": "Playable class page.", "url": "https://warcraft.wiki.gg/wiki/Druid"},
                {"title": "Rejuvenation", "pageid": 2, "snippet": "Druid ability.", "url": "https://warcraft.wiki.gg/wiki/Rejuvenation"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "class druid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["search_query"] == "druid"
    assert payload["excluded_terms"] == ["class"]
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "Druid"
    assert payload["match"]["metadata"]["content_family"] == "class_reference"


def test_warcraft_wiki_search_excludes_expansion_hint_terms(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (
            3,
            [
                {"title": "Legion Invasions", "pageid": 1, "snippet": "Legion world events.",
                    "url": "https://warcraft.wiki.gg/wiki/Legion_Invasions"},
                {"title": "World of Warcraft: Legion", "pageid": 2, "snippet": "The sixth WoW expansion.",
                    "url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft:_Legion"},
                {"title": "Burning Legion", "pageid": 3, "snippet": "Demonic army.", "url": "https://warcraft.wiki.gg/wiki/Burning_Legion"},
            ],
        ),
    )

    result = runner.invoke(warcraft_wiki_app, ["resolve", "expansion legion"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["search_query"] == "legion"
    assert payload["excluded_terms"] == ["expansion"]
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "World of Warcraft: Legion"
    assert payload["match"]["metadata"]["content_family"] == "expansion_reference"
