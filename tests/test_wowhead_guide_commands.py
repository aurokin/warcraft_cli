"""Guide fetch, export, and query commands for the wowhead CLI."""

from __future__ import annotations

import json
import shlex
import shutil
from datetime import UTC, datetime
from pathlib import Path

from warcraft_content.article_bundle import compare_article_bundles, load_article_bundle
from wowhead_cli.expansion_profiles import resolve_expansion
from wowhead_cli.guides import (
    GuideCategoryFilters,
    GuideExportOptions,
    GuideHydrationResult,
    filtered_guide_category_rows,
    guide_comment_matches,
    guide_export_manifest,
    guide_gatherer_matches,
    guide_linked_entity_matches,
    guide_navigation_matches,
    guide_query_match_sort_key,
    guide_query_top_matches,
    guide_row_matches_filters,
    guide_section_matches,
    guides_payload,
    validated_guides_filters,
    write_guide_export_assets,
)
from wowhead_cli.main import app
from wowhead_cli.wowhead_client import WowheadClient

from tests.wowhead_testkit import SAMPLE_GUIDE_HTML, runner


def test_guide_section_matches_applies_section_title_filter() -> None:
    matches = guide_section_matches(
        sections=[
            {"title": "Frost Death Knight Overview", "content_text": "Welcome to the guide.", "ordinal": 1, "level": 2},
            {"title": "BiS Gear", "content_text": "Use high item level gear.", "ordinal": 2, "level": 2},
        ],
        query="welcome",
        section_title_filter="overview",
    )

    assert len(matches) == 1
    assert matches[0]["title"] == "Frost Death Knight Overview"



def test_guide_linked_entity_matches_respects_source_filter() -> None:
    matches = guide_linked_entity_matches(
        linked_entities=[
            {
                "entity_type": "item",
                "id": 249277,
                "name": "Bellamy's Final Judgement",
                "url": "https://www.wowhead.com/item=249277",
                "citation_url": "https://www.wowhead.com/guide=3143",
                "sources": ["href", "gatherer"],
            },
            {
                "entity_type": "spell",
                "id": 49020,
                "name": "Obliterate",
                "url": "https://www.wowhead.com/spell=49020",
                "citation_url": "https://www.wowhead.com/guide=3143",
                "sources": ["href"],
            },
        ],
        query="bellamy",
        selected_link_sources=("multi",),
    )

    assert len(matches) == 1
    assert matches[0]["name"] == "Bellamy's Final Judgement"
    assert matches[0]["sources"] == ["gatherer", "href"]



def test_guide_navigation_gatherer_and_comment_matches_build_expected_shapes() -> None:
    navigation_matches = guide_navigation_matches(
        navigation_links=[{"label": "BiS Gear", "url": "https://www.wowhead.com/guide/bis", "source_url": None}],
        query="bis",
        page_url="https://www.wowhead.com/guide=3143",
    )
    assert navigation_matches[0]["kind"] == "navigation"
    assert navigation_matches[0]["citation_url"] == "https://www.wowhead.com/guide=3143"

    gatherer_matches = guide_gatherer_matches(
        gatherer_entities=[
            {
                "entity_type": "item",
                "id": 249277,
                "name": "Bellamy's Final Judgement",
                "url": "https://www.wowhead.com/item=249277",
                "citation_url": "https://www.wowhead.com/guide=3143",
            }
        ],
        query="bellamy",
    )
    assert gatherer_matches[0]["kind"] == "gatherer_entity"

    comment_matches = guide_comment_matches(
        comments=[{"id": 91, "user": "A", "body": "Solid guide", "citation_url": "https://www.wowhead.com/guide=3143#comments"}],
        query="solid",
    )
    assert comment_matches[0]["kind"] == "comment"
    assert comment_matches[0]["user"] == "A"



def test_guide_query_top_matches_dedupes_entity_results_across_groups() -> None:
    top = guide_query_top_matches(
        match_groups=[
            [],
            [],
            [{"kind": "linked_entity", "score": 50, "entity_type": "spell", "id": 49020, "name": "Obliterate"}],
            [{"kind": "gatherer_entity", "score": 48, "entity_type": "spell", "id": 49020, "name": "Obliterate"}],
            [],
        ],
        limit=5,
    )

    assert len(top) == 1
    assert top[0]["kind"] == "linked_entity"



