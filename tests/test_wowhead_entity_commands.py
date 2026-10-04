"""Entity, entity-page, comments, compare, and linked-preview commands for the wowhead CLI."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from wowhead_cli.entities import (
    build_linked_entity_preview,
    comparison_entity_record,
    comparison_field_diffs,
    comparison_linked_entities_summary,
    entity_comments_payload,
    entity_linked_entities_payload,
    entity_page_needs_fetch,
)
from wowhead_cli.expansion_profiles import resolve_expansion
from wowhead_cli.main import app
from wowhead_cli.wowhead_client import WowheadClient

from tests.wowhead_testkit import SAMPLE_PAGE_HTML, runner


def test_entity_page_command_returns_links_with_citations(monkeypatch) -> None:
    def fake_html(self, entity_type: str, entity_id: int):
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity-page", "item", "19019", "--max-links", "10"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert "comments_url" not in payload["data"]["entity"]
    assert payload["data"]["citations"]["comments"] == "https://www.wowhead.com/item=19019/thunderfury#comments"
    assert payload["data"]["linked_entities"]["count"] >= 1
    first = payload["data"]["linked_entities"]["items"][0]
    assert "citation_url" in first
    assert "source_url" in first



def test_entity_page_lower_cases_the_entity_type(monkeypatch) -> None:
    requested: list[str] = []

    def fake_html(self, entity_type: str, entity_id: int):
        requested.append(entity_type)
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity-page", "ITEM", "19019", "--max-links", "1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["entity"]["type"] == "item"
    assert requested == ["item"]


def test_comments_command_returns_comment_citations(monkeypatch) -> None:
    def fake_html(self, entity_type: str, entity_id: int):
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["comments", "item", "19019", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert "comments_url" not in payload["data"]["entity"]
    assert payload["data"]["citations"]["comments"] == "https://www.wowhead.com/item=19019/thunderfury#comments"
    assert payload["data"]["comments"][0]["citation_url"].endswith("#comments:id=11")
    assert payload["data"]["linked_entities"]["count"] >= 1
    assert payload["data"]["linked_entities"]["items"][0]["type"] == "npc"



def test_compare_command_returns_overlap_and_unique_links(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env: int = 11):
        if entity_id == 19019:
            return {"name": "Thunderfury", "quality": 5, "icon": "inv_sword_39"}
        return {"name": "Maladath", "quality": 4, "icon": "inv_sword_49"}

    def fake_html(self, entity_type: str, entity_id: int):
        if entity_id == 19019:
            return """
            <html><head>
              <meta property="og:title" content="Thunderfury">
              <meta name="description" content="Legendary sword A">
              <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury">
            </head><body>
              <a href="/npc=12056/baron-geddon">Shared</a>
              <a href="/quest=7786/thunderaan">UniqueA</a>
              <script>
                var lv_comments0 = [{"id": 501, "number": 0, "user": "A", "body": "A body", "date": "2024-01-01T00:00:00-06:00", "rating": 8, "nreplies": 0, "replies": []}];
              </script>
            </body></html>
            """
        return """
        <html><head>
          <meta property="og:title" content="Maladath">
          <meta name="description" content="Epic sword B">
          <link rel="canonical" href="https://www.wowhead.com/item=19351/maladath">
        </head><body>
          <a href="/npc=12056/baron-geddon">Shared</a>
          <a href="/quest=7787/other-quest">UniqueB</a>
          <script>
            var lv_comments0 = [{"id": 601, "number": 0, "user": "B", "body": "B body", "date": "2024-02-01T00:00:00-06:00", "rating": 3, "nreplies": 0, "replies": []}];
          </script>
        </body></html>
        """

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)

    result = runner.invoke(app, ["compare", "item:19019", "item:19351", "--comment-sample", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["comparison"]["linked_entities"]["shared_count_total"] == 1
    assert len(payload["data"]["comparison"]["linked_entities"]["unique_by_entity"]["item:19019"]) == 1
    assert len(payload["data"]["comparison"]["linked_entities"]["unique_by_entity"]["item:19351"]) == 1
    assert payload["data"]["comparison"]["fields"]["name"]["all_equal"] is False
    assert payload["data"]["entities"][0]["comments"]["top"][0]["citation_url"].endswith("#comments:id=501")
    assert payload["data"]["entities"][0]["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert "comments_url" not in payload["data"]["entities"][0]["entity"]
    assert "page" not in payload["data"]["entities"][0]["citations"]
    assert payload["data"]["entities"][0]["citations"]["comments"] == "https://www.wowhead.com/item=19019/thunderfury#comments"
    assert "citation_url" not in payload["data"]["comparison"]["linked_entities"]["shared_items"][0]
    assert "citation_url" not in payload["data"]["comparison"]["linked_entities"]["unique_by_entity"]["item:19019"][0]
    assert "citations" not in payload["data"]



def test_comparison_helper_payloads_are_stable() -> None:
    record, link_set = comparison_entity_record(
        ref="item:19019",
        entity_type="item",
        entity_id=19019,
        canonical_url="https://www.wowhead.com/item=19019/thunderfury",
        tooltip={"name": "Thunderfury", "quality": 5, "icon": "inv_sword_39"},
        metadata={"title": "Thunderfury", "description": "Legendary sword"},
        links=[
            {"entity_type": "npc", "id": 12056, "url": "https://www.wowhead.com/npc=12056"},
            {"entity_type": "quest", "id": 7786, "url": "https://www.wowhead.com/quest=7786"},
        ],
        max_links=1,
        raw_comments=[{"id": 1}, {"id": 2}],
        sampled_comments=[{"id": 1, "citation_url": "https://www.wowhead.com/item=19019#comments:id=1"}],
    )
    assert record["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert record["comments"]["count"] == 2
    assert (record["linked_entities"]["count"], record["linked_entities"]["total"]) == (1, 2)
    # The shared/unique comparison reads every link, not the block --max-links-per-entity cut.
    assert link_set == {("npc", 12056), ("quest", 7786)}

    fields = comparison_field_diffs(
        [
            {"ref": "item:19019", "summary": {"name": "Thunderfury", "quality": 5}},
            {"ref": "item:19351", "summary": {"name": "Maladath", "quality": 5}},
        ],
        comparable_fields=["name", "quality"],
    )
    assert fields["name"]["all_equal"] is False
    assert fields["quality"]["all_equal"] is True

    linked = comparison_linked_entities_summary(
        refs_in_order=["item:19019", "item:19351"],
        entity_link_sets={
            "item:19019": {("npc", 12056), ("quest", 7786)},
            "item:19351": {("npc", 12056), ("quest", 7787)},
        },
        expansion=resolve_expansion("retail"),
        max_shared_links=10,
        max_unique_links=10,
    )
    assert linked["shared_count_total"] == 1
    assert linked["unique_count_total_by_entity"] == {"item:19019": 1, "item:19351": 1}
    assert linked["shared_items"][0]["url"] == "https://www.wowhead.com/npc=12056"



def test_entity_page_needs_fetch_and_comments_payload_helpers() -> None:
    assert entity_page_needs_fetch(
        include_comments=False,
        linked_entity_preview_limit=0,
        tooltip_from_page_metadata=False,
    ) is False
    assert entity_page_needs_fetch(
        include_comments=True,
        linked_entity_preview_limit=0,
        tooltip_from_page_metadata=False,
    ) is True

    comments_payload, citations = entity_comments_payload(
        html=SAMPLE_PAGE_HTML,
        page_url="https://www.wowhead.com/item=19019/thunderfury",
        include_comments=True,
        include_all_comments=False,
        top_comment_limit=1,
        top_comment_chars=40,
    )
    assert comments_payload is not None
    assert comments_payload["count"] == 1
    assert comments_payload["top"][0]["user"] == "A"
    assert citations == {"comments": "https://www.wowhead.com/item=19019/thunderfury#comments"}



def test_a_zero_preview_limit_still_counts_every_linked_entity() -> None:
    """guide --linked-entity-preview-limit 0 used to report count 0 beside non-zero source counts."""
    links = [
        {"entity_type": "npc", "id": 1, "name": "A", "url": "https://www.wowhead.com/npc=1"},
        {"entity_type": "spell", "id": 2, "name": "B", "url": "https://www.wowhead.com/spell=2"},
    ]

    preview = build_linked_entity_preview(links, entity_type="item", entity_id=19019, preview_limit=0)

    assert (preview["count"], preview["counts_by_type"], preview["items"]) == (2, {"npc": 1, "spell": 1}, [])
    assert preview["more_available"] is True


def test_entity_linked_entities_payload_helper_builds_preview() -> None:
    payload = entity_linked_entities_payload(
        html=SAMPLE_PAGE_HTML,
        page_url="https://www.wowhead.com/item=19019/thunderfury",
        page_entity_type="item",
        page_entity_id=19019,
        requested_entity_type="item",
        requested_entity_id=19019,
        linked_entity_preview_limit=5,
        expansion=resolve_expansion("classic"),
    )
    assert payload is not None
    assert payload["count"] >= 1
    assert payload["fetch_more_command"] == "wowhead --expansion classic entity-page item 19019 --max-links 200"



def test_entity_respects_expansion_flag(monkeypatch) -> None:
    calls = []

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        calls.append((self.expansion.key, data_env))
        return {"name": "Thunderfury"}

    def fake_html(self, entity_type: str, entity_id: int):
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["--expansion", "classic", "entity", "item", "19019"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert calls == [("classic", None)]
    assert data["expansion"] == "classic"
    assert data["entity"]["name"] == "Thunderfury"
    assert data["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert "tooltip" not in data
    assert data["citations"]["comments"] == "https://www.wowhead.com/item=19019/thunderfury#comments"
    assert data["comments"]["count"] == 1
    assert data["comments"]["all_comments_included"] is True
    assert data["comments"]["needs_raw_fetch"] is False
    assert data["comments"]["top"][0]["citation_url"].endswith("#comments:id=11")
    assert data["linked_entities"]["count"] >= 1
    assert data["linked_entities"]["counts_by_type"]["npc"] == 1
    assert data["linked_entities"]["fetch_more_command"] == (
        "wowhead --expansion classic entity-page item 19019 --max-links 200"
    )



def test_entity_faction_uses_page_metadata_tooltip_fallback(monkeypatch) -> None:
    html = """
    <html><head>
      <meta property="og:title" content="Argent Dawn">
      <meta name="description" content="Protect Azeroth from the Scourge.">
      <link rel="canonical" href="https://www.wowhead.com/faction=529/argent-dawn">
    </head><body><script>var lv_comments0 = [];</script></body></html>
    """

    def fail_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        raise AssertionError("tooltip should not be called for faction fallback")

    def fake_html(self, entity_type: str, entity_id: int):
        assert (entity_type, entity_id) == ("faction", 529)
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fail_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "faction", "529", "--no-include-comments", "--linked-entity-preview-limit", "0"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["entity"] == {
        "type": "faction",
        "id": 529,
        "name": "Argent Dawn",
        "page_url": "https://www.wowhead.com/faction=529/argent-dawn",
    }
    assert payload["data"]["tooltip"]["text"] == "Argent Dawn Protect Azeroth from the Scourge."
    assert payload["data"]["tooltip"]["summary"] == "Protect Azeroth from the Scourge."



def test_entity_recipe_routes_through_spell_tooltip(monkeypatch) -> None:
    tooltip_calls = []

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        tooltip_calls.append((entity_type, entity_id, data_env))
        return {"name": "Seasoned Wolf Kabob", "tooltip": "<b>Seasoned Wolf Kabob</b>"}

    def fail_html(self, entity_type: str, entity_id: int):
        raise AssertionError("entity page should not be fetched")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fail_html)
    result = runner.invoke(app, ["entity", "recipe", "2549", "--no-include-comments", "--linked-entity-preview-limit", "0"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert tooltip_calls == [("spell", 2549, None)]
    assert payload["data"]["entity"]["type"] == "recipe"
    assert payload["data"]["entity"]["id"] == 2549
    assert payload["data"]["entity"]["page_url"] == "https://www.wowhead.com/spell=2549"
    assert payload["data"]["entity"]["name"] == "Seasoned Wolf Kabob"



def test_entity_page_merges_multi_source_linked_entities(monkeypatch) -> None:
    html = """
    <html><head>
      <meta property="og:title" content="Thunderfury">
      <meta name="description" content="Legendary sword">
      <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury">
    </head><body>
      <a href="/spell=49020"></a>
      <script>
        WH.Gatherer.addData(6, 1, {"49020":{"name_enus":"Obliterate"}});
        var lv_comments0 = [];
      </script>
    </body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity-page", "item", "19019", "--max-links", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["linked_entities"]["count"] == 1
    assert payload["data"]["linked_entities"]["items"][0]["name"] == "Obliterate"
    assert payload["data"]["linked_entities"]["items"][0]["sources"] == ["gatherer", "href"]
    assert payload["data"]["linked_entities"]["items"][0]["source_kind"] == "gatherer"



