"""Entity, entity-page, comments, compare, and linked-preview commands for the wowhead CLI."""

from __future__ import annotations

import json
from pathlib import Path

from wowhead_cli.entities import (
    comparison_entity_record,
    comparison_field_diffs,
    comparison_linked_entities_summary,
    entity_comments_payload,
    entity_linked_entities_payload,
    entity_page_needs_fetch,
    restore_cached_normalization_version,
)
from wowhead_cli.expansion_profiles import resolve_expansion
from wowhead_cli.main import app

from tests.wowhead_testkit import SAMPLE_PAGE_HTML, runner


def test_entity_page_command_returns_links_with_citations(monkeypatch) -> None:
    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity-page", "item", "19019", "--max-links", "10"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert "comments_url" not in payload["entity"]
    assert payload["citations"]["comments"] == "https://www.wowhead.com/item=19019/thunderfury#comments"
    assert payload["linked_entities"]["count"] >= 1
    first = payload["linked_entities"]["items"][0]
    assert "citation_url" in first
    assert "source_url" in first



def test_comments_command_returns_comment_citations(monkeypatch) -> None:
    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["comments", "item", "19019", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert "comments_url" not in payload["entity"]
    assert payload["citations"]["comments"] == "https://www.wowhead.com/item=19019/thunderfury#comments"
    assert payload["comments"][0]["citation_url"].endswith("#comments:id=11")
    assert payload["linked_entities"]["count"] >= 1
    assert payload["linked_entities"]["items"][0]["type"] == "npc"



def test_compare_command_returns_overlap_and_unique_links(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env: int = 11):  # noqa: ANN001
        if entity_id == 19019:
            return {"name": "Thunderfury", "quality": 5, "icon": "inv_sword_39"}
        return {"name": "Maladath", "quality": 4, "icon": "inv_sword_49"}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
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
    assert payload["comparison"]["linked_entities"]["shared_count_total"] == 1
    assert len(payload["comparison"]["linked_entities"]["unique_by_entity"]["item:19019"]) == 1
    assert len(payload["comparison"]["linked_entities"]["unique_by_entity"]["item:19351"]) == 1
    assert payload["comparison"]["fields"]["name"]["all_equal"] is False
    assert payload["entities"][0]["comments"]["top"][0]["citation_url"].endswith("#comments:id=501")
    assert payload["entities"][0]["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert "comments_url" not in payload["entities"][0]["entity"]
    assert "page" not in payload["entities"][0]["citations"]
    assert payload["entities"][0]["citations"]["comments"] == "https://www.wowhead.com/item=19019/thunderfury#comments"
    assert "citation_url" not in payload["comparison"]["linked_entities"]["shared_items"][0]
    assert "citation_url" not in payload["comparison"]["linked_entities"]["unique_by_entity"]["item:19019"][0]
    assert "citations" not in payload



def test_comparison_helper_payloads_are_stable() -> None:
    record, link_set = comparison_entity_record(
        ref="item:19019",
        entity_type="item",
        entity_id=19019,
        canonical_url="https://www.wowhead.com/item=19019/thunderfury",
        tooltip={"name": "Thunderfury", "quality": 5, "icon": "inv_sword_39"},
        metadata={"title": "Thunderfury", "description": "Legendary sword"},
        linked_entities={
            "count": 2,
            "total": 2,
            "truncated": False,
            "items": [
                {"entity_type": "npc", "id": 12056, "url": "https://www.wowhead.com/npc=12056"},
                {"entity_type": "quest", "id": 7786, "url": "https://www.wowhead.com/quest=7786"},
            ],
        },
        raw_comments=[{"id": 1}, {"id": 2}],
        sampled_comments=[{"id": 1, "citation_url": "https://www.wowhead.com/item=19019#comments:id=1"}],
    )
    assert record["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert record["comments"]["count"] == 2
    assert record["linked_entities"]["count"] == 2
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

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        calls.append((self.expansion.key, data_env))
        return {"name": "Thunderfury"}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
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

    def fail_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        raise AssertionError("tooltip should not be called for faction fallback")

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        assert (entity_type, entity_id) == ("faction", 529)
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fail_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "faction", "529", "--no-include-comments", "--linked-entity-preview-limit", "0"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["entity"] == {
        "type": "faction",
        "id": 529,
        "name": "Argent Dawn",
        "page_url": "https://www.wowhead.com/faction=529/argent-dawn",
    }
    assert payload["tooltip"]["text"] == "Argent Dawn Protect Azeroth from the Scourge."
    assert payload["tooltip"]["summary"] == "Protect Azeroth from the Scourge."



def test_entity_recipe_routes_through_spell_tooltip(monkeypatch) -> None:
    tooltip_calls = []

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        tooltip_calls.append((entity_type, entity_id, data_env))
        return {"name": "Seasoned Wolf Kabob", "tooltip": "<b>Seasoned Wolf Kabob</b>"}

    def fail_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        raise AssertionError("entity page should not be fetched")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fail_html)
    result = runner.invoke(app, ["entity", "recipe", "2549", "--no-include-comments", "--linked-entity-preview-limit", "0"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert tooltip_calls == [("spell", 2549, None)]
    assert payload["entity"]["type"] == "recipe"
    assert payload["entity"]["id"] == 2549
    assert payload["entity"]["page_url"] == "https://www.wowhead.com/spell=2549"
    assert payload["entity"]["name"] == "Seasoned Wolf Kabob"



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

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity-page", "item", "19019", "--max-links", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["linked_entities"]["count"] == 1
    assert payload["linked_entities"]["items"][0]["name"] == "Obliterate"
    assert payload["linked_entities"]["items"][0]["sources"] == ["gatherer", "href"]
    assert payload["linked_entities"]["items"][0]["source_kind"] == "gatherer"



def test_entity_supports_excluding_comments(monkeypatch) -> None:
    page_calls = []

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {"name": "Thunderfury"}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        page_calls.append((entity_type, entity_id))
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments", "--linked-entity-preview-limit", "0"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert "comments" not in payload
    assert "linked_entities" not in payload
    assert payload["entity"]["page_url"] == "https://www.wowhead.com/item=19019"
    assert "citations" not in payload
    assert page_calls == []



def test_entity_includes_linked_entity_preview_without_comments(monkeypatch) -> None:
    page_calls = []

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {"name": "Thunderfury"}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        page_calls.append((entity_type, entity_id))
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["linked_entities"]["count"] >= 1
    assert payload["linked_entities"]["counts_by_type"]["npc"] == 1
    assert payload["linked_entities"]["items"][0]["type"] == "npc"
    assert set(payload["linked_entities"]["items"][0].keys()) == {"type", "id", "name", "url"}
    assert payload["linked_entities"]["more_available"] is False
    assert page_calls == [("item", 19019)]



def test_entity_supports_include_all_comments(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {"name": "Thunderfury"}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return SAMPLE_PAGE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--include-all-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["comments"]["count"] == 1
    assert payload["comments"]["all_comments_included"] is True
    assert payload["comments"]["needs_raw_fetch"] is False
    assert "items" in payload["comments"]
    assert "top" not in payload["comments"]
    assert payload["comments"]["items"][0]["id"] == 11



def test_entity_marks_partial_comments_when_more_than_top_limit(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
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

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["comments"]["all_comments_included"] is False
    assert payload["comments"]["needs_raw_fetch"] is True
    assert payload["comments"]["count"] == 4
    assert len(payload["comments"]["top"]) == 3



def test_entity_normalizes_tooltip_name_and_html(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {"name": "Thunderfury", "tooltip": "<b>Legendary</b> weapon", "quality": 5}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["entity"]["name"] == "Thunderfury"
    assert payload["tooltip"]["quality"] == 5
    assert payload["tooltip"]["html"] == "<b>Legendary</b> weapon"
    assert payload["tooltip"]["text"] == "Legendary weapon"
    assert payload["tooltip"]["summary"] == "Legendary weapon"
    assert "name" not in payload["tooltip"]
    assert "tooltip" not in payload["tooltip"]



def test_entity_cleans_spell_tooltip_artifacts_and_builds_summary(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {
            "name": "Obliterate",
            "tooltip": (
                "<a href=\"/spell=49020/obliterate\"><b>Obliterate</b></a>"
                "<div>Talent</div><div>Instant</div>"
                "<div>A brutal attack [that deals [(105.751% of Attack Power)] Physical and "
                "[(105.751% of Attack Power)] Frost damage.] Physical and Frost damage.]</div>"
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "spell", "49020"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["tooltip"]["text"] == "Obliterate Talent Instant A brutal attack Physical and Frost damage."
    assert payload["tooltip"]["summary"] == "A brutal attack Physical and Frost damage."



def test_entity_item_summary_prefers_effect_text_over_item_metadata(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {
            "name": "Thunderfury",
            "tooltip": (
                "<table><tr><td><b>Thunderfury</b><br>Item Level 40<br>Binds when picked up</td></tr></table>"
                "<table><tr><td>Chance on hit: Blasts your enemy with lightning and slows its attack speed.</td></tr></table>"
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["tooltip"]["summary"] == "Chance on hit: Blasts your enemy with lightning and slows its attack speed."



def test_entity_mount_summary_prefers_use_text_over_mount_metadata(monkeypatch) -> None:
    # Mount pages resolve through the tooltip redirect, so the entity command calls
    # tooltip_with_metadata (not tooltip) and needs the final tooltip URL.
    def fake_tooltip_with_metadata(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        payload = {
            "name": "Grand Expedition Yak",
            "tooltip": (
                "<table><tr><td><b>Grand Expedition Yak</b><br>Item Level 10<br>Mount (Account-wide)</td></tr></table>"
                "<table><tr><td>Use: Teaches you how to summon this three-person mount with vendors.</td></tr></table>"
            ),
        }
        return payload, f"https://nether.wowhead.com/tooltip/{entity_type}/{entity_id}?dataEnv=1"

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip_with_metadata", fake_tooltip_with_metadata)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "mount", "460"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["tooltip"]["summary"] == "Use: Teaches you how to summon this three-person mount with vendors."



def test_entity_item_tooltip_text_formats_money_and_stat_spacing(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {
            "name": "Maladath",
            "tooltip": (
                "<table><tr><td><b>Maladath</b><br>+ 4 Parry<br>+ 2 Haste<br>"
                "Sell Price: 86 98</td></tr></table>"
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19351"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["tooltip"]["text"] == "Maladath +4 Parry +2 Haste Sell Price: 86g 98s"



def test_entity_item_style_tooltip_text_drops_flavor_quotes_and_normalizes_parenthetical_level(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {
            "name": "Grand Expedition Yak",
            "tooltip": (
                "<table><tr><td><b>Grand Expedition Yak</b><br>Requires level 1 to 90 ( 90)<br>"
                "Sell Price: 30,000<br>"
                "\"These beasts of burden are known to carry over five times their own weight.\"<br>"
                "Vendor: Uncle Bigpocket<br>Cost: 120000</td></tr></table>"
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "84101"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["tooltip"]["text"] == (
        "Grand Expedition Yak Requires level 1 to 90 (90) Sell Price: 30,000g Vendor: Uncle Bigpocket Cost: 120000g"
    )



def test_entity_tooltip_summary_strips_leading_entity_name(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {
            "name": "Fairbreeze Favors",
            "tooltip": (
                "<table><tr><td><b>Fairbreeze Favors</b></td></tr></table>"
                "<table><tr><td>Help restore order in Fairbreeze Village.</td></tr></table>"
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "quest", "86739"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["tooltip"]["text"] == "Fairbreeze Favors Help restore order in Fairbreeze Village."
    assert payload["tooltip"]["summary"] == "Help restore order in Fairbreeze Village."



def test_entity_uses_normalized_entity_cache_between_invocations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path / "cache"))
    calls = {"tooltip": 0}

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        calls["tooltip"] += 1
        return {
            "name": "Thunderfury",
            "tooltip": "<table><tr><td><b>Thunderfury</b><br>Legendary weapon</td></tr></table>",
        }

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        raise AssertionError("entity_page_html should not be used when comments and preview are disabled")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)

    args = ["entity", "item", "19019", "--no-include-comments", "--linked-entity-preview-limit", "0"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0
    second = runner.invoke(app, args)
    assert second.exit_code == 0

    assert calls["tooltip"] == 1
    assert json.loads(first.stdout) == json.loads(second.stdout)



def test_entity_preview_prefers_gatherer_name_when_href_label_missing(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
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

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["linked_entities"]["items"][0] == {
        "type": "spell",
        "id": 49020,
        "name": "Obliterate",
        "url": "https://www.wowhead.com/spell=49020",
    }



def test_entity_preview_prefers_multi_source_links_over_single_source_peers(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
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

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments", "--linked-entity-preview-limit", "2"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert [row["id"] for row in payload["linked_entities"]["items"]] == [49020, 49184]



def test_entity_preview_fetch_more_command_scales_with_known_count(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {"name": "Valorstones"}

    links = "\n".join(f'<a href="/item={200000 + idx}">Item {idx}</a>' for idx in range(250))
    html = f"""
    <html><head>
      <link rel="canonical" href="https://www.wowhead.com/currency=3008/valorstones">
    </head><body>
      {links}
      <script>var lv_comments0 = [];</script>
    </body></html>
    """

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "currency", "3008", "--no-include-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["linked_entities"]["count"] == 250
    assert payload["linked_entities"]["fetch_more_command"] == "wowhead entity-page currency 3008 --max-links 250"



def test_entity_preview_suppresses_low_signal_names(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
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

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "npc", "448", "--no-include-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["linked_entities"]["items"][0] == {
        "type": "npc",
        "id": 34942,
        "name": "Memory of Hogger",
        "url": "https://www.wowhead.com/npc=34942",
    }
    assert payload["linked_entities"]["items"][-1]["name"] is None



def test_entity_preview_prefers_diverse_high_value_types(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
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

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "item", "1", "--no-include-comments", "--linked-entity-preview-limit", "4"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert [row["type"] for row in payload["linked_entities"]["items"]] == ["npc", "quest", "spell", "item"]



def test_currency_preview_demotes_items_below_more_actionable_types(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
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

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["entity", "currency", "3008", "--no-include-comments", "--linked-entity-preview-limit", "4"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert [row["type"] for row in payload["linked_entities"]["items"]] == ["npc", "quest", "spell", "object"]



def test_compare_respects_expansion_flag_for_generated_urls(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {"name": f"Item {entity_id}", "quality": 1, "icon": "inv_misc_questionmark"}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
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
    assert payload["expansion"] == "wotlk"
    assert [row["entity"]["page_url"] for row in payload["entities"]] == [
        "https://www.wowhead.com/wotlk/item=1",
        "https://www.wowhead.com/wotlk/item=2",
    ]
    assert payload["comparison"]["linked_entities"]["shared_items"][0]["url"] == "https://www.wowhead.com/wotlk/npc=12056"
    assert "citation_url" not in payload["comparison"]["linked_entities"]["shared_items"][0]



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

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)

    default_result = runner.invoke(app, ["--expansion", "ptr", "entity-page", "item", "19019", "--max-links", "1"])
    assert default_result.exit_code == 0
    default_payload = json.loads(default_result.stdout)
    assert default_payload["normalize_canonical_to_expansion"] is False
    assert default_payload["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury-blessed-blade-of-the-windseeker"

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
    assert normalized_payload["normalize_canonical_to_expansion"] is True
    assert normalized_payload["entity"]["page_url"] == "https://www.wowhead.com/ptr/item=19019/thunderfury-blessed-blade-of-the-windseeker"



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

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
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
    assert payload["normalize_canonical_to_expansion"] is True
    assert payload["entity"]["page_url"] == "https://www.wowhead.com/ptr/item=19019/thunderfury-blessed-blade-of-the-windseeker"
    assert payload["comments"][0]["citation_url"] == "https://www.wowhead.com/ptr/item=19019/thunderfury-blessed-blade-of-the-windseeker#comments:id=11"


def test_restore_cached_normalization_version_moves_legacy_top_level_version() -> None:
    """Entries cached before the envelope kept `wowhead.entity.v1` at the top level; it must survive under `normalized`."""
    cached = {"schema_version": "wowhead.entity.v1", "entity": {"id": 1}, "normalized": {"item": {"id": 1}}}
    restored = restore_cached_normalization_version(cached)
    assert restored["normalized"] == {"schema_version": "wowhead.entity.v1", "item": {"id": 1}}
    assert restored["entity"] == {"id": 1}

    already_migrated = {"schema_version": "1", "normalized": {"schema_version": "wowhead.entity.v1", "item": {}}}
    assert restore_cached_normalization_version(already_migrated) is already_migrated
    without_normalized = {"schema_version": "wowhead.entity.v1", "entity": {}}
    assert restore_cached_normalization_version(without_normalized) is without_normalized


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