def test_validated_guides_filters_normalizes_and_rejects_invalid_ranges() -> None:
    category, filters = validated_guides_filters(
        category=" classes/ ",
        author=["Khazakdk,Another"],
        updated_after="2026-02-01",
        updated_before="2026-03-01",
        patch_min=1,
        patch_max=2,
        sort_by="updated",
    )

    assert category == "classes"
    assert filters.authors == ("khazakdk", "another")
    assert filters.updated_after is not None
    assert filters.updated_before is not None

    try:
        validated_guides_filters(
            category="classes",
            author=[],
            updated_after="2026-03-01",
            updated_before="2026-02-01",
            patch_min=1,
            patch_max=2,
            sort_by="updated",
        )
    except ValueError as exc:
        assert "--updated-after must be <= --updated-before." in str(exc)
    else:
        raise AssertionError("expected invalid date range")



def test_guide_row_matches_filters_and_filtered_rows() -> None:
    normalized_row = {
        "id": 32000,
        "title": "Frost Death Knight DPS Guide",
        "name": "Frost Death Knight DPS Guide - Midnight",
        "author": "Khazakdk",
        "last_updated": "2026-02-25T17:32:29+00:00",
        "patch": 120001,
        "category_path": "classes/death-knight/frost",
    }

    filters = GuideCategoryFilters(
        authors=("khazakdk",),
        updated_after=datetime(2026, 2, 1, tzinfo=UTC),
        updated_before=datetime(2026, 3, 1, tzinfo=UTC),
        patch_min=120000,
        patch_max=120001,
        sort_by="relevance",
    )

    assert guide_row_matches_filters(normalized_row, filters=filters) is True

    filtered = filtered_guide_category_rows(
        [
            {
                "id": 32000,
                "title": "Frost Death Knight DPS Guide",
                "name": "Frost Death Knight DPS Guide - Midnight",
                "url": "/guide/classes/death-knight/frost/overview-pve-dps",
                "categoryPath": "classes/death-knight/frost",
                "author": "Khazakdk",
                "lastEdit": "2026-02-25T17:32:29+00:00",
                "patch": 120001,
                "rating": 4.6,
            }
        ],
        query_text="death knight",
        filters=filters,
    )
    assert len(filtered) == 1
    assert filtered[0]["match_score"] > 0



def test_guides_payload_builds_expected_filters_and_facets() -> None:
    payload = guides_payload(
        expansion=resolve_expansion(None),
        category="classes",
        query="death knight",
        filters=GuideCategoryFilters(
            authors=("khazakdk",),
            updated_after=datetime(2026, 2, 1, tzinfo=UTC),
            updated_before=None,
            patch_min=120001,
            patch_max=None,
            sort_by="relevance",
        ),
        normalized_rows=[
            {
                "id": 32000,
                "author": "Khazakdk",
                "category_path": "classes/death-knight/frost",
                "title": "Frost Death Knight DPS Guide",
            }
        ],
        limit=5,
    )

    assert payload["guides_url"].endswith("/guides/classes")
    assert payload["filters"]["authors"] == ["khazakdk"]
    assert payload["facets"]["authors"] == ["Khazakdk"]



