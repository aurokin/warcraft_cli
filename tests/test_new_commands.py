from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import typer
from typer.testing import CliRunner
from wowhead_cli.expansion_profiles import resolve_expansion
from wowhead_cli.main import (
    _comparison_entity_record,
    _comparison_field_diffs,
    _comparison_linked_entities_summary,
    _entity_comments_payload,
    _entity_linked_entities_payload,
    _entity_page_needs_fetch,
    _exact_match_score,
    _filtered_guide_category_rows,
    _guide_comment_matches,
    _guide_export_manifest,
    _guide_gatherer_matches,
    _guide_linked_entity_matches,
    _guide_navigation_matches,
    _guide_query_top_matches,
    _guide_row_matches_filters,
    _guide_section_matches,
    _guides_payload,
    _is_filtered_high_confidence,
    _is_high_confidence_exact_match,
    _is_high_confidence_score,
    _is_medium_confidence_score,
    _popularity_score,
    _prefix_and_contains_score,
    _search_result_score_and_reasons,
    _term_match_score,
    _type_hint_score,
    _validated_guides_filters,
    _write_guide_export_assets,
    app,
)
from wowhead_cli.wowhead_client import WowheadClient

runner = CliRunner()

SAMPLE_PAGE_HTML = """
<html>
  <head>
    <meta property="og:title" content="Thunderfury">
    <meta name="description" content="Legendary sword">
    <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury">
    <script type="application/json" id="data.pageMeta">{"page":"item","serverTime":"2026-02-19T09:00:00-06:00","availableDataEnvs":[1,11],"envDomain":"wowhead.com"}</script>
  </head>
  <body>
    <a href="/npc=12056/baron-geddon">Baron Geddon</a>
    <script>
      WH.Gatherer.addData(3, 1, {"19019":{"name_enus":"Thunderfury"}});
      var lv_comments0 = [{"id": 11, "number": 0, "user": "A", "body": "Useful", "date": "2024-01-01T00:00:00-06:00", "rating": 7, "nreplies": 0, "replies": []}];
    </script>
  </body>
</html>
"""

SAMPLE_GUIDE_HTML = """
<html>
  <head>
    <meta property="og:title" content="Frost Death Knight DPS Guide - Midnight">
    <meta name="description" content="Guide description">
    <link rel="canonical" href="https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps">
    <script type="application/json" id="data.pageMeta">{"page":"guide","serverTime":"2026-02-19T09:00:00-06:00","availableDataEnvs":[1,2,3],"envDomain":"wowhead.com"}</script>
    <script type="application/ld+json">{"@context":"http://schema.org","@type":"Article","headline":"Frost Death Knight DPS Guide - Midnight","datePublished":"2015-03-11T18:36:20-05:00","dateModified":"2026-02-25T17:32:29-06:00","author":{"@type":"Person","name":"khazakdk"}}</script>
    <script type="application/json" id="data.guide.author">"khazakdk"</script>
    <script type="application/json" id="data.guide.author.profiles">{"discord":"https://discord.gg/acherus","youtube":"Khazakdk"}</script>
    <script type="application/json" id="data.guide.aboutTheAuthor.embedData">{"username":"khazakdk","bio":"Writes DK guides."}</script>
    <script type="application/json" id="data.wowhead-guide-nav">"[b]Spec Basics[/b][ul][li][url=guide/classes/death-knight/frost/overview-pve-dps]Overview[/url][/li][li][url=guide/classes/death-knight/frost/bis-gear]BiS Gear[/url][/li][/ul]"</script>
    <script type="application/json" id="data.wowhead-guide-body">"[h2 toc=\\"Overview\\"]Frost Death Knight Overview[/h2]\\r\\nWelcome to the guide.\\r\\n[h3]Strengths[/h3]\\r\\n[ul][li][spell=49020]Big damage[/li][/ul]\\r\\n[url=guide/classes/death-knight/frost/bis-gear]Best in Slot Gear[/url]"</script>
  </head>
  <body>
    <div class="interior-sidebar-rating-text">4.6/5 (<span class="guide-user-actions-rating-votes" id="guiderating-votes">70</span> Votes)</div>
    <script>
      WH.markup.printHtml(WH.getPageData("wowhead-guide-nav"), "interior-sidebar-related-markup");
      WH.markup.printHtml(WH.getPageData("wowhead-guide-body"), "guide-body", {"allow":30});
      $(document).ready(function () {
        $('#guiderating').append(GetStars(4.61597, false, 0, 3143));
      });
      WH.Gatherer.addData(3, 1, {"249277":{"name_enus":"Bellamy's Final Judgement"}});
      var lv_comments0 = [{"id": 91, "number": 0, "user": "A", "body": "Solid guide", "date": "2024-01-01T00:00:00-06:00", "rating": 7, "nreplies": 0, "replies": []}];
    </script>
    <a href="/item=249277/bellamys-final-judgement">Bellamy's Final Judgement</a>
    <a href="/spell=49020/obliterate">Obliterate</a>
  </body>
</html>
"""

SAMPLE_NEWS_HTML = """
<html>
  <head>
    <script type="application/json" id="data.news.newsData">
      {
        "newsPosts": [
          {
            "id": 380785,
            "title": "Midnight Hotfixes for March 13th",
            "author": "Staff",
            "authorPage": "/author/staff",
            "posted": "2026-03-13T12:34:56-06:00",
            "postedFull": "2026-03-13T12:34:56-06:00",
            "postedShort": "Mar 13",
            "postUrl": "/news/midnight-hotfixes-380785",
            "preview": "<p>Class bugfixes and more.</p>",
            "thumbnailUrl": "https://wow.zamimg.com/images/wow/icons/large/inv_misc_questionmark.jpg",
            "typeId": 1,
            "typeName": "News"
          },
          {
            "id": 380700,
            "title": "Older Tuning Roundup",
            "author": "Staff",
            "authorPage": "/author/staff",
            "posted": "2026-03-10T09:00:00-06:00",
            "postedFull": "2026-03-10T09:00:00-06:00",
            "postedShort": "Mar 10",
            "postUrl": "/news/older-tuning-roundup-380700",
            "preview": "<p>Older tuning notes.</p>",
            "thumbnailUrl": null,
            "typeId": 1,
            "typeName": "News"
          }
        ],
        "pinnedPosts": [],
        "totalPages": 1637,
        "gathered": 2
      }
    </script>
  </head>
</html>
"""

SAMPLE_BLUE_TRACKER_HTML = """
<html>
  <head>
    <script type="application/json" id="data.blueTracker.default">
      {
        "entries": [
          {
            "id": 610948,
            "title": "Class Tuning Incoming -- 18 March",
            "posted": "2026-03-12T22:00:00-06:00",
            "author": "Blizzard",
            "region": "eu",
            "forumArea": "Community",
            "forum": "General Discussion",
            "url": "/blue-tracker/topic/eu/class-tuning-incoming-18-march-610948",
            "body": "<p>Druid and Priest updates.</p>",
            "blueposts": 2,
            "posts": 24,
            "blues": 2,
            "score": 15,
            "maxscore": 15,
            "lastPost": "2026-03-12T23:00:00-06:00",
            "lastblue": "2026-03-12T23:00:00-06:00",
            "jobtitle": "Community Manager"
          },
          {
            "id": 610900,
            "title": "Auction House Maintenance",
            "posted": "2026-03-08T08:30:00-06:00",
            "author": "Blizzard",
            "region": "us",
            "forumArea": "Support",
            "forum": "Customer Support",
            "url": "/blue-tracker/topic/us/auction-house-maintenance-610900",
            "body": "<p>Scheduled maintenance window.</p>",
            "blueposts": 1,
            "posts": 8,
            "blues": 1,
            "score": 3,
            "maxscore": 3,
            "lastPost": "2026-03-08T09:00:00-06:00",
            "lastblue": "2026-03-08T09:00:00-06:00",
            "jobtitle": "Support"
          }
        ],
        "totalTopics": 33506,
        "page": 1,
        "baseUrl": "/blue-tracker"
      }
    </script>
  </head>
</html>
"""

SAMPLE_GUIDE_CATEGORY_HTML = """
<html>
  <body>
    <script>
      new Listview({"id":"guides","template":"guide","data":[
        {
          "id":33131,
          "name":"Devourer Demon Hunter Build Cheat Sheet",
          "title":"Devourer Demon Hunter Build Cheat Sheet - Midnight",
          "author":"VooDooSaurus",
          "authorPage":false,
          "patch":120001,
          "category":1,
          "categoryNames":["Classes"],
          "categoryPath":"classes",
          "when":"2026-01-18 14:45:27",
          "lastEdit":"2026-03-07T11:18:18-06:00",
          "rating":-1,
          "nvotes":1,
          "class":12,
          "spec":1480,
          "comments":0,
          "url":"https://www.wowhead.com/guide/classes/demon-hunter/devourer/cheat-sheet"
        },
        {
          "id":32000,
          "name":"Frost Death Knight Guide",
          "title":"Frost Death Knight Guide - Midnight",
          "author":"Khazakdk",
          "authorPage":false,
          "patch":120001,
          "category":1,
          "categoryNames":["Classes"],
          "categoryPath":"classes",
          "when":"2026-01-01 09:00:00",
          "lastEdit":"2026-03-01T10:00:00-06:00",
          "rating":5,
          "nvotes":10,
          "class":6,
          "spec":251,
          "comments":2,
          "url":"https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"
        }
      ]});
    </script>
  </body>
</html>
"""

SAMPLE_TALENT_CALC_HTML = """
<html>
  <head>
    <title>Balance Druid Midnight Talent Calculator - World of Warcraft</title>
    <meta property="og:title" content="Balance Druid Midnight Talent Calculator - World of Warcraft">
    <meta name="description" content="Balance talent build planning.">
    <link rel="canonical" href="https://www.wowhead.com/talent-calc/druid/balance">
    <script type="application/json" id="data.wow.talentCalcDragonflight.live.talentBuilds">
      {
        "117": {"id": 117, "isListed": true, "name": "Leveling", "spec": 102, "hash": "AAA111"},
        "118": {"id": 118, "isListed": true, "name": "Mythic+", "spec": 102, "hash": "BBB222"}
      }
    </script>
  </head>
</html>
"""