def test_entity_supports_excluding_comments(monkeypatch) -> None:
    page_calls = []

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Thunderfury"}

    def fake_html(self, entity_type: str, entity_id: int):
        page_calls.append((entity_type, entity_id))
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments", "--linked-entity-preview-limit", "0"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert "comments" not in payload["data"]
    assert "linked_entities" not in payload["data"]
    assert payload["data"]["entity"]["page_url"] == "https://www.wowhead.com/item=19019"
    assert "citations" not in payload["data"]
    assert page_calls == []



def test_entity_includes_linked_entity_preview_without_comments(monkeypatch) -> None:
    page_calls = []

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Thunderfury"}

    def fake_html(self, entity_type: str, entity_id: int):
        page_calls.append((entity_type, entity_id))
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["linked_entities"]["count"] >= 1
    assert payload["data"]["linked_entities"]["counts_by_type"]["npc"] == 1
    assert payload["data"]["linked_entities"]["items"][0]["type"] == "npc"
    assert set(payload["data"]["linked_entities"]["items"][0].keys()) == {"type", "id", "name", "url"}
    assert payload["data"]["linked_entities"]["more_available"] is False
    assert page_calls == [("item", 19019)]



def test_entity_supports_include_all_comments(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Thunderfury"}

    def fake_html(self, entity_type: str, entity_id: int):
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--include-all-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["comments"]["count"] == 1
    assert payload["data"]["comments"]["all_comments_included"] is True
    assert payload["data"]["comments"]["needs_raw_fetch"] is False
    assert "items" in payload["data"]["comments"]
    assert "top" not in payload["data"]["comments"]
    assert payload["data"]["comments"]["items"][0]["id"] == 11



def test_entity_marks_partial_comments_when_more_than_top_limit(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Thunderfury"}

    html = """
    <html><body><script>
      var lv_comments0 = [
        {"id": 1, "number": 0, "user": "A", "body": "One", "date": "2024-01-01T00:00:00-06:00", "rating": 1, "nreplies": 0, "replies": []},
        {"id": 2, "number": 1, "user": "B", "body": "Two", "date": "2024-01-02T00:00:00-06:00", "rating": 2, "nreplies": 0, "replies": []},
        {"id": 3, "number": 2, "user": "C", "body": "Three", "date": "2024-01-03T00:00:00-06:00", "rating": 3, "nreplies": 0, "replies": []},
        {"id": 4, "number": 3, "user": "D", "body": "Four", "date": "2024-01-04T00:00:00-06:00", "rating": 4, "nreplies": 0, "replies": []}
      ];
    </script></body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["comments"]["all_comments_included"] is False
    assert payload["data"]["comments"]["needs_raw_fetch"] is True
    assert payload["data"]["comments"]["count"] == 4
    assert len(payload["data"]["comments"]["top"]) == 3



def test_entity_normalizes_tooltip_name_and_html(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Thunderfury", "tooltip": "<b>Legendary</b> weapon", "quality": 5}

    def fake_html(self, entity_type: str, entity_id: int):
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["entity"]["name"] == "Thunderfury"
    assert payload["data"]["tooltip"]["quality"] == 5
    assert payload["data"]["tooltip"]["html"] == "<b>Legendary</b> weapon"
    assert payload["data"]["tooltip"]["text"] == "Legendary weapon"
    assert payload["data"]["tooltip"]["summary"] == "Legendary weapon"
    assert "name" not in payload["data"]["tooltip"]
    assert "tooltip" not in payload["data"]["tooltip"]



def test_entity_cleans_spell_tooltip_artifacts_and_builds_summary(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {
            "name": "Obliterate",
            "tooltip": (
                "<a href=\"/spell=49020/obliterate\"><b>Obliterate</b></a>"
                "<div>Talent</div><div>Instant</div>"
                "<div>A brutal attack [that deals [(105.751% of Attack Power)] Physical and "
                "[(105.751% of Attack Power)] Frost damage.] Physical and Frost damage.]</div>"
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "spell", "49020"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["tooltip"]["text"] == "Obliterate Talent Instant A brutal attack Physical and Frost damage."
    assert payload["data"]["tooltip"]["summary"] == "A brutal attack Physical and Frost damage."



def test_entity_item_summary_prefers_effect_text_over_item_metadata(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {
            "name": "Thunderfury",
            "tooltip": (
                "<table><tr><td><b>Thunderfury</b><br>Item Level 40<br>Binds when picked up</td></tr></table>"
                "<table><tr><td>Chance on hit: Blasts your enemy with lightning and slows its attack speed.</td></tr></table>"
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["tooltip"]["summary"] == "Chance on hit: Blasts your enemy with lightning and slows its attack speed."



def test_entity_mount_summary_prefers_use_text_over_mount_metadata(monkeypatch) -> None:
    # Mount pages resolve through the tooltip redirect, so the entity command calls
    # tooltip_with_metadata (not tooltip) and needs the final tooltip URL.
    def fake_tooltip_with_metadata(self, entity_type: str, entity_id: int, data_env=None):
        payload = {
            "name": "Grand Expedition Yak",
            "tooltip": (
                "<table><tr><td><b>Grand Expedition Yak</b><br>Item Level 10<br>Mount (Account-wide)</td></tr></table>"
                "<table><tr><td>Use: Teaches you how to summon this three-person mount with vendors.</td></tr></table>"
            ),
        }
        return payload, f"https://nether.wowhead.com/tooltip/{entity_type}/{entity_id}?dataEnv=1"

    def fake_html(self, entity_type: str, entity_id: int):
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip_with_metadata", fake_tooltip_with_metadata)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "mount", "460"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["tooltip"]["summary"] == "Use: Teaches you how to summon this three-person mount with vendors."



@pytest.mark.parametrize(
    ("price_html", "price_text"),
    [
        # Synthetic markup in Wowhead's shape: the span class, not the position, names the denomination.
        ('<span class="moneysilver">87</span> <span class="moneycopper">50</span>', "87s 50c"),
        ('<span class="moneycopper">13</span>', "13c"),
        (
            '<span class="moneygold">4</span> <span class="moneysilver">2</span> <span class="moneycopper">63</span>',
            "4g 2s 63c",
        ),
    ],
)
def test_entity_item_tooltip_text_reads_money_units_from_spans(monkeypatch, price_html: str, price_text: str) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {
            "name": "Maladath",
            "tooltip": (
                "<table><tr><td><b>Maladath</b><br>+ 4 Parry<br>+ 2 Haste<br>"
                f'<div class="whtt-sellprice">Sell Price: {price_html}</div></td></tr></table>'
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19351"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["tooltip"]["text"] == f"Maladath +4 Parry +2 Haste Sell Price: {price_text}"


def test_entity_item_tooltip_text_names_the_currency_of_a_cost(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {
            "name": "Darkmoon Dancing Bear",
            "tooltip": (
                '<table><tr><td><b>Darkmoon Dancing Bear</b><br><span style="color: #FFD200">Cost: </span>180'
                '<a href="/currency=515/darkmoon-prize-ticket" aria-label="Darkmoon Prize Ticket">'
                '<span class="iconmedium"><ins></ins><del></del></span></a><br /></td></tr></table>'
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "73766"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["tooltip"]["text"] == "Darkmoon Dancing Bear Cost: 180 Darkmoon Prize Ticket"


def test_entity_item_style_tooltip_text_drops_flavor_quotes_and_normalizes_parenthetical_level(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {
            "name": "Grand Expedition Yak",
            "tooltip": (
                "<table><tr><td><b>Grand Expedition Yak</b><br>Requires level 1 to 90 ( 90)<br>"
                '<div class="whtt-sellprice">Sell Price: <span class="moneygold">30,000</span></div><br>'
                "\"These beasts of burden are known to carry over five times their own weight.\"<br>"
                'Vendor: Uncle Bigpocket<br>Cost: <span class="moneygold">120000</span></td></tr></table>'
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "84101"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["tooltip"]["text"] == (
        "Grand Expedition Yak Requires level 1 to 90 (90) Sell Price: 30,000g Vendor: Uncle Bigpocket Cost: 120000g"
    )



def test_entity_tooltip_summary_strips_leading_entity_name(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {
            "name": "Fairbreeze Favors",
            "tooltip": (
                "<table><tr><td><b>Fairbreeze Favors</b></td></tr></table>"
                "<table><tr><td>Help restore order in Fairbreeze Village.</td></tr></table>"
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "quest", "86739"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["tooltip"]["text"] == "Fairbreeze Favors Help restore order in Fairbreeze Village."
    assert payload["data"]["tooltip"]["summary"] == "Help restore order in Fairbreeze Village."



def test_entity_uses_normalized_entity_cache_between_invocations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path / "cache"))
    calls = {"tooltip": 0}

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        calls["tooltip"] += 1
        return {
            "name": "Thunderfury",
            "tooltip": "<table><tr><td><b>Thunderfury</b><br>Legendary weapon</td></tr></table>",
        }

    def fake_html(self, entity_type: str, entity_id: int):
        raise AssertionError("entity_page_html should not be used when comments and preview are disabled")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)

    args = ["entity", "item", "19019", "--no-include-comments", "--linked-entity-preview-limit", "0"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0
    second = runner.invoke(app, args)
    assert second.exit_code == 0

    assert calls["tooltip"] == 1
    first_payload, second_payload = json.loads(first.stdout), json.loads(second.stdout)
    assert first_payload["data"] == second_payload["data"]
    assert (first_payload["provenance"]["cache"]["hit"], second_payload["provenance"]["cache"]["hit"]) == (False, True)


def test_entity_cache_hit_reports_how_this_run_picked_its_expansion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.tooltip",
        lambda self, entity_type, entity_id, data_env=None: {"name": "Thunderfury", "tooltip": "<b>Thunderfury</b>"},
    )
    options = ["--no-include-comments", "--linked-entity-preview-limit", "0"]

    first = runner.invoke(app, ["--expansion", "tbc", "entity", "item", "19019", *options])
    second = runner.invoke(app, ["entity", "--url", "https://www.wowhead.com/tbc/item=19019", *options])

    assert first.exit_code == 0 and second.exit_code == 0, second.output
    second_payload = json.loads(second.stdout)
    assert second_payload["provenance"]["cache"]["hit"] is True
    assert (json.loads(first.stdout)["data"]["expansion_source"], second_payload["data"]["expansion_source"]) == ("flag", "url")


@pytest.mark.parametrize(
    "args",
    [
        ["entity", "../tooltip/item", "19019"],
        ["entity", "item?x=1#", "19019"],
        ["comments", "item/../spell", "1"],
        ["linked-graph", "../item", "1"],
        ["entity-page", "item", "0"],
        ["comments", "item", "0"],
        ["linked-graph", "item", "0"],
        ["entity", "--url", "https://www.wowhead.com/item=19019", "spell", "1"],
        ["entity-page", "--url", "https://www.wowhead.com/item=19019", "npc", "1"],
        ["compare", "../tooltip/item:19019", "item:19019"],
    ],
)
def test_entity_commands_refuse_a_bad_type_a_zero_id_or_both_url_and_type_id(monkeypatch, args: list[str]) -> None:
    def no_request(self, *call_args, **call_kwargs):
        raise AssertionError("a refused reference must not reach Wowhead")

    for name in ("tooltip", "tooltip_with_metadata", "entity_page_html", "page_html"):
        monkeypatch.setattr(f"wowhead_cli.main.WowheadClient.{name}", no_request)
    result = runner.invoke(app, args)

    # A usage error (exit 2) before any request; a zero id is refused by the argument's own range.
    assert result.exit_code == 2, result.output



def test_entity_preview_prefers_gatherer_name_when_href_label_missing(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Thunderfury"}

    html = """
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury">
    </head><body>
      <a href="/spell=49020"></a>
      <script>
        WH.Gatherer.addData(6, 1, {"49020":{"name_enus":"Obliterate"}});
        var lv_comments0 = [];
      </script>
    </body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["linked_entities"]["items"][0] == {
        "type": "spell",
        "id": 49020,
        "name": "Obliterate",
        "url": "https://www.wowhead.com/spell=49020",
    }



def test_entity_preview_prefers_multi_source_links_over_single_source_peers(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Thunderfury"}

    html = """
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury">
    </head><body>
      <a href="/spell=49020/obliterate">Obliterate</a>
      <a href="/spell=49184/howling-blast">Howling Blast</a>
      <script>
        WH.Gatherer.addData(6, 1, {"49020":{"name_enus":"Obliterate"}});
        var lv_comments0 = [];
      </script>
    </body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments", "--linked-entity-preview-limit", "2"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert [row["id"] for row in payload["data"]["linked_entities"]["items"]] == [49020, 49184]



@pytest.mark.parametrize("command", [["entity", "currency", "3008", "--no-include-comments"], ["comments", "currency", "3008"]])
@pytest.mark.parametrize(
    ("link_count", "max_links", "truncated"), [(250, 250, False), (2000, 2000, False), (2100, 2000, True)]
)
def test_entity_preview_fetch_more_command_scales_with_known_count(
    monkeypatch, command: list[str], link_count: int, max_links: int, truncated: bool
) -> None:
    def fake_tooltip(self: WowheadClient, entity_type: str, entity_id: int, data_env: int | None = None) -> dict[str, str]:
        return {"name": "Valorstones"}

    # 50 of the links sit in a relation tab, which entity-page returns too, so both previews count them.
    links = "\n".join(f'<a href="/item={200000 + idx}">Item {idx}</a>' for idx in range(link_count - 50))
    npcs = json.dumps([{"id": 100 + idx, "name": f"Npc {idx}"} for idx in range(50)])
    html = f"""
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/currency=3008/valorstones">
    </head><body>
      {links}
      <script>var lv_comments0 = [];</script>
      <script>new Listview({{template: 'npc', id: 'npcs', data:{npcs}}});</script>
    </body></html>
    """

    def fake_html(self: WowheadClient, entity_type: str, entity_id: int) -> str:
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, command)
    assert result.exit_code == 0, result.output

    preview = json.loads(result.stdout)["data"]["linked_entities"]
    assert preview["count"] == link_count
    assert preview["fetch_more_command"] == f"wowhead entity-page currency 3008 --max-links {max_links}"
    # entity-page cannot return more than 2000 links, so the preview says when its command falls short.
    assert preview["fetch_more_truncated"] is truncated


def test_entity_preview_suppresses_low_signal_names(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Hogger"}

    html = """
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/npc=448/hogger">
    </head><body>
      <a href="/item=727">item</a>
      <a href="/npc=34942/memory-of-hogger">Memory of Hogger</a>
      <a href="/spell=8732/thunderclap">Thunderclap</a>
      <script>var lv_comments0 = [];</script>
    </body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "npc", "448", "--no-include-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["linked_entities"]["items"][0] == {
        "type": "npc",
        "id": 34942,
        "name": "Memory of Hogger",
        "url": "https://www.wowhead.com/npc=34942",
    }
    assert payload["data"]["linked_entities"]["items"][-1]["name"] is None



def test_entity_preview_prefers_diverse_high_value_types(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Test Item"}

    html = """
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/item=1/test-item">
    </head><body>
      <a href="/item=2/item-two">Item Two</a>
      <a href="/item=3/item-three">Item Three</a>
      <a href="/npc=4/test-npc">Test NPC</a>
      <a href="/quest=5/test-quest">Test Quest</a>
      <a href="/spell=6/test-spell">Test Spell</a>
      <script>var lv_comments0 = [];</script>
    </body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "1", "--no-include-comments", "--linked-entity-preview-limit", "4"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert [row["type"] for row in payload["data"]["linked_entities"]["items"]] == ["npc", "quest", "spell", "item"]



def test_currency_preview_demotes_items_below_more_actionable_types(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": "Valorstones"}

    html = """
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/currency=3008/valorstones">
    </head><body>
      <a href="/item=10/item-ten">Item Ten</a>
      <a href="/item=11/item-eleven">Item Eleven</a>
      <a href="/npc=12/test-npc">Test NPC</a>
      <a href="/quest=13/test-quest">Test Quest</a>
      <a href="/spell=14/test-spell">Test Spell</a>
      <a href="/object=15/test-object">Test Object</a>
      <script>var lv_comments0 = [];</script>
    </body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "currency", "3008", "--no-include-comments", "--linked-entity-preview-limit", "4"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert [row["type"] for row in payload["data"]["linked_entities"]["items"]] == ["npc", "quest", "spell", "object"]



def test_compare_respects_expansion_flag_for_generated_urls(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):
        return {"name": f"Item {entity_id}", "quality": 1, "icon": "inv_misc_questionmark"}

    def fake_html(self, entity_type: str, entity_id: int):
        if entity_id == 1:
            return """
            <html><body>
              <a href="/npc=12056/baron-geddon">Shared</a>
              <a href="/quest=7786/unique-a">UniqueA</a>
              <script>var lv_comments0 = [];</script>
            </body></html>
            """
        return """
        <html><body>
          <a href="/npc=12056/baron-geddon">Shared</a>
          <a href="/quest=7787/unique-b">UniqueB</a>
          <script>var lv_comments0 = [];</script>
        </body></html>
        """

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(
        app,
        ["--expansion", "wotlk", "compare", "item:1", "item:2", "--comment-sample", "0", "--max-links-per-entity", "10"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["expansion"] == "wotlk"
    assert [row["entity"]["page_url"] for row in payload["data"]["entities"]] == [
        "https://www.wowhead.com/wotlk/item=1",
        "https://www.wowhead.com/wotlk/item=2",
    ]
    assert payload["data"]["comparison"]["linked_entities"]["shared_items"][0]["url"] == "https://www.wowhead.com/wotlk/npc=12056"
    assert "citation_url" not in payload["data"]["comparison"]["linked_entities"]["shared_items"][0]



def test_canonical_normalization_flag_for_entity_page(monkeypatch) -> None:
    html = """
    <html><head>
      <meta property="og:title" content="Thunderfury">
      <meta name="description" content="Legendary sword">
      <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury-blessed-blade-of-the-windseeker">
    </head><body>
      <a href="/ptr/npc=12056/baron-geddon">Baron Geddon</a>
    </body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)

    default_result = runner.invoke(app, ["--expansion", "ptr", "entity-page", "item", "19019", "--max-links", "1"])
    assert default_result.exit_code == 0
    default_payload = json.loads(default_result.stdout)
    assert default_payload["data"]["normalize_canonical_to_expansion"] is False
    assert default_payload["data"]["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury-blessed-blade-of-the-windseeker"

    normalized_result = runner.invoke(
        app,
        [
            "--expansion",
            "ptr",
            "--normalize-canonical-to-expansion",
            "entity-page",
            "item",
            "19019",
            "--max-links",
            "1",
        ],
    )
    assert normalized_result.exit_code == 0
    normalized_payload = json.loads(normalized_result.stdout)
    assert normalized_payload["data"]["normalize_canonical_to_expansion"] is True
    assert normalized_payload["data"]["entity"]["page_url"] == "https://www.wowhead.com/ptr/item=19019/thunderfury-blessed-blade-of-the-windseeker"



def test_canonical_normalization_flag_for_comments_citations(monkeypatch) -> None:
    html = """
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury-blessed-blade-of-the-windseeker">
    </head><body>
      <script>
        var lv_comments0 = [{"id": 11, "number": 0, "user": "A", "body": "Useful", "date": "2024-01-01T00:00:00-06:00", "rating": 7, "nreplies": 0, "replies": []}];
      </script>
    </body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(
        app,
        [
            "--expansion",
            "ptr",
            "--normalize-canonical-to-expansion",
            "comments",
            "item",
            "19019",
            "--limit",
            "1",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["normalize_canonical_to_expansion"] is True
    assert payload["data"]["entity"]["page_url"] == "https://www.wowhead.com/ptr/item=19019/thunderfury-blessed-blade-of-the-windseeker"
    assert payload["data"]["comments"][0]["citation_url"] == "https://www.wowhead.com/ptr/item=19019/thunderfury-blessed-blade-of-the-windseeker#comments:id=11"


def test_comments_follow_up_command_carries_the_active_expansion(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.tooltip",
        lambda self, entity_type, entity_id, data_env=None: {"name": "Thunderfury"},
    )
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.entity_page_html",
        lambda self, entity_type, entity_id: SAMPLE_PAGE_HTML,
    )
    result = runner.invoke(app, ["--expansion", "classic", "comments", "item", "19019", "--limit", "1"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["linked_entities"]["fetch_more_command"] == (
        "wowhead --expansion classic entity-page item 19019 --max-links 200"
    )


def test_entity_page_reports_the_links_the_max_links_limit_cut_off(monkeypatch) -> None:
    links = "\n".join(f'<a href="/item={400000 + index}">Item {index}</a>' for index in range(30))
    html = f"""
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury">
    </head><body>{links}<script>var lv_comments0 = [];</script></body></html>
    """
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", lambda self, t, i: html)

    full = runner.invoke(app, ["entity-page", "item", "19019", "--max-links", "30"])
    capped = runner.invoke(app, ["entity-page", "item", "19019", "--max-links", "10"])
    assert full.exit_code == 0
    assert capped.exit_code == 0

    full_links = json.loads(full.stdout)["data"]["linked_entities"]
    assert full_links == {"count": 30, "total": 30, "truncated": False, "items": full_links["items"]}
    capped_links = json.loads(capped.stdout)["data"]["linked_entities"]
    assert capped_links["count"] == len(capped_links["items"]) == 10
    assert capped_links["total"] == 30
    assert capped_links["truncated"] is True


def test_compare_reports_the_links_each_entity_budget_cut_off(monkeypatch) -> None:
    links = "\n".join(f'<a href="/item={400000 + index}">Item {index}</a>' for index in range(30))
    html = f"<html><body>{links}<script>var lv_comments0 = [];</script></body></html>"
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", lambda self, t, i: html)
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.tooltip",
        lambda self, t, i, data_env=None: {"name": f"Item {i}"},
    )

    result = runner.invoke(
        app,
        ["compare", "item:1", "item:2", "--comment-sample", "0", "--max-links-per-entity", "10"],
    )
    assert result.exit_code == 0

    for record in json.loads(result.stdout)["data"]["entities"]:
        links_block = record["linked_entities"]
        assert links_block["count"] == len(links_block["items"]) == 10
        assert links_block["total"] == 30
        assert links_block["truncated"] is True


def test_compare_and_linked_graph_reach_entities_through_the_same_routes_as_entity(monkeypatch) -> None:
    calls: list[tuple[str, str, int]] = []

    def fake_tooltip(self: WowheadClient, entity_type: str, entity_id: int, data_env: int | None = None) -> dict[str, str]:
        calls.append(("tooltip", entity_type, entity_id))
        return {"name": f"{entity_type} {entity_id}"}

    def fake_tooltip_with_metadata(
        self: WowheadClient, entity_type: str, entity_id: int, data_env: int | None = None
    ) -> tuple[dict[str, str], str]:
        calls.append(("tooltip", entity_type, entity_id))
        return {"name": "Grand Expedition Yak"}, "https://nether.wowhead.com/tooltip/item/84101?dataEnv=1"

    def fake_page(self: WowheadClient, entity_type: str, entity_id: int) -> str:
        calls.append(("page", entity_type, entity_id))
        return '<html><head><meta property="og:title" content="Argent Dawn"></head></html>'

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip_with_metadata", fake_tooltip_with_metadata)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_page)

    compared = runner.invoke(app, ["compare", "faction:529", "recipe:2549", "mount:460", "--comment-sample", "0"])
    assert compared.exit_code == 0, compared.output
    # Faction has no tooltip route, recipe reads the spell, and the mount follows its tooltip redirect.
    assert calls == [
        ("page", "faction", 529),
        ("tooltip", "spell", 2549),
        ("page", "spell", 2549),
        ("tooltip", "mount", 460),
        ("page", "item", 84101),
    ]
    assert json.loads(compared.stdout)["data"]["entities"][0]["summary"]["name"] == "Argent Dawn"

    calls.clear()
    graphed = runner.invoke(app, ["linked-graph", "mount", "460"])
    assert graphed.exit_code == 0, graphed.output
    assert calls == [("tooltip", "mount", 460), ("page", "item", 84101)]


def test_compare_shares_links_beyond_the_per_entity_link_limit(monkeypatch) -> None:
    pages = {
        19019: '<a href="/item=1/a">a</a><a href="/item=2/b">b</a><a href="/item=3/c">c</a>',
        19351: '<a href="/item=3/c">c</a><a href="/item=2/b">b</a><a href="/item=1/a">a</a>',
    }
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", lambda self, entity_type, entity_id, data_env=None: {"name": "x"})
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.entity_page_html", lambda self, entity_type, entity_id: f"<html>{pages[entity_id]}</html>"
    )

    result = runner.invoke(app, ["compare", "item:19019", "item:19351", "--max-links-per-entity", "1", "--comment-sample", "0"])

    assert result.exit_code == 0, result.output
    linked = json.loads(result.stdout)["data"]["comparison"]["linked_entities"]
    assert linked["shared_count_total"] == 3
    assert linked["unique_count_total_by_entity"] == {"item:19019": 0, "item:19351": 0}


# Trimmed from the live faction=2653 page (2026-09): its members tab is a Listview with inline data.
FACTION_LISTVIEW_HTML = """<html><body><script>
new Listview({
    data: lv_comments0,
    id: 'comments',
    template: 'comment',
});
new Listview({
    template: 'npc',
    id: 'members',
    name: WH.TERMS.members,
    note: "<a href=\\"\\/npcs?filter=3;2653;0\\">Filter these results<\\/a>",
    extraCols: ['popularity'], sort: ["popularity"], maxPopularity: 543,
    data:[{"classification":0,"displayName":"Volo the Leg-Breaker","id":226516,"name":"Volo the Leg-Breaker","popularity":13},
          {"classification":0,"displayName":"Papa Kraz Torquewrench","id":226518,"name":"Papa Kraz Torquewrench"}]
});
new Listview({template: 'sound', id: 'sounds', data:[{"id":5,"name":"Hit"}]});
</script></body></html>"""


def test_entity_page_lists_the_entities_in_a_page_relation_tab(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", lambda self, entity_type, entity_id: FACTION_LISTVIEW_HTML)

    result = runner.invoke(app, ["entity-page", "faction", "2653"])

    assert result.exit_code == 0, result.output
    items = json.loads(result.stdout)["data"]["linked_entities"]["items"]
    assert [(row["entity_type"], row["id"], row["source_kind"], row["listview"]) for row in items] == [
        ("npc", 226516, "listview", "members"),
        ("npc", 226518, "listview", "members"),
    ]


def test_compare_counts_the_entities_in_each_page_relation_tab(monkeypatch) -> None:
    """compare read only body links and gatherer records, so two factions' members never showed up as shared."""
    pages = {2653: FACTION_LISTVIEW_HTML, 2654: "<html></html>"}
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", lambda self, entity_type, entity_id: pages[entity_id])

    result = runner.invoke(app, ["compare", "faction:2653", "faction:2654", "--comment-sample", "0"])

    assert result.exit_code == 0, result.output
    linked = json.loads(result.stdout)["data"]["comparison"]["linked_entities"]
    assert [(row["entity_type"], row["id"]) for row in linked["unique_by_entity"]["faction:2653"]] == [
        ("npc", 226516),
        ("npc", 226518),
    ]


# Trimmed from the live item=50818 (Invincible's Reins) and item=18567 pages (2026-10): comments link
# Onyxia and The Lich King, gatherer data names both and the smelting spell, the "dropped-by" tab names
# the dropper with its drop sample, and a comment links the spell with the anchor text "t".
DROPPED_BY_HTML = """<html><body>
<a href="/npc=10184/onyxia">Onyxia</a> <a href="/npc=36597/the-lich-king">The Lich King</a>
<a href="/spell=22967/smelt-enchanted-elementium">t</a> <a href="/achievement=3802">this achievement</a>
<script>
WH.Gatherer.addData(1, 1, {"10184":{"name_enus":"Onyxia"},"36597":{"name_enus":"The Lich King"}});
WH.Gatherer.addData(6, 1, {"22967":{"name_enus":"Smelt Enchanted Elementium"}});
new Listview({template: 'npc', id: 'dropped-by', name: WH.TERMS.droppedby,
    data:[{"boss":1,"displayName":"The Lich King","id":36597,"name":"The Lich King","count":2054,"outof":265761}]});
</script></body></html>"""


def test_a_relation_tab_survives_the_merge_and_leads_the_preview_with_its_drop_sample(monkeypatch) -> None:
    """Live `wowhead entity-page item 50818` (2026-10) showed The Lich King with `listview: None`, because the
    tab record merged into the earlier href record, and `wowhead entity item 50818` previewed Onyxia."""
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", lambda self, entity_type, entity_id: DROPPED_BY_HTML)

    items = json.loads(runner.invoke(app, ["entity-page", "item", "50818"]).stdout)["data"]["linked_entities"]["items"]
    lich_king = next(row for row in items if row["id"] == 36597)
    assert (lich_king["listview"], lich_king["listviews"]) == ("dropped-by", ["dropped-by"])
    assert lich_king["listview_data"] == {"count": 2054, "outof": 265761}
    assert lich_king["sources"] == ["gatherer", "href", "listview"]
    # Gatherer data carries Wowhead's name for the spell; the comment's anchor text does not replace it.
    assert next(row for row in items if row["id"] == 22967)["name"] == "Smelt Enchanted Elementium"

    preview = entity_linked_entities_payload(
        html=DROPPED_BY_HTML,
        page_url="https://www.wowhead.com/item=50818",
        page_entity_type="item",
        page_entity_id=50818,
        requested_entity_type="item",
        requested_entity_id=50818,
        linked_entity_preview_limit=3,
        expansion=resolve_expansion("retail"),
    )
    assert preview is not None
    assert preview["items"][0] == {
        "type": "npc",
        "id": 36597,
        "name": "The Lich King",
        "url": "https://www.wowhead.com/npc=36597",
        "listview": "dropped-by",
        "listview_data": {"count": 2054, "outof": 265761},
    }
    # A link's lowercase prose ("this achievement") is not shown as the linked entity's name.
    assert {row["id"]: row["name"] for row in preview["items"][1:]} == {22967: "Smelt Enchanted Elementium", 3802: None}

# Trimmed from the live quest=24748 page (2026-10): the Quick Facts infobox markup and the Series box.
QUEST_INFOBOX_HTML = r"""<html><body><table class="infobox-inner-table">
<tr class="infobox-heading"><th>Quick Facts</th></tr><tr><td><div id="infobox-contents-0"></div><script>
WH.markup.printHtml("[ul][li]Level: 30[\/li][li]Side: Both[\/li][li]Classes: [class=1], [class=2][\/li][li][img src=https:\/\/wow.zamimg.com\/questnormal.png style=\"vertical-align: middle;\"]Start: [url=\/npc=37120\/highlord-darion-mograine]Highlord Darion Mograine[\/url][\/li][li][img src=https:\/\/wow.zamimg.com\/questturnin.png]End: [url=\/npc=37120\/highlord-darion-mograine]Highlord Darion Mograine[\/url][\/li][li]Added in patch [acronym=\"3.3.0.11159\"]3.3.0[\/acronym] \"Fall of the Lich King\"[\/li][\/ul]", "infobox-contents-0", {dbPage: true});
</script></td></tr></table>
<table class="infobox-inner-table"><tr class="infobox-heading"><th>Series</th></tr><tr><td>
<table class="series"><tr><th>1.</th><td><div><a href="/quest=24545/the-sacred-and-the-corrupt">The Sacred and the Corrupt</a></div></td></tr><tr><th>2.</th><td><div><a href="/quest=24549/shadowmourne">Shadowmourne...</a></div></td></tr><tr><th>3.</th><td><div><b>The Lich King's Last Stand</b></div></td></tr></table>
</td></tr></table></body></html>"""

# Trimmed from the live npc=130993 page (2026-10): the "This NPC can be found in" link and the map data.
NPC_MAPPER_HTML = """<html><body><div>This NPC can be found in <span id="locations"><a href="javascript:" onclick="
    myMapper.update({
        zone: 9359,
        level: 1,
    });
    return false;" onmousedown="return false">The Vindicaar</a>&nbsp;(10).</span></div>
<script>var g_mapperData = {"9359":{"1":{"count":2,"coords":[[43.2,25.2],[51,44.8]]}}};
var myMapper = new Mapper({"parent":"k6b43j6b","name":"Captain Fareeya"});</script></body></html>"""


def test_entity_page_reports_a_quest_chain_its_start_and_end_and_an_npc_location(monkeypatch) -> None:
    """Live quest and NPC pages (2026-10) carry these in the infobox and map data, and entity-page left them out."""
    pages = {("quest", 24748): QUEST_INFOBOX_HTML, ("npc", 130993): NPC_MAPPER_HTML}
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", lambda self, entity_type, entity_id: pages[(entity_type, entity_id)])

    quest = json.loads(runner.invoke(app, ["entity-page", "quest", "24748"]).stdout)["data"]["facts"]
    mograine = {"type": "npc", "id": 37120, "name": "Highlord Darion Mograine", "url": "https://www.wowhead.com/npc=37120"}
    assert quest["quick_facts"] == [
        "Level: 30",
        "Side: Both",
        "Classes: class 1, class 2",
        "Start: Highlord Darion Mograine",
        "End: Highlord Darion Mograine",
        'Added in patch 3.3.0 "Fall of the Lich King"',
    ]
    assert (quest["start"], quest["end"]) == (mograine, mograine)
    assert [(step["position"], step["id"], step["current"]) for step in quest["series"][0]] == [
        (1, 24545, False),
        (2, 24549, False),
        (3, 24748, True),
    ]
    assert quest["series"][0][2]["url"] == "https://www.wowhead.com/quest=24748"
    assert "locations" not in quest

    npc = json.loads(runner.invoke(app, ["entity-page", "npc", "130993"]).stdout)["data"]["facts"]
    assert npc == {
        "locations": [{"zone_id": 9359, "zone": "The Vindicaar", "level": 1, "count": 2, "coords": [[43.2, 25.2], [51, 44.8]]}]
    }


# Trimmed from the live quest=5089, achievement=6 and npc=448 pages (2026-10).
ITEM_STARTED_QUEST_HTML = r"""<table><tr><th>Quick Facts</th></tr><tr><td><script>
WH.markup.printHtml("[ul][li]Side: [span class=icon-alliance]Alliance[\/span][\/li][li][img src=https:\/\/wow.zamimg.com\/questnormal.png]Start: [item=12780][\/li][\/ul]", "infobox-contents-0", {dbPage: true});
</script></td></tr></table>"""
ACHIEVEMENT_SERIES_HTML = r"""<table><tr><th>Quick Facts</th></tr><tr><td><script>
WH.markup.printHtml("[ul][li]Points: [achievementpoints=10][\/li][li class=icon-db-link]Icon: [icondb=236562 name=true][\/li][\/ul]", "infobox-contents-0", {dbPage: true});
</script></td></tr></table>
<table class="series"><tr><th>1.</th><td><div><b>Level 10</b></div></td></tr><tr><th>2.</th><td><div><a href="/achievement=7/level-20">Level 20</a></div></td></tr></table>"""
HOSTILE_NPC_HTML = r"""<table><tr><th>Quick Facts</th></tr><tr><td><script>
WH.markup.printHtml("[ul][li]React: [color=q10]A[\/color] [color=q2]H[\/color][\/li][\/ul]", "infobox-contents-0", {dbPage: true});
</script></td></tr></table>"""


def test_page_facts_read_item_starts_achievement_chains_reactions_and_keep_entity_compact(monkeypatch) -> None:
    """Live (2026-10): quest 5089's item start was missing, achievement 6's own step read as quest 6, "React"
    lost who is hostile, and `entity object 1731` carried 4300 spawn coordinates."""
    pages = {
        ("quest", 5089): ITEM_STARTED_QUEST_HTML,
        ("achievement", 6): ACHIEVEMENT_SERIES_HTML,
        ("npc", 448): HOSTILE_NPC_HTML,
        ("npc", 130993): NPC_MAPPER_HTML,
    }
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", lambda self, entity_type, entity_id: pages[(entity_type, entity_id)])

    def facts(*args: str) -> dict:
        return json.loads(runner.invoke(app, ["entity-page", *args]).stdout)["data"]["facts"]

    quest = facts("quest", "5089")
    assert quest["start"] == {"type": "item", "id": 12780, "name": None, "url": "https://www.wowhead.com/item=12780"}
    assert quest["quick_facts"] == ["Side: Alliance", "Start: item 12780"]

    achievement = facts("achievement", "6")
    assert achievement["quick_facts"] == ["Points: 10"]
    assert achievement["series"][0][0] == {
        "position": 1,
        "type": "achievement",
        "id": 6,
        "name": "Level 10",
        "url": "https://www.wowhead.com/achievement=6",
        "current": True,
    }

    assert facts("npc", "448")["quick_facts"] == ["React: Alliance hostile, Horde friendly"]

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", lambda self, entity_type, entity_id, data_env=None: {"name": "Captain Fareeya"})
    entity = json.loads(runner.invoke(app, ["entity", "npc", "130993", "--no-include-comments"]).stdout)["data"]
    assert entity["facts"]["locations"] == [{"zone_id": 9359, "zone": "The Vindicaar", "level": 1, "count": 2}]

def _raise_404(url: str):
    def fetch(self, entity_type: str, entity_id: int, data_env=None):
        request = httpx.Request("GET", url.format(type=entity_type, id=entity_id))
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    return fetch


@pytest.mark.parametrize(
    ("args", "method", "url", "message_start"),
    [
        # The tooltip endpoint 404s on real page types too (`class`, `title`), so it points at entity-page.
        (
            ["entity", "class", "1"],
            "tooltip",
            "https://nether.wowhead.com/tooltip/{type}/{id}",
            "Wowhead's tooltip endpoint has no 'class' 1; if the type is right, `wowhead entity-page class 1` may still answer.",
        ),
        (
            ["entity-page", "foo", "1"],
            "entity_page_html",
            "https://www.wowhead.com/{type}={id}",
            "'foo' is not an entity type this CLI knows",
        ),
    ],
)
def test_a_404_on_an_unknown_entity_type_is_a_usage_error(monkeypatch, args, method, url, message_start) -> None:
    monkeypatch.setattr(f"wowhead_cli.main.WowheadClient.{method}", _raise_404(url))

    result = runner.invoke(app, args)

    assert result.exit_code == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_argument"
    assert error["message"].startswith(message_start)
    assert ". Known entity types: achievement, battle-pet," in error["message"]


def test_an_unknown_entity_type_that_lands_on_a_listing_is_a_usage_error(monkeypatch) -> None:
    """Wowhead redirects `/items=19019` to its item listing instead of answering 404."""
    listing = '<html><head><link rel="canonical" href="https://www.wowhead.com/items"></head><body></body></html>'
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", lambda self, entity_type, entity_id: listing)

    result = runner.invoke(app, ["entity-page", "items", "19019"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["details"] == {"canonical_url": "https://www.wowhead.com/items"}


def test_a_404_on_a_known_entity_type_stays_not_found(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", _raise_404("https://nether.wowhead.com/tooltip/{type}/{id}"))

    result = runner.invoke(app, ["entity", "item", "99999999"])

    assert result.exit_code == 4
    assert json.loads(result.stderr)["error"]["code"] == "not_found"