def test_write_guide_export_assets_and_manifest_helpers(tmp_path: Path) -> None:
    payload = {
        "expansion": "retail",
        "guide": {"id": 3143, "title": "Frost Death Knight DPS Guide"},
        "page": {"title": "Frost Death Knight DPS Guide", "canonical_url": "https://www.wowhead.com/guide=3143"},
        "body": {"raw_markup": "[h2]Overview[/h2]", "section_chunks": [{"title": "Overview", "ordinal": 1}]},
        "navigation": {"raw_markup": "[ul][li]Overview[/li][/ul]", "links": [{"label": "Overview", "url": "https://www.wowhead.com/guide=3143#overview"}]},
        "linked_entities": {"items": [{"entity_type": "spell", "id": 49020, "name": "Obliterate"}]},
        "gatherer_entities": {"items": [{"entity_type": "item", "id": 249277, "name": "Bellamy's Final Judgement"}]},
        "comments": {"items": [{"id": 91, "user": "A", "body": "Solid guide"}]},
        "analysis_surfaces": {"items": [{"surface_tags": ["overview"], "section_title": "Overview"}]},
        "structured_data": {"@type": "Article"},
    }

    files_written, assets = write_guide_export_assets(
        export_dir=tmp_path,
        payload=payload,
        html="<html></html>",
    )
    assert files_written["guide_json"] == "guide.json"
    assert files_written["structured_data_json"] == "structured-data.json"
    assert len(assets.sections) == 1
    assert len(assets.linked_items) == 1

    manifest = guide_export_manifest(
        export_dir=tmp_path,
        payload=payload,
        options=GuideExportOptions(
            guide_ref="3143",
            max_links=10,
            include_replies=False,
            hydrate_linked_entities=True,
            hydrate_types=("spell",),
            hydrate_limit=5,
        ),
        assets=assets,
        hydration=GuideHydrationResult(
            items=[{"entity_type": "spell", "id": 49020, "storage_source": "entity_cache"}],
            hydrated_at="2026-03-14T00:00:00+00:00",
            files_written={},
        ),
        files_written=files_written,
    )
    assert manifest["counts"]["sections"] == 1
    assert manifest["counts"]["analysis_surfaces"] == 1
    assert manifest["counts"]["hydrated_entities"] == 1
    assert manifest["hydration"]["source_counts"]["entity_cache"] == 1