SAMPLE_PROFESSION_TREE_HTML = """
<html>
  <head>
    <title>Alchemy Midnight Profession Tree Calculator - World of Warcraft</title>
    <meta property="og:title" content="Alchemy Midnight Profession Tree Calculator - World of Warcraft">
    <meta name="description" content="Alchemy profession planning.">
    <link rel="canonical" href="https://www.wowhead.com/profession-tree-calc/alchemy">
  </head>
</html>
"""

SAMPLE_DRESSING_ROOM_HTML = """
<html>
  <head>
    <title>Dressing Room - World of Warcraft</title>
    <meta property="og:title" content="Dressing Room - World of Warcraft">
    <meta name="description" content="Try out armor sets on any World of Warcraft character.">
    <link rel="canonical" href="https://www.wowhead.com/dressing-room">
  </head>
</html>
"""

SAMPLE_PROFILER_HTML = """
<html>
  <head>
    <title>Profiler - Wowhead</title>
    <meta property="og:title" content="Profiler - Wowhead">
    <meta name="description" content="Load your character's Blizzard Battle.net profile or create a custom list.">
    <link rel="canonical" href="https://www.wowhead.com/list">
  </head>
</html>
"""

SAMPLE_NEWS_POST_HTML = """
<html>
  <head>
    <title>Midnight Hotfixes for March 13th</title>
    <meta property="og:title" content="Midnight Hotfixes for March 13th">
    <meta name="description" content="Class bugfixes and more.">
    <link rel="canonical" href="https://www.wowhead.com/news/midnight-hotfixes-380785">
    <script type="application/json" id="data.newsPost.aboutTheAuthor.embedData">
      {"username":"staff","fullName":"Staff","title":"Author","bio":"Writes news."}
    </script>
    <script type="application/json" id="data.WH.News.recentPosts">
      {
        "news": [
          {
            "author": "Jaydaa",
            "name": "Another Hotfix Roundup",
            "newsTypeName": "Live",
            "pinned": false,
            "time": "3h",
            "url": "/news/another-hotfix-roundup-380700"
          }
        ],
        "blueTracker": [
          {
            "blue": true,
            "name": "Class Tuning Incoming -- 18 March",
            "news": false,
            "region": "eu",
            "time": "1h",
            "url": "/blue-tracker/topic/eu/610948"
          }
        ],
        "video": false
      }
    </script>
  </head>
  <body>
    <script>
      WH.markup.printHtml("[b]March 13, 2026[/b]\\r\\n[h2]Classes[/h2]\\r\\nDeath Knight fixes.");
    </script>
  </body>
</html>
"""

SAMPLE_BLUE_TOPIC_HTML = """
<html>
  <head>
    <title>Class Tuning Incoming -- 18 March - General Discussion - EU - Blue Tracker - World of Warcraft</title>
    <meta property="og:title" content="Class Tuning Incoming -- 18 March - General Discussion - EU - Blue Tracker - World of Warcraft">
    <meta name="description" content="The first few days of Midnight max-level play have given us data.">
    <link rel="canonical" href="https://www.wowhead.com/blue-tracker/topic/eu/class-tuning-incoming-18-march-610948">
    <script type="application/json" id="data.blueTracker.topic">
      {
        "entries": [
          {
            "post": 6200022,
            "topic": 610948,
            "author": "Kaivax",
            "authorUrl": "/blue-tracker/author/Kaivax",
            "avatar": "/avatar.png",
            "body": "<p>The first few days of Midnight max-level play have given us data.</p>",
            "posted": "2026-03-12T22:00:00-06:00",
            "date": "2026-03-12T22:00:00-06:00",
            "updated": "2026-03-12T23:00:00-06:00",
            "region": "eu",
            "forumArea": "General Discussion",
            "forumAreaSlug": "wow",
            "forum": "General Discussion",
            "jobtitle": "Community Manager",
            "blue": true,
            "system": true,
            "index": 1
          }
        ]
      }
    </script>
  </head>
</html>
"""


