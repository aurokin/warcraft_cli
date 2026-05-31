from __future__ import annotations

import json

from typer.testing import CliRunner
from wowhead_cli.citation_pack import citation_pack_from_compare, citation_pack_from_entity
from wowhead_cli.main import app

runner = CliRunner()


def test_citation_pack_from_entity_collects_page_and_comment_anchors() -> None:
    pack = citation_pack_from_entity(
        {
            "entity": {"type": "item", "id": 19019, "page_url": "https://www.wowhead.com/item=19019"},
            "tooltip": {"name": "Thunderfury", "quality": 5},
            "citations": {"comments": "https://www.wowhead.com/item=19019#comments"},
            "comments": {
                "top": [
                    {
                        "id": 1,
                        "body": "Great sword",
                        "citation_url": "https://www.wowhead.com/item=19019#comments:id=1",
                    }
                ]
            },
        }
    )
    assert pack["source_count"] >= 2
    claims = {row["claim"] for row in pack["anchors"]}
    assert "tooltip.name" in claims
    assert "comments.top[0].body" in claims
    comment_anchor = next(row for row in pack["anchors"] if row["claim"] == "comments.top[0].body")
    assert comment_anchor["url"] == "https://www.wowhead.com/item=19019#comments:id=1"
    assert comment_anchor["url"].count("#") == 1


def test_citation_pack_from_compare_merges_entities_and_comparison() -> None:
    pack = citation_pack_from_compare(
        {
            "entities": [
                {
                    "ref": "item:19019",
                    "entity": {"type": "item", "id": 19019, "page_url": "https://www.wowhead.com/item=19019"},
                    "tooltip": {"name": "Thunderfury"},
                    "comments": {
                        "top": [
                            {"id": 1, "body": "c", "citation_url": "https://www.wowhead.com/item=19019#c:id=1"}
                        ]
                    },
                },
                {
                    "ref": "item:17771",
                    "entity": {"type": "item", "id": 17771, "page_url": "https://www.wowhead.com/item=17771"},
                    "summary": {"name": "Elementium"},
                },
                {"ref": ""},
                "not a dict",
            ],
            "comparison": {
                "linked_entities": {
                    "shared_items": [{"url": "https://www.wowhead.com/spell=1"}, {"name": "nourl"}, "bad"],
                    "unique_by_entity": {
                        "item:19019": [{"url": "https://www.wowhead.com/item=2"}],
                        "item:17771": [{"url": "https://www.wowhead.com/item=3"}, {"name": "x"}],
                    },
                },
                "fields": {
                    "quality": {"values": {"item:19019": 5, "item:17771": 4, "missingref": 3}},
                    "name": {"values": {"item:19019": "T", "item:17771": "E"}},
                },
            },
        }
    )

    # build_citation_pack sorts + dedupes, so output is deterministic regardless of collection order.
    assert pack["source_count"] == 6
    assert pack["anchor_count"] == 7
    source_keys = [row["key"] for row in pack["sources"]]
    assert source_keys == [
        "comparison.linked_entities.shared_items[0]",
        "comparison.linked_entities.unique_by_entity.item:17771[0]",
        "comparison.linked_entities.unique_by_entity.item:19019[0]",
        "item:17771.page",
        "item:19019.comments",
        "item:19019.page",
    ]
    # ref-prefixed nested entity source preserves the comment citation_url (the colliding key winner).
    comments_source = next(row for row in pack["sources"] if row["key"] == "item:19019.comments")
    assert comments_source["url"] == "https://www.wowhead.com/item=19019#c:id=1"
    # the #-fragment anchor rewrite survives the ref-prefixed merge.
    comment_anchor = next(row for row in pack["anchors"] if row["claim"] == "item:19019.comments.top[0].body")
    assert comment_anchor["anchor"] == "#c:id=1"
    assert comment_anchor["url"] == "https://www.wowhead.com/item=19019#c:id=1"
    # comparison.fields claims are emitted for every ref present in an entity, skipping unknown refs.
    field_claims = {row["claim"] for row in pack["anchors"] if row["claim"].startswith("comparison.fields.")}
    assert field_claims == {
        "comparison.fields.name.item:17771",
        "comparison.fields.name.item:19019",
        "comparison.fields.quality.item:17771",
        "comparison.fields.quality.item:19019",
    }


def test_entity_command_can_emit_citation_pack(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {"name": "Thunderfury", "quality": 5}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return """
        <html><head>
          <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury">
        </head><body>
          <script>var lv_comments0 = [{"id": 1, "number": 0, "user": "A", "body": "Nice", "date": "2024-01-01T00:00:00-06:00", "rating": 1, "nreplies": 0, "replies": []}];</script>
        </body></html>
        """

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)

    result = runner.invoke(app, ["--citation-pack", "entity", "item", "19019", "--no-include-comments"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert "citation_pack" in payload
    assert payload["citation_pack"]["source_count"] >= 1
    assert payload["citation_pack"]["anchors"]