def test_guide_command_supports_id_lookup(monkeypatch) -> None:
    calls = []

    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        calls.append(guide_id)
        return SAMPLE_GUIDE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    result = runner.invoke(app, ["--expansion", "wotlk", "guide", "3143", "--comment-sample", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert calls == [3143]
    assert payload["data"]["guide"]["id"] == 3143
    assert payload["data"]["guide"]["lookup_url"] == "https://www.wowhead.com/wotlk/guide=3143"
    assert payload["data"]["guide"]["page_url"] == "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"
    assert payload["data"]["comments"]["count"] == 1
    assert payload["data"]["comments"]["top"][0]["citation_url"].endswith("#comments:id=91")
    assert payload["data"]["linked_entities"]["count"] >= 2
    assert payload["data"]["linked_entities"]["source_counts"] == {"href": 2, "gatherer": 1, "merged": 2}
    assert payload["data"]["linked_entities"]["items"][0]["url"]



def test_guide_follow_up_commands_carry_the_active_expansion(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.guide_page_html",
        lambda self, guide_id: SAMPLE_GUIDE_HTML,
    )
    result = runner.invoke(app, ["--expansion", "classic", "guide", "3143", "--comment-sample", "0"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["linked_entities"]["fetch_more_command"] == "wowhead --expansion classic guide-full 3143"
    assert data["analysis_surfaces"]["fetch_more_command"] == "wowhead --expansion classic guide-full 3143"



def _guide_html_with_many_links(*, href_count: int) -> str:
    """A guide whose href links alone fill any sane --max-links, with Gatherer records after them."""
    # "item" is a low-signal anchor label, so only a Gatherer record can name these links.
    links = "\n".join(f'<a href="/item={300000 + index}">item</a>' for index in range(href_count))
    last_href_id = 300000 + href_count - 1
    gatherer = (
        f'WH.Gatherer.addData(3, 1, {{"{last_href_id}":{{"name_enus":"Named Only By Gatherer"}}}});'
        'WH.Gatherer.addData(1, 1, {"249998":{"name_enus":"Gatherer Only Npc"}});'
    )
    return f"""
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/guide/test-guide">
    </head><body>
      {links}
      <script>{gatherer} var lv_comments0 = [];</script>
    </body></html>
    """


def test_guide_full_merges_gatherer_records_even_when_href_links_fill_the_limit(monkeypatch) -> None:
    html = _guide_html_with_many_links(href_count=250)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", lambda self, guide_id: html)
    result = runner.invoke(app, ["guide-full", "3143", "--max-links", "250"])
    assert result.exit_code == 0

    links = json.loads(result.stdout)["data"]["linked_entities"]
    assert links["source_counts"] == {"href": 250, "gatherer": 2, "merged": 251}
    # The last href link is enriched by its Gatherer record instead of the merge stopping short.
    enriched = next(row for row in links["items"] if row["id"] == 300249)
    assert enriched["name"] == "Named Only By Gatherer"
    assert enriched["sources"] == ["gatherer", "href"]
    # What the limit does cut off is reported, not silently dropped.
    assert links["count"] == len(links["items"]) == 250
    assert links["total"] == 251
    assert links["truncated"] is True


def test_guide_command_supports_full_wowhead_url(monkeypatch) -> None:
    calls = []

    def fake_page_html(self, page_url: str):  # noqa: ANN001
        calls.append(page_url)
        return SAMPLE_GUIDE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    guide_url = "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"
    result = runner.invoke(app, ["guide", guide_url, "--comment-sample", "0"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert calls == [guide_url]
    assert payload["data"]["guide"]["id"] is None
    assert payload["data"]["guide"]["lookup_url"] == guide_url
    assert payload["data"]["comments"]["top"] == []



def test_guide_follow_up_commands_quote_the_guide_ref(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, page_url: SAMPLE_GUIDE_HTML)
    guide_url = "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps?tab=1&view=2"
    result = runner.invoke(app, ["guide", guide_url, "--comment-sample", "0"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    for command in (data["analysis_surfaces"]["fetch_more_command"], data["linked_entities"]["fetch_more_command"]):
        # `&` would background the command in a shell, so the URL has to arrive quoted.
        assert command == f"wowhead guide-full {shlex.quote(guide_url)}"


def test_guide_command_rejects_non_wowhead_url() -> None:
    result = runner.invoke(app, ["guide", "https://example.com/guide=3143"])
    assert result.exit_code != 0
    assert "Guide URL must point to wowhead.com" in result.output



def test_guide_full_returns_rich_payload(monkeypatch) -> None:
    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    result = runner.invoke(app, ["guide-full", "3143"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["guide"]["id"] == 3143
    assert payload["data"]["guide"]["page_url"] == "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"
    assert payload["data"]["author"]["name"] == "khazakdk"
    assert payload["data"]["rating"]["votes"] == 70
    assert payload["data"]["body"]["sections"][0]["title"] == "Frost Death Knight Overview"
    assert payload["data"]["body"]["section_chunks"][0]["content_text"] == "Welcome to the guide."
    assert payload["data"]["navigation"]["links"][0]["url"] == "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"
    assert payload["data"]["linked_entities"]["count"] >= 2
    assert payload["data"]["linked_entities"]["source_counts"] == {"href": 2, "gatherer": 1, "merged": 2}
    assert payload["data"]["gatherer_entities"]["items"][0]["id"] == 249277
    assert payload["data"]["gatherer_entities"]["items"][0]["citation_url"] == "https://www.wowhead.com/item=249277"
    merged_item = next(row for row in payload["data"]["linked_entities"]["items"] if row["id"] == 249277)
    assert merged_item["sources"] == ["gatherer", "href"]
    assert merged_item["source_kind"] == "gatherer"
    assert payload["data"]["comments"]["all_comments_included"] is True
    assert payload["data"]["comments"]["items"][0]["citation_url"].endswith("#comments:id=91")
    assert payload["data"]["structured_data"]["headline"] == "Frost Death Knight DPS Guide - Midnight"
    assert payload["data"]["analysis_surfaces"]["count"] >= 1
    assert payload["data"]["analysis_surfaces"]["items"][0]["surface_tags"] == ["overview"]



def test_guide_and_guide_full_share_linked_entity_count(monkeypatch) -> None:
    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    guide_result = runner.invoke(app, ["guide", "3143", "--comment-sample", "0"])
    full_result = runner.invoke(app, ["guide-full", "3143"])
    assert guide_result.exit_code == 0
    assert full_result.exit_code == 0

    guide_payload = json.loads(guide_result.stdout)
    full_payload = json.loads(full_result.stdout)
    assert guide_payload["data"]["linked_entities"]["count"] == full_payload["data"]["linked_entities"]["count"] == 2
    assert guide_payload["data"]["analysis_surfaces"]["count"] == full_payload["data"]["analysis_surfaces"]["count"]



def test_guide_export_writes_local_assets(monkeypatch, tmp_path) -> None:
    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    export_dir = tmp_path / "guide-export"
    result = runner.invoke(app, ["guide-export", "3143", "--out", str(export_dir)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["output_dir"] == str(export_dir)
    assert payload["data"]["counts"] == {
        "sections": 2,
        "analysis_surfaces": 1,
        "navigation_links": 2,
        "linked_entities": 2,
        "gatherer_entities": 1,
        "hydrated_entities": 0,
        "comments": 1,
    }

    expected_files = {
        "manifest.json",
        "guide.json",
        "page.html",
        "body.markup.txt",
        "navigation.markup.txt",
        "sections.jsonl",
        "navigation-links.jsonl",
        "linked-entities.jsonl",
        "gatherer-entities.jsonl",
        "comments.jsonl",
        "structured-data.json",
    }
    assert expected_files.issubset({path.name for path in export_dir.iterdir()})

    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    guide_json = json.loads((export_dir / "guide.json").read_text(encoding="utf-8"))
    root_index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert manifest["files"]["manifest_json"] == "manifest.json"
    assert manifest["files"]["guide_json"] == "guide.json"
    assert guide_json["guide"]["id"] == 3143
    assert root_index["index_version"] == 1
    assert root_index["count"] == 1
    assert root_index["bundles"][0]["path"] == str(export_dir)
    assert manifest["export_version"] == 2
    assert manifest["export_options"] == {
        "guide_ref": "3143",
        "max_links": 250,
        "include_replies": False,
    }
    assert manifest["hydration"] == {
        "enabled": False,
        "types": [],
        "limit": 0,
        "hydrated_at": None,
        "source_counts": {},
    }
    assert isinstance(manifest["exported_at"], str)
    assert isinstance(manifest["guide_fetched_at"], str)
    # The shared bundle loader names each bundle in `warcraft guide-compare` from these manifest fields.
    copy_dir = shutil.copytree(export_dir, tmp_path / "guide-export-copy")
    described = compare_article_bundles(
        [(export_dir, load_article_bundle(export_dir)), (copy_dir, load_article_bundle(copy_dir))]
    )["bundles"][0]
    assert (described["provider"], described["title"]) == ("wowhead", "Frost Death Knight DPS Guide - Midnight")

    sections_lines = (export_dir / "sections.jsonl").read_text(encoding="utf-8").strip().splitlines()
    navigation_lines = (export_dir / "navigation-links.jsonl").read_text(encoding="utf-8").strip().splitlines()
    comments_lines = (export_dir / "comments.jsonl").read_text(encoding="utf-8").strip().splitlines()
    first_section = json.loads(sections_lines[0])
    first_nav = json.loads(navigation_lines[0])
    first_comment = json.loads(comments_lines[0])
    linked_entity_lines = (export_dir / "linked-entities.jsonl").read_text(encoding="utf-8").strip().splitlines()
    linked_rows = [json.loads(line) for line in linked_entity_lines]
    merged_item = next(row for row in linked_rows if row["id"] == 249277)
    assert first_section["ordinal"] == 1
    assert first_section["level"] == 2
    assert first_section["title"] == "Frost Death Knight Overview"
    assert first_section["content_text"] == "Welcome to the guide."
    assert first_nav["label"] == "Overview"
    assert first_comment["citation_url"].endswith("#comments:id=91")
    assert merged_item["sources"] == ["gatherer", "href"]
    assert "Welcome to the guide." in (export_dir / "body.markup.txt").read_text(encoding="utf-8")



def test_guide_export_hydrates_linked_entities(monkeypatch, tmp_path: Path) -> None:
    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        if (entity_type, entity_id) == ("spell", 49020):
            return {
                "name": "Obliterate",
                "tooltip": "<table><tr><td><b>Obliterate</b><br>Talent<br>Instant<br>A brutal attack.</td></tr></table>",
            }
        if (entity_type, entity_id) == ("item", 249277):
            return {
                "name": "Bellamy's Final Judgement",
                "tooltip": "<table><tr><td><b>Bellamy's Final Judgement</b><br>Item Level 639</td></tr></table>",
            }
        raise AssertionError(f"Unexpected tooltip lookup: {(entity_type, entity_id)}")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)

    export_dir = tmp_path / "guide-export"
    result = runner.invoke(
        app,
        [
            "guide-export",
            "3143",
            "--out",
            str(export_dir),
            "--hydrate-linked-entities",
            "--hydrate-type",
            "spell,item",
            "--hydrate-limit",
            "2",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["counts"]["hydrated_entities"] == 2
    assert payload["data"]["hydration"]["enabled"] is True
    assert payload["data"]["hydration"]["types"] == ["spell", "item"]
    assert payload["data"]["hydration"]["limit"] == 2
    assert isinstance(payload["data"]["hydration"]["hydrated_at"], str)

    entities_manifest = json.loads((export_dir / "entities" / "manifest.json").read_text(encoding="utf-8"))
    assert entities_manifest["count"] == 2
    assert entities_manifest["counts_by_type"] == {"item": 1, "spell": 1}
    assert entities_manifest["counts_by_storage_source"] == {"live_fetch": 2}
    assert {row["path"] for row in entities_manifest["items"]} == {
        "entities/item/249277.json",
        "entities/spell/49020.json",
    }
    assert {row["storage_source"] for row in entities_manifest["items"]} == {"live_fetch"}
    assert payload["data"]["hydration"]["source_counts"] == {"live_fetch": 2}

    hydrated_spell = json.loads((export_dir / "entities" / "spell" / "49020.json").read_text(encoding="utf-8"))
    hydrated_item = json.loads((export_dir / "entities" / "item" / "249277.json").read_text(encoding="utf-8"))
    assert hydrated_spell["entity"]["name"] == "Obliterate"
    assert hydrated_item["entity"]["name"] == "Bellamy's Final Judgement"



def test_guide_export_hydration_uses_normalized_entity_cache_before_live_fetch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path / "cache"))

    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    def fail_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        raise AssertionError(f"tooltip should not be used when normalized cache is prepopulated: {(entity_type, entity_id)}")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fail_tooltip)

    cache_client = WowheadClient(cache_dir=tmp_path / "cache", cache_backend="file")
    cache_client.set_cached_entity_response(
        {
            "expansion": "retail",
            "entity": {
                "type": "spell",
                "id": 49020,
                "name": "Obliterate",
                "page_url": "https://www.wowhead.com/spell=49020/obliterate",
            },
            "tooltip": {
                "summary": "A brutal attack.",
                "text": "Obliterate Talent Instant A brutal attack.",
                "html": "<table><tr><td><b>Obliterate</b><br>Talent<br>Instant<br>A brutal attack.</td></tr></table>",
            },
        },
        requested_type="spell",
        requested_id=49020,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    )
    cache_client.set_cached_entity_response(
        {
            "expansion": "retail",
            "entity": {
                "type": "item",
                "id": 249277,
                "name": "Bellamy's Final Judgement",
                "page_url": "https://www.wowhead.com/item=249277/bellamys-final-judgement",
            },
            "tooltip": {
                "summary": "Item Level 639",
                "text": "Bellamy's Final Judgement Item Level 639",
                "html": "<table><tr><td><b>Bellamy's Final Judgement</b><br>Item Level 639</td></tr></table>",
            },
        },
        requested_type="item",
        requested_id=249277,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    )

    export_dir = tmp_path / "guide-export"
    result = runner.invoke(
        app,
        [
            "guide-export",
            "3143",
            "--out",
            str(export_dir),
            "--hydrate-linked-entities",
            "--hydrate-type",
            "spell,item",
            "--hydrate-limit",
            "2",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["hydration"]["source_counts"] == {"entity_cache": 2}
    entities_manifest = json.loads((export_dir / "entities" / "manifest.json").read_text(encoding="utf-8"))
    assert entities_manifest["counts_by_storage_source"] == {"entity_cache": 2}
    assert {row["storage_source"] for row in entities_manifest["items"]} == {"entity_cache"}

    spell_payload = json.loads((export_dir / "entities" / "spell" / "49020.json").read_text(encoding="utf-8"))
    item_payload = json.loads((export_dir / "entities" / "item" / "249277.json").read_text(encoding="utf-8"))
    assert spell_payload["entity"]["name"] == "Obliterate"
    assert item_payload["entity"]["name"] == "Bellamy's Final Judgement"



def test_guide_export_hydration_provenance_can_mix_cache_and_live_fetch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path / "cache"))

    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    tooltip_calls: dict[tuple[str, int], int] = {}

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        key = (entity_type, entity_id)
        tooltip_calls[key] = tooltip_calls.get(key, 0) + 1
        if key == ("item", 249277):
            return {
                "name": "Bellamy's Final Judgement",
                "tooltip": "<table><tr><td><b>Bellamy's Final Judgement</b><br>Item Level 639</td></tr></table>",
            }
        raise AssertionError(f"Unexpected tooltip lookup: {key}")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)

    cache_client = WowheadClient(cache_dir=tmp_path / "cache", cache_backend="file")
    cache_client.set_cached_entity_response(
        {
            "expansion": "retail",
            "entity": {
                "type": "spell",
                "id": 49020,
                "name": "Obliterate",
                "page_url": "https://www.wowhead.com/spell=49020/obliterate",
            },
            "tooltip": {
                "summary": "A brutal attack.",
                "text": "Obliterate Talent Instant A brutal attack.",
                "html": "<table><tr><td><b>Obliterate</b><br>Talent<br>Instant<br>A brutal attack.</td></tr></table>",
            },
        },
        requested_type="spell",
        requested_id=49020,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    )

    export_dir = tmp_path / "guide-export"
    result = runner.invoke(
        app,
        [
            "guide-export",
            "3143",
            "--out",
            str(export_dir),
            "--hydrate-linked-entities",
            "--hydrate-type",
            "spell,item",
            "--hydrate-limit",
            "2",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["hydration"]["source_counts"] == {
        "entity_cache": 1,
        "live_fetch": 1,
    }
    assert tooltip_calls == {
        ("item", 249277): 1,
    }
    entities_manifest = json.loads((export_dir / "entities" / "manifest.json").read_text(encoding="utf-8"))
    assert entities_manifest["counts_by_storage_source"] == {
        "entity_cache": 1,
        "live_fetch": 1,
    }
    source_by_entity = {
        (row["entity_type"], row["id"]): row["storage_source"]
        for row in entities_manifest["items"]
    }
    assert source_by_entity == {
        ("spell", 49020): "entity_cache",
        ("item", 249277): "live_fetch",
    }



def test_guide_query_reads_exported_assets(monkeypatch, tmp_path) -> None:
    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    export_dir = tmp_path / "guide-export"
    export_result = runner.invoke(app, ["guide-export", "3143", "--out", str(export_dir)])
    assert export_result.exit_code == 0

    result = runner.invoke(app, ["guide-query", str(export_dir), "bellamy", "--limit", "3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["counts"]["gatherer_entities"] >= 1
    assert payload["data"]["counts"]["analysis_surfaces"] == 0
    assert payload["data"]["matches"]["gatherer_entities"][0]["name"] == "Bellamy's Final Judgement"
    assert payload["data"]["top"][0]["kind"] == "linked_entity"
    assert payload["data"]["top"][0]["name"] == "Bellamy's Final Judgement"
    assert payload["data"]["top"][0]["sources"] == ["gatherer", "href"]

    result = runner.invoke(app, ["guide-query", str(export_dir), "obliterate", "--limit", "3"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["counts"]["linked_entities"] >= 1
    assert payload["data"]["matches"]["linked_entities"][0]["entity_type"] == "spell"
    assert payload["data"]["matches"]["linked_entities"][0]["name"] == "Obliterate"
    assert payload["data"]["top"][0]["kind"] == "linked_entity"
    assert payload["data"]["top"][0]["sources"] == ["href"]

    duplicate_entity_rows = [
        row for row in payload["data"]["top"] if row.get("entity_type") == "spell" and row.get("id") == 49020
    ]
    assert len(duplicate_entity_rows) == 1

    result = runner.invoke(app, ["guide-query", str(export_dir), "welcome guide", "--limit", "2"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["matches"]["sections"][0]["title"] == "Frost Death Knight Overview"
    assert "Welcome to the guide." in payload["data"]["matches"]["sections"][0]["preview"]

    result = runner.invoke(app, ["guide-query", str(export_dir), "overview", "--kind", "analysis_surfaces"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["counts"]["analysis_surfaces"] >= 1
    assert payload["data"]["matches"]["analysis_surfaces"][0]["surface_tags"] == ["overview"]

    result = runner.invoke(
        app,
        ["guide-query", str(export_dir), "welcome", "--kind", "sections", "--section-title", "overview"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["filters"] == {
        "kinds": ["sections"],
        "section_title": "overview",
        "linked_sources": [],
    }
    assert payload["data"]["counts"]["sections"] == 1
    assert payload["data"]["counts"]["comments"] == 0
    assert payload["data"]["matches"]["sections"][0]["title"] == "Frost Death Knight Overview"

    result = runner.invoke(app, ["guide-query", str(export_dir), "solid", "--kind", "comments"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["counts"]["comments"] == 1
    assert payload["data"]["counts"]["sections"] == 0
    assert payload["data"]["matches"]["comments"][0]["user"] == "A"

    root = tmp_path / "wowhead_exports"
    selector_dir = root / export_dir.name
    root.mkdir(exist_ok=True)
    export_dir.rename(selector_dir)

    result = runner.invoke(app, ["guide-query", "3143", "obliterate", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["output_dir"] == str(selector_dir)
    assert payload["data"]["matches"]["linked_entities"][0]["name"] == "Obliterate"

    result = runner.invoke(
        app,
        ["guide-query", "3143", "bellamy", "--root", str(root), "--kind", "linked_entities", "--linked-source", "multi"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["filters"]["linked_sources"] == ["multi"]
    assert payload["data"]["counts"]["linked_entities"] == 1
    assert payload["data"]["matches"]["linked_entities"][0]["name"] == "Bellamy's Final Judgement"
    assert payload["data"]["matches"]["linked_entities"][0]["sources"] == ["gatherer", "href"]

    result = runner.invoke(
        app,
        ["guide-query", "3143", "obliterate", "--root", str(root), "--kind", "linked_entities", "--linked-source", "href"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["filters"]["linked_sources"] == ["href"]
    assert payload["data"]["matches"]["linked_entities"][0]["name"] == "Obliterate"

    result = runner.invoke(app, ["guide-query", selector_dir.name, "solid", "--root", str(root), "--kind", "comments"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["output_dir"] == str(selector_dir)
    assert payload["data"]["matches"]["comments"][0]["user"] == "A"

    missing_dir = tmp_path / "missing-corpus"
    result = runner.invoke(app, ["guide-query", str(missing_dir), "anything"])
    assert result.exit_code != 0
    assert "does not exist" in result.output

    result = runner.invoke(app, ["guide-query", str(selector_dir), "anything", "--linked-source", "bad-source"])
    assert result.exit_code != 0
    assert "Unsupported linked source filter" in result.output




def test_guide_query_match_sort_key_ranks_non_entity_rows_by_score_then_kind() -> None:
    """Sections, comments and navigation rows share one branch; score must beat kind priority."""
    rows = [
        {"kind": "comment", "score": 9, "ordinal": 1},
        {"kind": "section", "score": 9, "ordinal": 2},
        {"kind": "section", "score": 12, "ordinal": 3},
        {"kind": "navigation", "score": 12, "ordinal": 4},
        {"kind": "section", "score": 12, "ordinal": 1},
    ]
    ordered = [row["ordinal"] for row in sorted(rows, key=guide_query_match_sort_key)]
    assert ordered == [1, 3, 4, 2, 1]
