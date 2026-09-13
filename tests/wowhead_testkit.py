"""Shared synthetic Wowhead HTML fixtures and bundle builders for the wowhead CLI tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

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


def write_bundle_fixture(
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