def _write_bundle_fixture(
    root: Path,
    *,
    dir_name: str,
    guide_id: int,
    title: str,
    expansion: str = "retail",
    sections: list[dict[str, object]] | None = None,
    analysis_surfaces: list[dict[str, object]] | None = None,
    navigation_links: list[dict[str, object]] | None = None,
    linked_entities: list[dict[str, object]] | None = None,
    gatherer_entities: list[dict[str, object]] | None = None,
    comments: list[dict[str, object]] | None = None,
) -> Path:
    bundle_dir = root / dir_name
    bundle_dir.mkdir(parents=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    sections = sections or []
    analysis_surfaces = analysis_surfaces or []
    navigation_links = navigation_links or []
    linked_entities = linked_entities or []
    gatherer_entities = gatherer_entities or []
    comments = comments or []

    files = {
        "sections_jsonl": "sections.jsonl",
        "analysis_surfaces_jsonl": "analysis-surfaces.jsonl",
        "navigation_links_jsonl": "navigation-links.jsonl",
        "linked_entities_jsonl": "linked-entities.jsonl",
        "gatherer_entities_jsonl": "gatherer-entities.jsonl",
        "comments_jsonl": "comments.jsonl",
    }
    (bundle_dir / files["sections_jsonl"]).write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in sections),
        encoding="utf-8",
    )
    (bundle_dir / files["analysis_surfaces_jsonl"]).write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in analysis_surfaces),
        encoding="utf-8",
    )
    (bundle_dir / files["navigation_links_jsonl"]).write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in navigation_links),
        encoding="utf-8",
    )
    (bundle_dir / files["linked_entities_jsonl"]).write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in linked_entities),
        encoding="utf-8",
    )
    (bundle_dir / files["gatherer_entities_jsonl"]).write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in gatherer_entities),
        encoding="utf-8",
    )
    (bundle_dir / files["comments_jsonl"]).write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in comments),
        encoding="utf-8",
    )

    manifest = {
        "export_version": 2,
        "output_dir": str(bundle_dir),
        "exported_at": now,
        "guide_fetched_at": now,
        "expansion": expansion,
        "guide": {"id": guide_id, "page_url": f"https://www.wowhead.com/guide={guide_id}"},
        "page": {
            "title": title,
            "canonical_url": f"https://www.wowhead.com/guide/{guide_id}",
        },
        "counts": {
            "sections": len(sections),
            "analysis_surfaces": len(analysis_surfaces),
            "navigation_links": len(navigation_links),
            "linked_entities": len(linked_entities),
            "gatherer_entities": len(gatherer_entities),
            "hydrated_entities": 0,
            "comments": len(comments),
        },
        "hydration": {
            "enabled": False,
            "types": [],
            "limit": 0,
            "hydrated_at": None,
            "source_counts": {},
        },
        "files": files,
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle_dir


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
    record, link_set = _comparison_entity_record(
        ref="item:19019",
        entity_type="item",
        entity_id=19019,
        canonical_url="https://www.wowhead.com/item=19019/thunderfury",
        tooltip={"name": "Thunderfury", "quality": 5, "icon": "inv_sword_39"},
        metadata={"title": "Thunderfury", "description": "Legendary sword"},
        deduped_links=[
            {"entity_type": "npc", "id": 12056, "url": "https://www.wowhead.com/npc=12056"},
            {"entity_type": "quest", "id": 7786, "url": "https://www.wowhead.com/quest=7786"},
        ],
        raw_comments=[{"id": 1}, {"id": 2}],
        sampled_comments=[{"id": 1, "citation_url": "https://www.wowhead.com/item=19019#comments:id=1"}],
    )
    assert record["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert record["comments"]["count"] == 2
    assert record["linked_entities"]["count"] == 2
    assert link_set == {("npc", 12056), ("quest", 7786)}

    fields = _comparison_field_diffs(
        [
            {"ref": "item:19019", "summary": {"name": "Thunderfury", "quality": 5}},
            {"ref": "item:19351", "summary": {"name": "Maladath", "quality": 5}},
        ],
        comparable_fields=["name", "quality"],
    )
    assert fields["name"]["all_equal"] is False
    assert fields["quality"]["all_equal"] is True

    linked = _comparison_linked_entities_summary(
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


def test_expansions_command_exposes_profiles() -> None:
    result = runner.invoke(app, ["expansions"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["default"] == "retail"
    keys = {row["key"] for row in payload["profiles"]}
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
    assert payload["expansion"] == "wotlk"
    assert payload["search_url"].startswith("https://www.wowhead.com/wotlk/search?q=")


def test_news_command_filters_by_query_and_date(monkeypatch) -> None:
    def fake_news_page(self, *, page: int = 1):  # noqa: ANN001
        assert page == 1
        return SAMPLE_NEWS_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.news_page_html", fake_news_page)

    result = runner.invoke(
        app,
        [
            "news",
            "hotfixes",
            "--date-from",
            "2026-03-11",
            "--limit",
            "5",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == 380785
    assert payload["results"][0]["preview"] == "Class bugfixes and more."
    assert payload["scan"]["pages_scanned"] == 1
    assert payload["scan"]["total_pages"] == 1637
    assert payload["news_url"] == "https://www.wowhead.com/news"
    assert payload["facets"]["authors"] == ["Staff"]
    assert payload["facets"]["types"] == ["News"]


def test_news_command_filters_by_author_and_type(monkeypatch) -> None:
    def fake_news_page(self, *, page: int = 1):  # noqa: ANN001
        assert page == 1
        return SAMPLE_NEWS_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.news_page_html", fake_news_page)

    result = runner.invoke(
        app,
        [
            "news",
            "--author",
            "staff",
            "--type",
            "news",
            "--limit",
            "5",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["filters"]["authors"] == ["staff"]
    assert payload["filters"]["types"] == ["news"]
    assert payload["count"] == 2
    assert payload["facets"]["authors"] == ["Staff"]
    assert payload["facets"]["types"] == ["News"]


def test_blue_tracker_command_filters_by_topic_and_date(monkeypatch) -> None:
    def fake_blue_page(self, *, page: int = 1):  # noqa: ANN001
        assert page == 1
        return SAMPLE_BLUE_TRACKER_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.blue_tracker_page_html", fake_blue_page)

    result = runner.invoke(
        app,
        [
            "blue-tracker",
            "druid",
            "--date-from",
            "2026-03-10",
            "--limit",
            "5",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == 610948
    assert payload["results"][0]["region"] == "eu"
    assert payload["results"][0]["body_preview"] == "Druid and Priest updates."
    assert payload["scan"]["pages_scanned"] == 1
    assert payload["scan"]["total_pages"] == 671
    assert payload["blue_tracker_url"] == "https://www.wowhead.com/blue-tracker"
    assert payload["facets"]["regions"] == ["eu"]
    assert payload["facets"]["forums"] == ["General Discussion"]


def test_blue_tracker_command_filters_by_author_region_and_forum(monkeypatch) -> None:
    def fake_blue_page(self, *, page: int = 1):  # noqa: ANN001
        assert page == 1
        return SAMPLE_BLUE_TRACKER_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.blue_tracker_page_html", fake_blue_page)

    result = runner.invoke(
        app,
        [
            "blue-tracker",
            "--author",
            "blizzard",
            "--region",
            "eu",
            "--forum",
            "general discussion",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["filters"]["authors"] == ["blizzard"]
    assert payload["filters"]["regions"] == ["eu"]
    assert payload["filters"]["forums"] == ["general discussion"]
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == 610948
    assert payload["facets"]["authors"] == ["Blizzard"]


def test_blue_tracker_command_rejects_invalid_date_range(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.blue_tracker_page_html", lambda self, page=1: SAMPLE_BLUE_TRACKER_HTML)
    result = runner.invoke(
        app,
        [
            "blue-tracker",
            "--date-from",
            "2026-03-13",
            "--date-to",
            "2026-03-01",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "invalid_argument"


def test_guides_command_returns_category_rows(monkeypatch) -> None:
    def fake_guides_page(self, category: str):  # noqa: ANN001
        assert category == "classes"
        return SAMPLE_GUIDE_CATEGORY_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_category_page_html", fake_guides_page)
    result = runner.invoke(app, ["guides", "classes", "death knight", "--limit", "5"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["category"] == "classes"
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == 32000
    assert payload["results"][0]["url"].endswith("/frost/overview-pve-dps")
    assert payload["facets"]["authors"] == ["Khazakdk"]


def test_guides_command_filters_by_author_and_patch(monkeypatch) -> None:
    def fake_guides_page(self, category: str):  # noqa: ANN001
        assert category == "classes"
        return SAMPLE_GUIDE_CATEGORY_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_category_page_html", fake_guides_page)
    result = runner.invoke(
        app,
        [
            "guides",
            "classes",
            "--author",
            "khazakdk",
            "--patch-min",
            "120001",
            "--updated-after",
            "2026-02-01",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["filters"]["authors"] == ["khazakdk"]
    assert payload["filters"]["patch_min"] == 120001
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == 32000


def test_guides_command_sorts_by_rating(monkeypatch) -> None:
    def fake_guides_page(self, category: str):  # noqa: ANN001
        assert category == "classes"
        return SAMPLE_GUIDE_CATEGORY_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_category_page_html", fake_guides_page)
    result = runner.invoke(app, ["guides", "classes", "--sort", "rating", "--limit", "2"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["filters"]["sort"] == "rating"
    assert [row["id"] for row in payload["results"]] == [32000, 33131]


def test_talent_calc_command_decodes_url_and_embedded_builds(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc", "druid/balance/ABC123", "--listed-build-limit", "5"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tool"]["class_slug"] == "druid"
    assert payload["tool"]["spec_slug"] == "balance"
    assert payload["tool"]["build_code"] == "ABC123"
    assert payload["tool"]["state_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert payload["build_identity"]["status"] == "inferred"
    assert payload["build_identity"]["class_spec_identity"]["identity"] == {"actor_class": "druid", "spec": "balance"}
    assert payload["listed_builds"]["count"] == 2
    assert payload["listed_builds"]["items"][0]["name"] == "Leveling"


def test_talent_calc_command_supports_expansion_prefixed_relative_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/cata/talent-calc/hunter/beast-mastery/XYZ987")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc", "cata/talent-calc/hunter/beast-mastery/XYZ987"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["expansion"] == "cata"
    assert payload["tool"]["state_url"] == "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"
    assert payload["tool"]["class_slug"] == "hunter"
    assert payload["tool"]["spec_slug"] == "beast-mastery"
    assert payload["tool"]["build_code"] == "XYZ987"


def test_talent_calc_command_supports_expansion_prefixed_class_spec_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/classic/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc", "classic/druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["expansion"] == "classic"
    assert payload["tool"]["state_url"] == "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"
    assert payload["tool"]["class_slug"] == "druid"
    assert payload["tool"]["spec_slug"] == "balance"
    assert payload["tool"]["build_code"] == "ABC123"


def test_talent_calc_command_supports_scheme_less_wowhead_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url == "https://wowhead.com/talent-calc/druid/balance/ABC123"
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc", "wowhead.com/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tool"]["state_url"] == "https://wowhead.com/talent-calc/druid/balance/ABC123"


def test_talent_calc_command_rejects_empty_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "druid//balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must not include empty path segments."


def test_talent_calc_command_rejects_trailing_extra_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>]."


def test_talent_calc_command_rejects_malformed_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_talent_calc_command_rejects_nested_talent_calc_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "talent-calc/foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_talent_calc_command_rejects_buried_real_wowhead_path() -> None:
    result = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/items/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must point to /talent-calc."


def test_talent_calc_packet_command_emits_exact_transport_packet(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "5"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "wowhead"
    assert payload["kind"] == "talent_calc_packet"
    assert payload["tool"]["state_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert payload["talent_transport_packet"]["transport_status"] == "exact"
    assert (
        payload["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    )
    assert payload["talent_transport_packet"]["build_identity"]["class_spec_identity"]["identity"] == {
        "actor_class": "druid",
        "spec": "balance",
    }
    assert payload["talent_transport_packet"]["scope"] == {"type": "wowhead_talent_calc", "expansion": "retail"}
    assert payload["listed_builds"]["count"] == 2


def test_talent_calc_packet_command_supports_expansion_prefixed_relative_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/cata/talent-calc/hunter/beast-mastery/XYZ987")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "cata/talent-calc/hunter/beast-mastery/XYZ987"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tool"]["state_url"] == "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"
    assert payload["expansion"] == "cata"
    assert (
        payload["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"
    )
    assert payload["talent_transport_packet"]["scope"]["expansion"] == "cata"


def test_talent_calc_packet_command_supports_expansion_prefixed_class_spec_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/classic/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "classic/druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tool"]["state_url"] == "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"
    assert payload["expansion"] == "classic"
    assert (
        payload["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"
    )
    assert payload["talent_transport_packet"]["scope"]["expansion"] == "classic"


def test_talent_calc_packet_command_supports_scheme_less_wowhead_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url == "https://wowhead.com/talent-calc/druid/balance/ABC123"
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "wowhead.com/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tool"]["state_url"] == "https://wowhead.com/talent-calc/druid/balance/ABC123"
    assert (
        payload["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://wowhead.com/talent-calc/druid/balance/ABC123"
    )


def test_talent_calc_packet_command_can_write_exact_transport_packet(monkeypatch, tmp_path: Path) -> None:
    out_path = tmp_path / "balance-packet.json"

    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(out_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["written_packet_path"] == str(out_path.resolve())

    written_packet = json.loads(out_path.read_text())
    assert written_packet == payload["talent_transport_packet"]
    assert written_packet["transport_status"] == "exact"


def test_talent_calc_packet_command_does_not_require_page_fetch_for_exact_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        raise httpx.ConnectError("network down", request=httpx.Request("GET", page_url))

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tool"]["state_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert payload["tool"]["page_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert payload["page"]["canonical_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert "listed_builds" not in payload
    assert payload["talent_transport_packet"]["transport_status"] == "exact"


def test_talent_calc_packet_command_does_not_require_client_init_for_exact_ref(monkeypatch, tmp_path: Path) -> None:
    out_path = tmp_path / "exact-packet.json"
    monkeypatch.setattr("wowhead_cli.main._client", lambda ctx: (_ for _ in ()).throw(typer.Exit(1)))
    stdout_result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert stdout_result.exit_code == 0
    stdout_payload = json.loads(stdout_result.stdout)
    assert stdout_payload["tool"]["state_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert stdout_payload["talent_transport_packet"]["transport_status"] == "exact"
    assert "listed_builds" not in stdout_payload

    out_result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(out_path)])
    assert out_result.exit_code == 0
    out_payload = json.loads(out_result.stdout)
    assert out_payload["written_packet_path"] == str(out_path.resolve())
    assert json.loads(out_path.read_text()) == out_payload["talent_transport_packet"] == stdout_payload["talent_transport_packet"]


def test_talent_calc_packet_command_falls_back_on_http_status_error(monkeypatch, tmp_path: Path) -> None:
    out_path = tmp_path / "exact-packet.json"

    def fake_page_html(self, page_url: str):  # noqa: ANN001
        request = httpx.Request("GET", page_url)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("service unavailable", request=request, response=response)

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)

    stdout_result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert stdout_result.exit_code == 0
    stdout_payload = json.loads(stdout_result.stdout)
    assert stdout_payload["talent_transport_packet"]["transport_status"] == "exact"
    assert "listed_builds" not in stdout_payload

    out_result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(out_path)])
    assert out_result.exit_code == 0
    out_payload = json.loads(out_result.stdout)
    assert out_payload["written_packet_path"] == str(out_path.resolve())
    assert json.loads(out_path.read_text()) == out_payload["talent_transport_packet"] == stdout_payload["talent_transport_packet"]


def test_talent_calc_packet_command_normalizes_write_failure(monkeypatch, tmp_path: Path) -> None:
    out_dir = tmp_path / "out-dir"
    out_dir.mkdir()

    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(out_dir)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "transport_packet_write_failed"


def test_talent_calc_packet_command_rejects_ref_without_build_code(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/talent-calc/druid/balance")
        return SAMPLE_TALENT_CALC_HTML.replace("/talent-calc/druid/balance/ABC123", "/talent-calc/druid/balance")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc packet refs must include an explicit build code."


def test_talent_calc_packet_command_rejects_non_wowhead_absolute_url() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://notwowhead.com/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc URL must point to wowhead.com."


def test_talent_calc_packet_command_rejects_malformed_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_talent_calc_packet_command_rejects_nested_talent_calc_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "talent-calc/foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_talent_calc_packet_command_rejects_empty_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "druid//balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must not include empty path segments."


def test_talent_calc_packet_command_rejects_trailing_extra_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>]."


def test_talent_calc_packet_command_rejects_buried_real_wowhead_path() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://www.wowhead.com/items/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must point to /talent-calc."


def test_talent_calc_packet_command_rejects_invalid_transport_packet(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    monkeypatch.setattr(
        "wowhead_cli.main.build_reference_transport_packet_payload",
        lambda **kwargs: {
            "kind": "talent_transport_packet",
            "transport_status": "validated",
            "build_identity": {},
            "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
            "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
            "validation": {},
            "scope": {},
        },
    )

    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"


def test_profession_tree_command_decodes_url(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/profession-tree-calc/alchemy/BCuA")
        return SAMPLE_PROFESSION_TREE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["profession-tree", "alchemy/BCuA"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tool"]["profession_slug"] == "alchemy"
    assert payload["tool"]["loadout_code"] == "BCuA"
    assert payload["tool"]["state_url"].endswith("/profession-tree-calc/alchemy/BCuA")


def test_dressing_room_command_normalizes_hash_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url == "https://www.wowhead.com/dressing-room"
        return SAMPLE_DRESSING_ROOM_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["dressing-room", "#fz8zz0zb89c8mM8YB"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tool"]["share_hash"] == "fz8zz0zb89c8mM8YB"
    assert payload["tool"]["has_share_hash"] is True
    assert payload["tool"]["state_url"].startswith("https://www.wowhead.com/dressing-room#")


def test_profiler_command_normalizes_list_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url == "https://www.wowhead.com/list"
        return SAMPLE_PROFILER_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["profiler", "97060220/us/illidan/Roguecane"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tool"]["list_id"] == "97060220"
    assert payload["tool"]["region_slug"] == "us"
    assert payload["tool"]["realm_slug"] == "illidan"
    assert payload["tool"]["character_name"] == "Roguecane"


def test_news_post_command_extracts_markup_and_author(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url == "https://www.wowhead.com/news/midnight-hotfixes-380785"
        return SAMPLE_NEWS_POST_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["news-post", "/news/midnight-hotfixes-380785"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["post"]["page_url"] == "https://www.wowhead.com/news/midnight-hotfixes-380785"
    assert payload["content"]["section_count"] == 1
    assert payload["author"]["username"] == "staff"
    assert payload["related"]["news"]["count"] == 1
    assert payload["related"]["blueTracker"]["items"][0]["is_blue_tracker"] is True
    assert "Death Knight fixes" in payload["content"]["text"]


def test_blue_topic_command_extracts_posts(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url == "https://www.wowhead.com/blue-tracker/topic/eu/class-tuning-incoming-18-march-610948"
        return SAMPLE_BLUE_TOPIC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["blue-topic", "/blue-tracker/topic/eu/class-tuning-incoming-18-march-610948"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["posts"]["count"] == 1
    first = payload["posts"]["items"][0]
    assert first["author"] == "Kaivax"
    assert first["author_page"] == "https://www.wowhead.com/blue-tracker/author/Kaivax"
    assert first["blue"] is True
    assert first["body_text"].startswith("The first few days of Midnight")
    assert payload["summary"]["participants"] == ["Kaivax"]
    assert payload["summary"]["blue_authors"] == ["Kaivax"]


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
    assert payload["results"][0]["entity_type"] == "guide"
    assert payload["results"][0]["url"] == "https://www.wowhead.com/wotlk/guide=3143"


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
    assert payload["results"][0]["entity_type"] == "faction"
    assert payload["results"][0]["url"] == "https://www.wowhead.com/faction=529"


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
    assert [row["id"] for row in payload["results"]] == [19019, 2]
    assert "exact_name" in payload["results"][0]["ranking"]["match_reasons"]
    assert payload["results"][0]["ranking"]["score"] > payload["results"][1]["ranking"]["score"]


def test_search_type_hint_promotes_guides_for_guide_queries(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "type": 3,
                    "id": 19019,
                    "name": "Frost Death Knight",
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
    assert [row["entity_type"] for row in payload["results"]] == ["guide", "item"]
    assert "type_hint" in payload["results"][0]["ranking"]["match_reasons"]


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
    assert payload["results"][0]["entity_type"] == "pet"
    assert payload["results"][0]["url"] == "https://www.wowhead.com/pet=39"


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
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    assert payload["search_query"] == "fairbreeze favors"
    assert payload["match"]["entity_type"] == "quest"
    assert payload["next_command"] == "wowhead entity quest 86739"
    assert payload["fallback_search_command"] is None


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
    assert payload["resolved"] is False
    assert payload["confidence"] == "low"
    assert payload["next_command"] is None
    assert payload["fallback_search_command"] == "wowhead search frost"
    assert payload["count"] == 2
    assert len(payload["candidates"]) == 2


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
    assert payload["filters"]["entity_types"] == ["guide"]
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    assert payload["match"]["entity_type"] == "guide"
    assert payload["next_command"] == "wowhead --expansion wotlk guide 3143"


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
    assert payload["search_query"] == "thunderfury"
    assert payload["results"][0]["follow_up"] == {
        "recommended_surface": "entity",
        "recommended_command": "wowhead entity item 19019",
        "reason": "entity_summary",
        "alternatives": [
            "wowhead entity-page item 19019",
            "wowhead comments item 19019",
        ],
    }


def test_exact_match_score_prefers_exact_name_over_display_name() -> None:
    score, reasons = _exact_match_score(
        "createframe",
        name_normalized="createframe",
        display_normalized="api createframe",
    )
    assert score == 30
    assert reasons == ["exact_name"]


def test_prefix_and_contains_score_prefers_name_prefix_before_contains() -> None:
    score, reasons = _prefix_and_contains_score(
        "create",
        name_normalized="createframe",
        display_normalized="api createframe",
    )
    assert score == 10
    assert reasons == ["name_prefix"]


def test_term_match_score_requires_all_terms() -> None:
    score, reasons = _term_match_score({"world", "api"}, haystacks=["world of warcraft api", "reference"])
    assert score == 6
    assert reasons == ["all_terms_match"]

    score, reasons = _term_match_score({"world", "api", "dragonflight"}, haystacks=["world of warcraft api", "reference"])
    assert score == 0
    assert reasons == []


def test_type_hint_score_boosts_matching_entity_type() -> None:
    score, reasons = _type_hint_score("quest thunderfury", entity_type="quest")
    assert score == 9
    assert reasons == ["type_hint"]

    score, reasons = _type_hint_score("quest thunderfury", entity_type="item")
    assert score == 0
    assert reasons == []


def test_popularity_score_adds_reason_and_entity_bonus() -> None:
    score, reasons = _popularity_score(999, entity_type="item")
    assert score >= 1
    assert reasons == ["popularity"]

    score, reasons = _popularity_score(0, entity_type="item")
    assert score == 1
    assert reasons == []


def test_search_result_score_and_reasons_composes_helper_scores() -> None:
    score, reasons = _search_result_score_and_reasons(
        {
            "type": 5,
            "id": 86739,
            "name": "Fairbreeze Favors",
            "displayName": "Fairbreeze Favors",
            "typeName": "Quest",
            "popularity": 999,
        },
        query="quest fairbreeze favors",
        ranking_query="quest fairbreeze favors",
    )
    assert score > 0
    assert "all_terms_match" in reasons
    assert "type_hint" in reasons
    assert "popularity" in reasons


def test_resolve_confidence_policy_helpers_cover_exact_filtered_and_medium_cases() -> None:
    assert _is_high_confidence_exact_match({"exact_name"}, margin=4, second_score=20) is True
    assert _is_high_confidence_exact_match({"exact_display_name"}, margin=0, second_score=0) is True
    assert _is_high_confidence_exact_match({"all_terms_match"}, margin=10, second_score=0) is False

    assert _is_high_confidence_score(24, margin=6) is True
    assert _is_high_confidence_score(23, margin=6) is False

    assert _is_filtered_high_confidence(("guide",), top_score=18, margin=4) is True
    assert _is_filtered_high_confidence((), top_score=18, margin=4) is False

    assert _is_medium_confidence_score(18, margin=4) is True
    assert _is_medium_confidence_score(17, margin=4) is False


def test_guide_section_matches_applies_section_title_filter() -> None:
    matches = _guide_section_matches(
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
    matches = _guide_linked_entity_matches(
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
    navigation_matches = _guide_navigation_matches(
        navigation_links=[{"label": "BiS Gear", "url": "https://www.wowhead.com/guide/bis", "source_url": None}],
        query="bis",
        page_url="https://www.wowhead.com/guide=3143",
    )
    assert navigation_matches[0]["kind"] == "navigation"
    assert navigation_matches[0]["citation_url"] == "https://www.wowhead.com/guide=3143"

    gatherer_matches = _guide_gatherer_matches(
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

    comment_matches = _guide_comment_matches(
        comments=[{"id": 91, "user": "A", "body": "Solid guide", "citation_url": "https://www.wowhead.com/guide=3143#comments"}],
        query="solid",
    )
    assert comment_matches[0]["kind"] == "comment"
    assert comment_matches[0]["user"] == "A"


def test_guide_query_top_matches_dedupes_entity_results_across_groups() -> None:
    top = _guide_query_top_matches(
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
    category, authors, updated_after, updated_before = _validated_guides_filters(
        category=" classes/ ",
        author=["Khazakdk,Another"],
        updated_after="2026-02-01",
        updated_before="2026-03-01",
        patch_min=1,
        patch_max=2,
        sort_by="updated",
    )

    assert category == "classes"
    assert authors == ("khazakdk", "another")
    assert updated_after is not None
    assert updated_before is not None

    try:
        _validated_guides_filters(
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

    assert _guide_row_matches_filters(
        normalized_row,
        selected_authors=("khazakdk",),
        parsed_updated_after=datetime(2026, 2, 1, tzinfo=UTC),
        parsed_updated_before=datetime(2026, 3, 1, tzinfo=UTC),
        patch_min=120000,
        patch_max=120001,
    ) is True

    filtered = _filtered_guide_category_rows(
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
        selected_authors=("khazakdk",),
        parsed_updated_after=datetime(2026, 2, 1, tzinfo=UTC),
        parsed_updated_before=datetime(2026, 3, 1, tzinfo=UTC),
        patch_min=120000,
        patch_max=120001,
        sort_by="relevance",
    )
    assert len(filtered) == 1
    assert filtered[0]["match_score"] > 0


def test_guides_payload_builds_expected_filters_and_facets() -> None:
    class DummyConfig:
        expansion = resolve_expansion(None)

    payload = _guides_payload(
        cfg=DummyConfig(),
        category="classes",
        query="death knight",
        sort_by="relevance",
        selected_authors=("khazakdk",),
        parsed_updated_after=datetime(2026, 2, 1, tzinfo=UTC),
        parsed_updated_before=None,
        patch_min=120001,
        patch_max=None,
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


def test_entity_page_needs_fetch_and_comments_payload_helpers() -> None:
    assert _entity_page_needs_fetch(
        include_comments=False,
        linked_entity_preview_limit=0,
        tooltip_from_page_metadata=False,
    ) is False
    assert _entity_page_needs_fetch(
        include_comments=True,
        linked_entity_preview_limit=0,
        tooltip_from_page_metadata=False,
    ) is True

    comments_payload, citations = _entity_comments_payload(
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
    payload = _entity_linked_entities_payload(
        html=SAMPLE_PAGE_HTML,
        page_url="https://www.wowhead.com/item=19019/thunderfury",
        page_entity_type="item",
        page_entity_id=19019,
        requested_entity_type="item",
        requested_entity_id=19019,
        linked_entity_preview_limit=5,
    )
    assert payload is not None
    assert payload["count"] >= 1
    assert payload["fetch_more_command"] == "wowhead entity-page item 19019 --max-links 200"


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

    files_written, bundle_parts = _write_guide_export_assets(
        export_dir=tmp_path,
        payload=payload,
        html="<html></html>",
    )
    assert files_written["guide_json"] == "guide.json"
    assert files_written["structured_data_json"] == "structured-data.json"
    assert len(bundle_parts["sections"]) == 1
    assert len(bundle_parts["linked_items"]) == 1

    manifest = _guide_export_manifest(
        export_dir=tmp_path,
        payload=payload,
        guide_ref="3143",
        max_links=10,
        include_replies=False,
        hydrate_linked_entities=True,
        hydrate_types=("spell",),
        hydrate_limit=5,
        hydrated_summary_items=[{"entity_type": "spell", "id": 49020, "storage_source": "entity_cache"}],
        hydrated_at="2026-03-14T00:00:00+00:00",
        files_written=files_written,
        sections=bundle_parts["sections"],
        nav_links=bundle_parts["navigation_links"],
        linked_items=bundle_parts["linked_items"],
        gatherer_items=bundle_parts["gatherer_items"],
        comment_items=bundle_parts["comment_items"],
        analysis_surfaces=bundle_parts["analysis_surfaces"],
    )
    assert manifest["counts"]["sections"] == 1
    assert manifest["counts"]["analysis_surfaces"] == 1
    assert manifest["counts"]["hydrated_entities"] == 1
    assert manifest["hydration"]["source_counts"]["entity_cache"] == 1


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
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    assert payload["match"]["entity_type"] == "quest"
    assert payload["match"]["follow_up"]["recommended_surface"] == "comments"
    assert payload["next_command"] == "wowhead comments quest 86739"


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
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    assert payload["match"]["entity_type"] == "item"
    assert payload["match"]["follow_up"]["recommended_surface"] == "entity-page"
    assert payload["next_command"] == "wowhead entity-page item 19019"


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
    assert payload["resolved"] is True
    assert payload["match"]["entity_type"] == "guide"
    assert payload["match"]["follow_up"]["recommended_surface"] == "guide-full"
    assert payload["next_command"] == "wowhead guide-full 3143"


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
    assert payload["guide"]["id"] == 3143
    assert payload["guide"]["lookup_url"] == "https://www.wowhead.com/wotlk/guide=3143"
    assert payload["guide"]["page_url"] == "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"
    assert payload["comments"]["count"] == 1
    assert payload["comments"]["top"][0]["citation_url"].endswith("#comments:id=91")
    assert payload["linked_entities"]["count"] >= 2
    assert payload["linked_entities"]["source_counts"] == {"href": 2, "gatherer": 1, "merged": 2}
    assert payload["linked_entities"]["items"][0]["url"]


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
    assert payload["guide"]["id"] is None
    assert payload["guide"]["lookup_url"] == guide_url
    assert payload["comments"]["top"] == []


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
    assert payload["guide"]["id"] == 3143
    assert payload["guide"]["page_url"] == "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"
    assert payload["author"]["name"] == "khazakdk"
    assert payload["rating"]["votes"] == 70
    assert payload["body"]["sections"][0]["title"] == "Frost Death Knight Overview"
    assert payload["body"]["section_chunks"][0]["content_text"] == "Welcome to the guide."
    assert payload["navigation"]["links"][0]["url"] == "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"
    assert payload["linked_entities"]["count"] >= 2
    assert payload["linked_entities"]["source_counts"] == {"href": 2, "gatherer": 1, "merged": 2}
    assert payload["gatherer_entities"]["items"][0]["id"] == 249277
    assert payload["gatherer_entities"]["items"][0]["citation_url"] == "https://www.wowhead.com/item=249277"
    merged_item = next(row for row in payload["linked_entities"]["items"] if row["id"] == 249277)
    assert merged_item["sources"] == ["gatherer", "href"]
    assert merged_item["source_kind"] == "gatherer"
    assert payload["comments"]["all_comments_included"] is True
    assert payload["comments"]["items"][0]["citation_url"].endswith("#comments:id=91")
    assert payload["structured_data"]["headline"] == "Frost Death Knight DPS Guide - Midnight"
    assert payload["analysis_surfaces"]["count"] >= 1
    assert payload["analysis_surfaces"]["items"][0]["surface_tags"] == ["overview"]


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
    assert guide_payload["linked_entities"]["count"] == full_payload["linked_entities"]["count"] == 2
    assert guide_payload["analysis_surfaces"]["count"] == full_payload["analysis_surfaces"]["count"]


def test_guide_export_writes_local_assets(monkeypatch, tmp_path) -> None:
    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    export_dir = tmp_path / "guide-export"
    result = runner.invoke(app, ["guide-export", "3143", "--out", str(export_dir)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["output_dir"] == str(export_dir)
    assert payload["counts"] == {
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
    assert payload["counts"]["hydrated_entities"] == 2
    assert payload["hydration"]["enabled"] is True
    assert payload["hydration"]["types"] == ["spell", "item"]
    assert payload["hydration"]["limit"] == 2
    assert isinstance(payload["hydration"]["hydrated_at"], str)

    entities_manifest = json.loads((export_dir / "entities" / "manifest.json").read_text(encoding="utf-8"))
    assert entities_manifest["count"] == 2
    assert entities_manifest["counts_by_type"] == {"item": 1, "spell": 1}
    assert entities_manifest["counts_by_storage_source"] == {"live_fetch": 2}
    assert {row["path"] for row in entities_manifest["items"]} == {
        "entities/item/249277.json",
        "entities/spell/49020.json",
    }
    assert {row["storage_source"] for row in entities_manifest["items"]} == {"live_fetch"}
    assert payload["hydration"]["source_counts"] == {"live_fetch": 2}

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
    assert payload["hydration"]["source_counts"] == {"entity_cache": 2}
    entities_manifest = json.loads((export_dir / "entities" / "manifest.json").read_text(encoding="utf-8"))
    assert entities_manifest["counts_by_storage_source"] == {"entity_cache": 2}
    assert {row["storage_source"] for row in entities_manifest["items"]} == {"entity_cache"}

    spell_payload = json.loads((export_dir / "entities" / "spell" / "49020.json").read_text(encoding="utf-8"))
    item_payload = json.loads((export_dir / "entities" / "item" / "249277.json").read_text(encoding="utf-8"))
    assert spell_payload["entity"]["name"] == "Obliterate"
    assert item_payload["entity"]["name"] == "Bellamy's Final Judgement"


def test_guide_bundle_refresh_skips_fresh_bundle_with_default_max_age(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = root / "guide-3143-frost"
    bundle_dir.mkdir(parents=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    (bundle_dir / "manifest.json").write_text(
        json.dumps(
            {
                "export_version": 2,
                "exported_at": now,
                "guide_fetched_at": now,
                "expansion": "retail",
                "output_dir": str(bundle_dir),
                "guide": {
                    "input": "3143",
                    "id": 3143,
                    "page_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                },
                "page": {
                    "title": "Frost Death Knight DPS Guide - Midnight",
                    "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                },
                "counts": {
                    "sections": 11,
                    "navigation_links": 15,
                    "linked_entities": 52,
                    "gatherer_entities": 52,
                    "hydrated_entities": 0,
                    "comments": 9,
                },
                "hydration": {
                    "enabled": False,
                    "types": [],
                    "limit": 0,
                    "hydrated_at": None,
                },
                "export_options": {
                    "guide_ref": "3143",
                    "max_links": 250,
                    "include_replies": False,
                },
                "files": {"manifest_json": "manifest.json"},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["guide-bundle-refresh", "3143", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["refresh"] == {
        "updated": False,
        "reason": "fresh",
        "max_age_hours": 24,
    }


def test_guide_bundle_refresh_updates_stale_bundle_and_reuses_manifest_settings(
    monkeypatch,
    tmp_path: Path,
) -> None:
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
    export_result = runner.invoke(
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
    assert export_result.exit_code == 0

    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stale = (datetime.now(UTC) - timedelta(hours=48)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest["exported_at"] = stale
    manifest["guide_fetched_at"] = stale
    manifest["hydration"]["hydrated_at"] = stale
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = runner.invoke(app, ["guide-bundle-refresh", str(export_dir)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["refresh"] == {
        "updated": True,
        "reason": "stale",
        "max_age_hours": 24,
    }
    assert payload["hydration"]["enabled"] is True
    assert payload["hydration"]["types"] == ["spell", "item"]
    assert payload["counts"]["hydrated_entities"] == 2
    assert (export_dir / "entities" / "manifest.json").exists()


def test_guide_bundle_refresh_rehydrates_only_stale_hydrated_entities(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tooltip_calls: dict[tuple[str, int], int] = {}

    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        key = (entity_type, entity_id)
        tooltip_calls[key] = tooltip_calls.get(key, 0) + 1
        if key == ("spell", 49020):
            return {
                "name": "Obliterate",
                "tooltip": "<table><tr><td><b>Obliterate</b><br>Talent<br>Instant<br>A brutal attack.</td></tr></table>",
            }
        if key == ("item", 249277):
            return {
                "name": "Bellamy's Final Judgement",
                "tooltip": "<table><tr><td><b>Bellamy's Final Judgement</b><br>Item Level 639</td></tr></table>",
            }
        raise AssertionError(f"Unexpected tooltip lookup: {key}")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)

    export_dir = tmp_path / "guide-export"
    export_result = runner.invoke(
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
    assert export_result.exit_code == 0
    assert tooltip_calls == {
        ("spell", 49020): 1,
        ("item", 249277): 1,
    }

    tooltip_calls.clear()

    stale = (datetime.now(UTC) - timedelta(hours=48)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fresh = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["exported_at"] = stale
    manifest["guide_fetched_at"] = stale
    manifest["hydration"]["hydrated_at"] = stale
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    entities_manifest_path = export_dir / "entities" / "manifest.json"
    entities_manifest = json.loads(entities_manifest_path.read_text(encoding="utf-8"))
    items = entities_manifest["items"]
    for row in items:
        if row["entity_type"] == "spell":
            row["stored_at"] = fresh
        elif row["entity_type"] == "item":
            row["stored_at"] = stale
    entities_manifest["hydrated_at"] = stale
    entities_manifest_path.write_text(json.dumps(entities_manifest), encoding="utf-8")

    result = runner.invoke(app, ["guide-bundle-refresh", str(export_dir)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["refresh"] == {
        "updated": True,
        "reason": "stale",
        "max_age_hours": 24,
    }
    assert tooltip_calls == {
        ("item", 249277): 1,
    }

    refreshed_entities_manifest = json.loads(entities_manifest_path.read_text(encoding="utf-8"))
    refreshed_items = {
        (row["entity_type"], row["id"]): row
        for row in refreshed_entities_manifest["items"]
    }
    assert refreshed_items[("spell", 49020)]["stored_at"] == fresh
    assert refreshed_items[("item", 249277)]["stored_at"] != stale
    assert refreshed_items[("spell", 49020)]["storage_source"] == "bundle_store"
    assert refreshed_items[("item", 249277)]["storage_source"] == "live_fetch"
    assert refreshed_entities_manifest["counts_by_storage_source"] == {
        "bundle_store": 1,
        "live_fetch": 1,
    }
    assert payload["hydration"]["source_counts"] == {
        "bundle_store": 1,
        "live_fetch": 1,
    }


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
    assert payload["hydration"]["source_counts"] == {
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
    assert payload["counts"]["gatherer_entities"] >= 1
    assert payload["counts"]["analysis_surfaces"] == 0
    assert payload["matches"]["gatherer_entities"][0]["name"] == "Bellamy's Final Judgement"
    assert payload["top"][0]["kind"] == "linked_entity"
    assert payload["top"][0]["name"] == "Bellamy's Final Judgement"
    assert payload["top"][0]["sources"] == ["gatherer", "href"]

    result = runner.invoke(app, ["guide-query", str(export_dir), "obliterate", "--limit", "3"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["counts"]["linked_entities"] >= 1
    assert payload["matches"]["linked_entities"][0]["entity_type"] == "spell"
    assert payload["matches"]["linked_entities"][0]["name"] == "Obliterate"
    assert payload["top"][0]["kind"] == "linked_entity"
    assert payload["top"][0]["sources"] == ["href"]

    duplicate_entity_rows = [
        row for row in payload["top"] if row.get("entity_type") == "spell" and row.get("id") == 49020
    ]
    assert len(duplicate_entity_rows) == 1

    result = runner.invoke(app, ["guide-query", str(export_dir), "welcome guide", "--limit", "2"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["matches"]["sections"][0]["title"] == "Frost Death Knight Overview"
    assert "Welcome to the guide." in payload["matches"]["sections"][0]["preview"]

    result = runner.invoke(app, ["guide-query", str(export_dir), "overview", "--kind", "analysis_surfaces"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["counts"]["analysis_surfaces"] >= 1
    assert payload["matches"]["analysis_surfaces"][0]["surface_tags"] == ["overview"]

    result = runner.invoke(
        app,
        ["guide-query", str(export_dir), "welcome", "--kind", "sections", "--section-title", "overview"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["filters"] == {
        "kinds": ["sections"],
        "section_title": "overview",
        "linked_sources": [],
    }
    assert payload["counts"]["sections"] == 1
    assert payload["counts"]["comments"] == 0
    assert payload["matches"]["sections"][0]["title"] == "Frost Death Knight Overview"

    result = runner.invoke(app, ["guide-query", str(export_dir), "solid", "--kind", "comments"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["counts"]["comments"] == 1
    assert payload["counts"]["sections"] == 0
    assert payload["matches"]["comments"][0]["user"] == "A"

    root = tmp_path / "wowhead_exports"
    selector_dir = root / export_dir.name
    root.mkdir(exist_ok=True)
    export_dir.rename(selector_dir)

    result = runner.invoke(app, ["guide-query", "3143", "obliterate", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["output_dir"] == str(selector_dir)
    assert payload["matches"]["linked_entities"][0]["name"] == "Obliterate"

    result = runner.invoke(
        app,
        ["guide-query", "3143", "bellamy", "--root", str(root), "--kind", "linked_entities", "--linked-source", "multi"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["filters"]["linked_sources"] == ["multi"]
    assert payload["counts"]["linked_entities"] == 1
    assert payload["matches"]["linked_entities"][0]["name"] == "Bellamy's Final Judgement"
    assert payload["matches"]["linked_entities"][0]["sources"] == ["gatherer", "href"]

    result = runner.invoke(
        app,
        ["guide-query", "3143", "obliterate", "--root", str(root), "--kind", "linked_entities", "--linked-source", "href"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["filters"]["linked_sources"] == ["href"]
    assert payload["matches"]["linked_entities"][0]["name"] == "Obliterate"

    result = runner.invoke(app, ["guide-query", selector_dir.name, "solid", "--root", str(root), "--kind", "comments"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["output_dir"] == str(selector_dir)
    assert payload["matches"]["comments"][0]["user"] == "A"

    missing_dir = tmp_path / "missing-corpus"
    result = runner.invoke(app, ["guide-query", str(missing_dir), "anything"])
    assert result.exit_code != 0
    assert "does not exist" in result.output

    result = runner.invoke(app, ["guide-query", str(selector_dir), "anything", "--linked-source", "bad-source"])
    assert result.exit_code != 0
    assert "Unsupported linked source filter" in result.output


def test_guide_bundle_list_discovers_exported_bundles(tmp_path) -> None:
    root = tmp_path / "wowhead_exports"
    corpus_a = root / "guide-3143-frost"
    corpus_b = root / "guide-42-other"
    junk = root / "not-a-corpus"
    corpus_a.mkdir(parents=True)
    corpus_b.mkdir(parents=True)
    junk.mkdir(parents=True)
    fresh = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    stale = (datetime.now(UTC) - timedelta(hours=48)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    (corpus_a / "manifest.json").write_text(
        json.dumps(
            {
                "export_version": 1,
                "expansion": "retail",
                "output_dir": str(corpus_a),
                "exported_at": stale,
                "guide_fetched_at": stale,
                "guide": {"id": 3143, "page_url": "https://www.wowhead.com/guide=3143"},
                "page": {
                    "title": "Frost Death Knight DPS Guide - Midnight",
                    "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                },
                "counts": {
                    "sections": 11,
                    "navigation_links": 15,
                    "linked_entities": 27,
                    "gatherer_entities": 52,
                    "hydrated_entities": 2,
                    "comments": 9,
                },
                "hydration": {
                    "enabled": True,
                    "types": ["spell", "item"],
                    "limit": 2,
                    "hydrated_at": stale,
                    "source_counts": {"entity_cache": 1, "live_fetch": 1},
                },
                "files": {"manifest_json": "manifest.json"},
            }
        ),
        encoding="utf-8",
    )
    (corpus_b / "manifest.json").write_text(
        json.dumps(
            {
                "export_version": 1,
                "expansion": "classic",
                "output_dir": str(corpus_b),
                "exported_at": fresh,
                "guide_fetched_at": fresh,
                "guide": {"id": 42, "page_url": "https://www.wowhead.com/guide=42"},
                "page": {
                    "title": "Arcane Mage Guide",
                    "canonical_url": "https://www.wowhead.com/guide/classes/mage/arcane/overview-pve-dps",
                },
                "counts": {
                    "sections": 4,
                    "navigation_links": 6,
                    "linked_entities": 5,
                    "gatherer_entities": 3,
                    "hydrated_entities": 0,
                    "comments": 2,
                },
                "hydration": {
                    "enabled": False,
                    "types": [],
                    "limit": 0,
                    "hydrated_at": None,
                    "source_counts": {},
                },
                "files": {"manifest_json": "manifest.json"},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["guide-bundle-list", "--root", str(root)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["root"] == str(root)
    assert payload["count"] == 2
    assert payload["max_age_hours"] == 24
    assert [row["guide_id"] for row in payload["bundles"]] == [42, 3143]
    assert payload["bundles"][0]["dir_name"] == "guide-42-other"
    assert payload["bundles"][0]["title"] == "Arcane Mage Guide"
    assert payload["bundles"][0]["freshness"]["max_age_hours"] == 24
    assert payload["bundles"][0]["freshness"]["bundle"] == "fresh"
    assert payload["bundles"][0]["freshness"]["bundle_reasons"] == []
    assert payload["bundles"][0]["freshness"]["hydration"] == "disabled"
    assert payload["bundles"][0]["freshness"]["hydration_reasons"] == ["disabled"]
    assert payload["bundles"][0]["hydration"] == {
        "enabled": False,
        "types": [],
        "limit": 0,
        "hydrated_at": None,
        "hydrated_entities": 0,
        "source_counts": {},
    }
    assert payload["bundles"][1]["counts"]["linked_entities"] == 27
    assert payload["bundles"][1]["freshness"]["max_age_hours"] == 24
    assert payload["bundles"][1]["freshness"]["bundle"] == "stale"
    assert payload["bundles"][1]["freshness"]["bundle_reasons"] == ["max_age_exceeded"]
    assert payload["bundles"][1]["freshness"]["hydration"] == "stale"
    assert "bundle_stale" in payload["bundles"][1]["freshness"]["hydration_reasons"]
    assert "max_age_exceeded" in payload["bundles"][1]["freshness"]["hydration_reasons"]
    assert payload["bundles"][1]["hydration"] == {
        "enabled": True,
        "types": ["spell", "item"],
        "limit": 2,
        "hydrated_at": stale,
        "hydrated_entities": 2,
        "source_counts": {"entity_cache": 1, "live_fetch": 1},
    }

    result = runner.invoke(app, ["guide-bundle-list", "--root", str(root), "--max-age-hours", "72"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["max_age_hours"] == 72
    assert payload["stale_reason_counts"] == {"bundle": {}, "hydration": {}}
    assert payload["bundles"][1]["freshness"]["max_age_hours"] == 72
    assert payload["bundles"][1]["freshness"]["bundle"] == "fresh"
    assert payload["bundles"][1]["freshness"]["bundle_reasons"] == []
    assert payload["bundles"][1]["freshness"]["hydration"] == "fresh"
    assert payload["bundles"][1]["freshness"]["hydration_reasons"] == []


def test_guide_bundle_list_uses_root_index_when_available(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = root / "guide-3143-frost"
    bundle_dir.mkdir(parents=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    (bundle_dir / "manifest.json").write_text(
        json.dumps({"guide": {"id": 3143}}),
        encoding="utf-8",
    )
    (root / "index.json").write_text(
        json.dumps(
            {
                "index_version": 1,
                "updated_at": now,
                "root": str(root),
                "count": 1,
                "bundles": [
                    {
                        "path": str(bundle_dir),
                        "dir_name": bundle_dir.name,
                        "guide_id": 3143,
                        "title": "Frost Death Knight DPS Guide - Midnight",
                        "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                        "expansion": "retail",
                        "export_version": 2,
                        "counts": {
                            "sections": 11,
                            "navigation_links": 15,
                            "linked_entities": 52,
                            "gatherer_entities": 52,
                            "hydrated_entities": 1,
                            "comments": 9,
                        },
                        "exported_at": now,
                        "guide_fetched_at": now,
                        "hydration": {
                            "enabled": True,
                            "types": ["spell"],
                            "limit": 1,
                            "hydrated_at": now,
                            "hydrated_entities": 1,
                            "source_counts": {"entity_cache": 1},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fail_scan(root_path: Path) -> list[dict[str, object]]:  # noqa: ANN202
        raise AssertionError(f"scan should not be used when a valid index exists: {root_path}")

    monkeypatch.setattr("wowhead_cli.main._scan_guide_bundle_rows", fail_scan)

    result = runner.invoke(app, ["guide-bundle-list", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["bundles"][0]["guide_id"] == 3143
    assert payload["stale_reason_counts"] == {"bundle": {}, "hydration": {}}
    assert payload["bundles"][0]["hydration"]["source_counts"] == {"entity_cache": 1}
    assert payload["bundles"][0]["freshness"]["max_age_hours"] == 24
    assert payload["bundles"][0]["freshness"]["bundle"] == "fresh"
    assert payload["bundles"][0]["freshness"]["bundle_reasons"] == []
    assert payload["bundles"][0]["freshness"]["hydration"] == "fresh"
    assert payload["bundles"][0]["freshness"]["hydration_reasons"] == []


def test_guide_bundle_search_returns_ranked_matches_and_follow_up_commands(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    frost = root / "guide-3143-frost"
    arcane = root / "guide-42-arcane"
    frost.mkdir(parents=True)
    arcane.mkdir(parents=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    for bundle_dir, guide_id, title, expansion in [
        (frost, 3143, "Frost Death Knight DPS Guide - Midnight", "retail"),
        (arcane, 42, "Arcane Mage Guide", "classic"),
    ]:
        (bundle_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "export_version": 2,
                    "output_dir": str(bundle_dir),
                    "exported_at": now,
                    "guide_fetched_at": now,
                    "expansion": expansion,
                    "guide": {"id": guide_id, "page_url": f"https://www.wowhead.com/guide={guide_id}"},
                    "page": {
                        "title": title,
                        "canonical_url": f"https://www.wowhead.com/guide/{guide_id}",
                    },
                    "counts": {
                        "sections": 1,
                        "navigation_links": 1,
                        "linked_entities": 1,
                        "gatherer_entities": 1,
                        "hydrated_entities": 0,
                        "comments": 1,
                    },
                    "hydration": {
                        "enabled": False,
                        "types": [],
                        "limit": 0,
                        "hydrated_at": None,
                        "source_counts": {},
                    },
                }
            ),
            encoding="utf-8",
        )

    result = runner.invoke(app, ["guide-bundle-search", "frost death knight", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"] == "frost death knight"
    assert payload["count"] == 1
    assert payload["stale_reason_counts"] == {"bundle": {}, "hydration": {}}
    assert payload["matches"][0]["guide_id"] == 3143
    assert "title" in payload["matches"][0]["match_reasons"]
    assert payload["matches"][0]["suggested_query_command"] == (
        f"wowhead guide-query 3143 'frost death knight' --root {root}"
    )

    result = runner.invoke(app, ["guide-bundle-search", "42", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["matches"][0]["guide_id"] == 42
    assert "guide_id" in payload["matches"][0]["match_reasons"]

    result = runner.invoke(app, ["guide-bundle-search", "classic", "--root", str(root), "--limit", "1"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["matches"][0]["guide_id"] == 42
    assert "expansion" in payload["matches"][0]["match_reasons"]


def test_guide_bundle_search_uses_root_index_when_available(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = root / "guide-3143-frost"
    bundle_dir.mkdir(parents=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    (bundle_dir / "manifest.json").write_text(json.dumps({"guide": {"id": 3143}}), encoding="utf-8")
    (root / "index.json").write_text(
        json.dumps(
            {
                "index_version": 1,
                "updated_at": now,
                "root": str(root),
                "count": 1,
                "bundles": [
                    {
                        "path": str(bundle_dir),
                        "dir_name": bundle_dir.name,
                        "guide_id": 3143,
                        "title": "Frost Death Knight DPS Guide - Midnight",
                        "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                        "expansion": "retail",
                        "export_version": 2,
                        "counts": {
                            "sections": 11,
                            "navigation_links": 15,
                            "linked_entities": 52,
                            "gatherer_entities": 52,
                            "hydrated_entities": 1,
                            "comments": 9,
                        },
                        "exported_at": now,
                        "guide_fetched_at": now,
                        "hydration": {
                            "enabled": True,
                            "types": ["spell"],
                            "limit": 1,
                            "hydrated_at": now,
                            "hydrated_entities": 1,
                            "source_counts": {"entity_cache": 1},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fail_scan(root_path: Path) -> list[dict[str, object]]:  # noqa: ANN202
        raise AssertionError(f"scan should not be used when a valid index exists: {root_path}")

    monkeypatch.setattr("wowhead_cli.main._scan_guide_bundle_rows", fail_scan)

    result = runner.invoke(app, ["guide-bundle-search", "frost", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["matches"][0]["guide_id"] == 3143
    assert "title" in payload["matches"][0]["match_reasons"]


def test_guide_bundle_query_returns_cross_bundle_matches(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    _write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
        sections=[
            {
                "ordinal": 1,
                "level": 2,
                "title": "Rotation",
                "content_text": "Use Obliterate and Frost Strike in your rotation.",
            }
        ],
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 49020,
                "name": "Obliterate",
                "url": "https://www.wowhead.com/spell=49020/obliterate",
                "citation_url": "https://www.wowhead.com/spell=49020/obliterate",
                "sources": ["href", "gatherer"],
                "source_kind": "href",
            }
        ],
    )
    _write_bundle_fixture(
        root,
        dir_name="guide-42-arcane",
        guide_id=42,
        title="Arcane Mage Guide",
        expansion="classic",
        sections=[
            {
                "ordinal": 1,
                "level": 2,
                "title": "Rotation",
                "content_text": "Use Arcane Blast and Arcane Missiles.",
            }
        ],
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 30451,
                "name": "Arcane Blast",
                "url": "https://www.wowhead.com/spell=30451/arcane-blast",
                "citation_url": "https://www.wowhead.com/spell=30451/arcane-blast",
                "sources": ["href"],
                "source_kind": "href",
            }
        ],
    )

    result = runner.invoke(app, ["guide-bundle-query", "obliterate", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["searched_bundle_count"] == 2
    assert payload["count"] == 1
    assert payload["counts"] == {
        "sections": 1,
        "analysis_surfaces": 0,
        "navigation": 0,
        "linked_entities": 1,
        "gatherer_entities": 0,
        "comments": 0,
    }
    assert payload["bundles"][0]["guide_id"] == 3143
    assert payload["bundles"][0]["match_count"] == 2
    assert payload["bundles"][0]["match_counts"]["linked_entities"] == 1
    assert payload["bundles"][0]["suggested_query_command"] == (
        f"wowhead guide-query 3143 obliterate --root {root}"
    )
    assert payload["top"][0]["kind"] == "linked_entity"
    assert payload["top"][0]["bundle"]["guide_id"] == 3143


def test_guide_bundle_query_uses_filters_and_root_index(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    frost = _write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 49020,
                "name": "Obliterate",
                "url": "https://www.wowhead.com/spell=49020/obliterate",
                "citation_url": "https://www.wowhead.com/spell=49020/obliterate",
                "sources": ["href", "gatherer"],
                "source_kind": "href",
            }
        ],
    )
    _write_bundle_fixture(
        root,
        dir_name="guide-42-arcane",
        guide_id=42,
        title="Arcane Mage Guide",
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 30451,
                "name": "Obliterate Echo",
                "url": "https://www.wowhead.com/spell=30451/obliterate-echo",
                "citation_url": "https://www.wowhead.com/spell=30451/obliterate-echo",
                "sources": ["href"],
                "source_kind": "href",
            }
        ],
    )
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    (root / "index.json").write_text(
        json.dumps(
            {
                "index_version": 1,
                "updated_at": now,
                "root": str(root),
                "count": 2,
                "bundles": [
                    {
                        "path": str(frost),
                        "dir_name": frost.name,
                        "guide_id": 3143,
                        "title": "Frost Death Knight DPS Guide - Midnight",
                        "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                        "expansion": "retail",
                        "export_version": 2,
                        "counts": {
                            "sections": 0,
                            "navigation_links": 0,
                            "linked_entities": 1,
                            "gatherer_entities": 0,
                            "hydrated_entities": 0,
                            "comments": 0,
                        },
                        "exported_at": now,
                        "guide_fetched_at": now,
                        "hydration": {
                            "enabled": False,
                            "types": [],
                            "limit": 0,
                            "hydrated_at": None,
                            "hydrated_entities": 0,
                            "source_counts": {},
                        },
                    },
                    {
                        "path": str(root / "guide-42-arcane"),
                        "dir_name": "guide-42-arcane",
                        "guide_id": 42,
                        "title": "Arcane Mage Guide",
                        "canonical_url": "https://www.wowhead.com/guide/42",
                        "expansion": "classic",
                        "export_version": 2,
                        "counts": {
                            "sections": 0,
                            "navigation_links": 0,
                            "linked_entities": 1,
                            "gatherer_entities": 0,
                            "hydrated_entities": 0,
                            "comments": 0,
                        },
                        "exported_at": now,
                        "guide_fetched_at": now,
                        "hydration": {
                            "enabled": False,
                            "types": [],
                            "limit": 0,
                            "hydrated_at": None,
                            "hydrated_entities": 0,
                            "source_counts": {},
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def fail_scan(root_path: Path) -> list[dict[str, object]]:  # noqa: ANN202
        raise AssertionError(f"scan should not be used when a valid index exists: {root_path}")

    monkeypatch.setattr("wowhead_cli.main._scan_guide_bundle_rows", fail_scan)

    result = runner.invoke(
        app,
        [
            "guide-bundle-query",
            "obliterate",
            "--root",
            str(root),
            "--kind",
            "linked_entities",
            "--linked-source",
            "multi",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["filters"]["kinds"] == ["linked_entities"]
    assert payload["filters"]["linked_sources"] == ["multi"]
    assert payload["counts"] == {
        "sections": 0,
        "analysis_surfaces": 0,
        "navigation": 0,
        "linked_entities": 1,
        "gatherer_entities": 0,
        "comments": 0,
    }
    assert payload["bundles"][0]["guide_id"] == 3143
    assert set(payload["top"][0]["sources"]) == {"href", "gatherer"}


def test_guide_bundle_inspect_reports_counts_and_index_status(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = _write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
        sections=[
            {
                "ordinal": 1,
                "level": 2,
                "title": "Rotation",
                "content_text": "Use Obliterate.",
            }
        ],
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 49020,
                "name": "Obliterate",
                "url": "https://www.wowhead.com/spell=49020/obliterate",
                "citation_url": "https://www.wowhead.com/spell=49020/obliterate",
                "sources": ["href", "gatherer"],
                "source_kind": "href",
            }
        ],
        comments=[
            {
                "id": 7,
                "user": "Tester",
                "body": "Obliterate section is clear.",
                "citation_url": "https://www.wowhead.com/guide=3143#comments:id=7",
            }
        ],
    )
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["hydrated_entities"] = 1
    manifest["hydration"] = {
        "enabled": True,
        "types": ["spell"],
        "limit": 1,
        "hydrated_at": manifest["exported_at"],
        "source_counts": {"entity_cache": 1},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    entities_dir = bundle_dir / "entities"
    entities_dir.mkdir()
    (entities_dir / "manifest.json").write_text(
        json.dumps(
            {
                "hydrated_at": manifest["exported_at"],
                "count": 1,
                "counts_by_type": {"spell": 1},
                "counts_by_storage_source": {"entity_cache": 1},
                "items": [
                    {
                        "entity_type": "spell",
                        "id": 49020,
                        "path": "entities/spell/49020.json",
                        "stored_at": manifest["exported_at"],
                        "storage_source": "entity_cache",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rebuild = runner.invoke(app, ["guide-bundle-index-rebuild", "--root", str(root)])
    assert rebuild.exit_code == 0

    result = runner.invoke(app, ["guide-bundle-inspect", "3143", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["guide"]["id"] == 3143
    assert payload["freshness"]["bundle"] == "fresh"
    assert payload["freshness"]["bundle_reasons"] == []
    assert payload["freshness"]["hydration"] == "fresh"
    assert payload["freshness"]["hydration_reasons"] == []
    assert payload["counts"]["manifest"] == payload["counts"]["observed"]
    assert payload["hydration"]["enabled"] is True
    assert payload["entities_manifest"]["count"] == 1
    assert payload["index"]["valid"] is True
    assert payload["index"]["contains_bundle"] is True
    assert payload["issues"] == []

    summary_result = runner.invoke(app, ["guide-bundle-inspect", "3143", "--root", str(root), "--summary"])
    assert summary_result.exit_code == 0
    summary_payload = json.loads(summary_result.stdout)
    assert summary_payload["issue_count"] == 0
    assert summary_payload["issue_codes"] == []
    assert summary_payload["missing_files"] == []
    assert summary_payload["count_mismatches"] == []


def test_guide_bundle_inspect_reports_missing_files_and_invalid_index(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = _write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
        sections=[
            {
                "ordinal": 1,
                "level": 2,
                "title": "Rotation",
                "content_text": "Use Obliterate.",
            }
        ],
    )
    (bundle_dir / "sections.jsonl").unlink()
    (root / "index.json").write_text(json.dumps({"broken": True}), encoding="utf-8")

    result = runner.invoke(app, ["guide-bundle-inspect", str(bundle_dir)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    issue_codes = {row["code"] for row in payload["issues"]}
    assert {"missing_file", "count_mismatch", "invalid_index"}.issubset(issue_codes)
    assert payload["files"]["sections_jsonl"]["exists"] is False
    assert payload["counts"]["manifest"]["sections"] == 1
    assert payload["counts"]["observed"]["sections"] == 0
    assert payload["index"]["exists"] is True
    assert payload["index"]["valid"] is False


def test_guide_bundle_index_rebuild_rewrites_invalid_index(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    _write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
    )
    _write_bundle_fixture(
        root,
        dir_name="guide-42-arcane",
        guide_id=42,
        title="Arcane Mage Guide",
        expansion="classic",
    )
    (root / "index.json").write_text(json.dumps({"broken": True}), encoding="utf-8")

    result = runner.invoke(app, ["guide-bundle-index-rebuild", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["index"]["previous"] == {"exists": True, "valid": False, "count": 0}
    assert payload["index"]["current"] == {"exists": True, "valid": True, "count": 2}

    rebuilt_index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert rebuilt_index["count"] == 2
    assert {row["guide_id"] for row in rebuilt_index["bundles"]} == {42, 3143}


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

    payload = json.loads(result.stdout)
    assert calls == [("classic", None)]
    assert payload["expansion"] == "classic"
    assert payload["entity"]["name"] == "Thunderfury"
    assert payload["entity"]["page_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert "tooltip" not in payload
    assert payload["citations"]["comments"] == "https://www.wowhead.com/item=19019/thunderfury#comments"
    assert payload["comments"]["count"] == 1
    assert payload["comments"]["all_comments_included"] is True
    assert payload["comments"]["needs_raw_fetch"] is False
    assert payload["comments"]["top"][0]["citation_url"].endswith("#comments:id=11")
    assert payload["linked_entities"]["count"] >= 1
    assert payload["linked_entities"]["counts_by_type"]["npc"] == 1
    assert payload["linked_entities"]["fetch_more_command"] == "wowhead entity-page item 19019 --max-links 200"


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
    assert payload["entity"]["type"] == "mount"
    assert payload["entity"]["id"] == 460
    assert payload["entity"]["page_url"] == "https://www.wowhead.com/item=84101/reins-of-the-grand-expedition-yak"
    assert payload["linked_entities"]["count"] == 1


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
    assert payload["entity"]["type"] == "battle-pet"
    assert payload["entity"]["id"] == 39
    assert payload["entity"]["page_url"] == "https://www.wowhead.com/npc=2671/mechanical-squirrel"
    assert payload["comments"][0]["citation_url"].endswith("#comments:id=11")


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
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {
            "name": "Grand Expedition Yak",
            "tooltip": (
                "<table><tr><td><b>Grand Expedition Yak</b><br>Item Level 10<br>Mount (Account-wide)</td></tr></table>"
                "<table><tr><td>Use: Teaches you how to summon this three-person mount with vendors.</td></tr></table>"
            ),
        }

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
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


def test_invalid_expansion_is_rejected() -> None:
    result = runner.invoke(app, ["--expansion", "not-a-real-expansion", "search", "defias"])
    assert result.exit_code != 0
    assert "Unknown expansion" in result.output


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
