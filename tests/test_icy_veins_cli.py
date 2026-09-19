from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from icy_veins_cli.main import app
from icy_veins_cli.page_parser import CLASS_HUB_SLUGS, classify_guide_slug, parse_guide_page, parse_sitemap_guides
from icy_veins_cli.provider import resolve_payload
from icy_veins_cli.search import NEUTRAL_SLUG_TERMS, resolve_is_confident, score_family_match
from typer.testing import CliRunner
from warcraft_core.envelope import envelope_violations

runner = CliRunner()

INTRO_HTML = """
<html>
  <head>
    <title>Mistweaver Monk Healing Guide - Midnight (12.0.1) - World of Warcraft - Icy Veins</title>
    <meta name="description" content="This guide contains everything you need to know to be an excellent Mistweaver Monk.">
    <meta property="og:title" content="Mistweaver Monk Healing Guide - Midnight (12.0.1)">
    <link rel="canonical" href="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide">
    <script>
      dataLayer = [{
        'author': 'dhaubbs',
        'page_type': 'guides',
        'page_game': 'wow',
        'page_cat1': 'monk',
        'page_cat2': 'mistweaver',
        'page_cat3': 'intro'
      }];
    </script>
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": "Mistweaver Monk Healing Guide - Midnight (12.0.1)",
        "dateModified": "2026-03-05T05:19:00+00:00",
        "datePublished": "2012-09-13T02:17:00+00:00",
        "author": {"@type": "Person", "name": "Dhaubbs"},
        "description": "This guide contains everything you need to know to be an excellent Mistweaver Monk."
      }
    </script>
  </head>
  <body>
    <div class="page_content_container text_color">
      <div class="page_content_header">
        <div class="page_content_header_meta">
          <span class="page_author"><span>Last updated <span class="local_date">on <span class="local_date_date">Mar 05, 2026</span> at <span class="local_date_hour">05:19</span></span></span><span>&nbsp;by <span style="color:#fff;">Dhaubbs</span></span></span>
          <span class="page_comments"><a href="https://www.icy-veins.com/forums/topic/52937-mistweaver-monk-pve">48 comments</a></span>
        </div>
        <div class="page_content_header_intro">
          <span>General Information</span>
          <p>Welcome to our Mistweaver Monk guide for World of Warcraft.</p>
        </div>
      </div>
      <div class="toc">
        <div class="toc_page_list toc_page_list_monk">
          <div class="toc_page_center_item">
            <span class="toc_page_list_item selected">
              <a href="//www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide">
                <span class="toc_page_list_item_icon"></span>
                <span>Mistweaver Monk Guide</span>
              </a>
            </span>
          </div>
          <div class="toc_page_list_items">
            <span class="toc_page_list_item">
              <a href="//www.icy-veins.com/wow/mistweaver-monk-leveling-guide"><span></span><span>Leveling</span></a>
            </span>
            <span class="toc_page_list_item">
              <a href="//www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority"><span></span><span>Stat Priority</span></a>
            </span>
          </div>
        </div>
        <div class="toc_page_content">
          <div class="toc_page_content_items">
            <ul>
              <li><span><a href="//www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide#mistweaver-overview">1. Mistweaver Overview</a></span></li>
              <li><span><a href="//www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide#talents">2. Talents</a></span></li>
            </ul>
          </div>
        </div>
      </div>
      <div class="page_content">
        <div class="raider-io-links"><a href="https://raider.io/example">Ignore</a></div>
        <div class="heading_container heading_number_2"><span>1.</span><h2 id="mistweaver-overview">Mistweaver Overview</h2></div>
        <p>Use <a href="https://www.wowhead.com/spell=116670/vivify">Vivify</a> well.</p>
        <p>Reference build: <a href="https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123">Raid Build</a>.</p>
        <div class="heading_container heading_number_3"><span>2.</span><h3 id="talents">Talents</h3></div>
        <p>See our <a href="/wow/mistweaver-monk-pve-healing-stat-priority">Stat Priority</a> page.</p>
      </div>
    </div>
  </body>
</html>
"""

STATS_HTML = """
<html>
  <head>
    <title>Mistweaver Monk Stat Priority - Midnight (12.0.1) - World of Warcraft - Icy Veins</title>
    <meta name="description" content="Learn the Mistweaver Monk stat priority.">
    <meta property="og:title" content="Mistweaver Monk Stat Priority - Midnight (12.0.1)">
    <link rel="canonical" href="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority">
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": "Mistweaver Monk Stat Priority - Midnight (12.0.1)",
        "dateModified": "2026-03-05T05:19:00+00:00",
        "datePublished": "2012-09-13T02:17:00+00:00",
        "author": {"@type": "Person", "name": "Dhaubbs"},
        "description": "Learn the Mistweaver Monk stat priority."
      }
    </script>
  </head>
  <body>
    <div class="page_content_container text_color">
      <div class="toc">
        <div class="toc_page_list toc_page_list_monk">
          <div class="toc_page_center_item">
            <span class="toc_page_list_item">
              <a href="//www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"><span></span><span>Mistweaver Monk Guide</span></a>
            </span>
          </div>
          <div class="toc_page_list_items">
            <span class="toc_page_list_item selected">
              <a href="//www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority"><span></span><span>Stat Priority</span></a>
            </span>
          </div>
        </div>
      </div>
      <div class="page_content">
        <div class="heading_container heading_number_2"><span>1.</span><h2 id="stats">Stat Priority</h2></div>
        <p>Prioritize Critical Strike and Versatility.</p>
      </div>
    </div>
  </body>
</html>
"""

LEVELING_HTML = """
<html>
  <head>
    <title>Mistweaver Monk Leveling Guide - Midnight (12.0.1) - World of Warcraft - Icy Veins</title>
    <meta name="description" content="Leveling as Mistweaver Monk.">
    <meta property="og:title" content="Mistweaver Monk Leveling Guide - Midnight (12.0.1)">
    <link rel="canonical" href="https://www.icy-veins.com/wow/mistweaver-monk-leveling-guide">
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": "Mistweaver Monk Leveling Guide - Midnight (12.0.1)",
        "dateModified": "2026-03-05T05:19:00+00:00",
        "datePublished": "2012-09-13T02:17:00+00:00",
        "author": {"@type": "Person", "name": "Dhaubbs"},
        "description": "Leveling as Mistweaver Monk."
      }
    </script>
  </head>
  <body>
    <div class="page_content_container text_color">
      <div class="toc">
        <div class="toc_page_list toc_page_list_monk">
          <div class="toc_page_center_item">
            <span class="toc_page_list_item">
              <a href="//www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"><span></span><span>Mistweaver Monk Guide</span></a>
            </span>
          </div>
          <div class="toc_page_list_items">
            <span class="toc_page_list_item selected">
              <a href="//www.icy-veins.com/wow/mistweaver-monk-leveling-guide"><span></span><span>Leveling</span></a>
            </span>
          </div>
        </div>
      </div>
      <div class="page_content">
        <div class="heading_container heading_number_2"><span>1.</span><h2 id="leveling">Leveling</h2></div>
        <p>Level with Tiger Palm and Vivify.</p>
      </div>
    </div>
  </body>
</html>
"""

CLASS_HUB_HTML = """
<html>
  <head>
    <title>Monk Guide - Midnight (12.0.1) - World of Warcraft - Icy Veins</title>
    <meta name="description" content="Monk hub guide.">
    <meta property="og:title" content="Monk Guide - Midnight (12.0.1)">
    <link rel="canonical" href="https://www.icy-veins.com/wow/monk-guide">
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": "Monk Guide - Midnight (12.0.1)",
        "dateModified": "2026-03-05T05:19:00+00:00",
        "author": {"@type": "Person", "name": "Dhaubbs"},
        "description": "Monk hub guide."
      }
    </script>
  </head>
  <body>
    <div class="page_content_container text_color">
      <div class="toc">
        <div class="toc_page_list toc_page_list_classes">
          <div class="toc_page_list_items">
            <span class="toc_page_list_item">
              <a href="//www.icy-veins.com/wow/death-knight-guide"><span></span><span>Death Knight</span></a>
            </span>
            <span class="toc_page_list_item selected">
              <a href="//www.icy-veins.com/wow/monk-guide"><span></span><span>Monk</span></a>
            </span>
            <span class="toc_page_list_item">
              <a href="//www.icy-veins.com/wow/warrior-guide"><span></span><span>Warrior</span></a>
            </span>
          </div>
        </div>
      </div>
      <div class="page_content">
        <div class="heading_container heading_number_2"><span>1.</span><h2 id="midnight-monk-specializations">Midnight Monk Specializations</h2></div>
        <p>See the monk specializations and related guides.</p>
      </div>
    </div>
  </body>
</html>
"""

EASY_MODE_HTML = """
<html>
  <head>
    <title>Fury Warrior DPS Easy Mode - Midnight (12.0.1) - World of Warcraft - Icy Veins</title>
    <meta name="description" content="Easy mode for Fury Warrior.">
    <meta property="og:title" content="Fury Warrior DPS Easy Mode - Midnight (12.0.1)">
    <link rel="canonical" href="https://www.icy-veins.com/wow/fury-warrior-pve-dps-easy-mode">
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": "Fury Warrior DPS Easy Mode - Midnight (12.0.1)",
        "dateModified": "2026-03-05T05:19:00+00:00",
        "author": {"@type": "Person", "name": "Archimtiros"},
        "description": "Easy mode for Fury Warrior."
      }
    </script>
  </head>
  <body>
    <div class="page_content_container text_color">
      <div class="toc">
        <div class="toc_page_list toc_page_list_warrior">
          <div class="toc_page_center_item">
            <span class="toc_page_list_item">
              <a href="//www.icy-veins.com/wow/fury-warrior-pve-dps-guide"><span></span><span>Fury Warrior Guide</span></a>
            </span>
          </div>
          <div class="toc_page_list_items">
            <span class="toc_page_list_item selected">
              <a href="//www.icy-veins.com/wow/fury-warrior-pve-dps-easy-mode"><span></span><span>Easy Mode</span></a>
            </span>
            <span class="toc_page_list_item">
              <a href="//www.icy-veins.com/wow/fury-warrior-pve-dps-stat-priority"><span></span><span>Stat Priority</span></a>
            </span>
          </div>
        </div>
      </div>
      <div class="page_content">
        <div class="heading_container heading_number_2"><span>1.</span><h2 id="foreword">Foreword</h2></div>
        <p>Simple Fury Warrior guidance.</p>
      </div>
    </div>
  </body>
</html>
"""

SITEMAP_XML = """
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide</loc></url>
  <url><loc>https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority</loc></url>
  <url><loc>https://www.icy-veins.com/wow/mistweaver-monk-leveling-guide</loc></url>
  <url><loc>https://www.icy-veins.com/wow/news-roundup</loc></url>
  <url><loc>https://www.icy-veins.com/hearthstone/decks</loc></url>
</urlset>
"""


def _fake_fetch_guide_page(guide_ref: str) -> dict[str, object]:
    if str(guide_ref).endswith("fury-warrior-pve-dps-easy-mode"):
        return parse_guide_page(
            EASY_MODE_HTML,
            source_url="https://www.icy-veins.com/wow/fury-warrior-pve-dps-easy-mode",
        )
    if str(guide_ref).endswith("monk-guide"):
        return parse_guide_page(
            CLASS_HUB_HTML,
            source_url="https://www.icy-veins.com/wow/monk-guide",
        )
    if str(guide_ref).endswith("mistweaver-monk-pve-healing-stat-priority"):
        return parse_guide_page(
            STATS_HTML,
            source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority",
        )
    if str(guide_ref).endswith("mistweaver-monk-leveling-guide"):
        return parse_guide_page(
            LEVELING_HTML,
            source_url="https://www.icy-veins.com/wow/mistweaver-monk-leveling-guide",
        )
    return parse_guide_page(
        INTRO_HTML,
        source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
    )


def test_parse_sitemap_guides_filters_wow_guide_like_pages() -> None:
    guides = parse_sitemap_guides(SITEMAP_XML)
    assert guides == [
        {
            "content_family": "leveling",
            "slug": "mistweaver-monk-leveling-guide",
            "name": "Mistweaver Monk Leveling Guide",
            "url": "https://www.icy-veins.com/wow/mistweaver-monk-leveling-guide",
        },
        {
            "content_family": "spec_guide",
            "slug": "mistweaver-monk-pve-healing-guide",
            "name": "Mistweaver Monk PvE Healing Guide",
            "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
        },
        {
            "content_family": "stat_priority",
            "slug": "mistweaver-monk-pve-healing-stat-priority",
            "name": "Mistweaver Monk PvE Healing Stat Priority",
            "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority",
        },
    ]


def test_parse_guide_page_extracts_navigation_toc_sections_and_links() -> None:
    payload = parse_guide_page(
        INTRO_HTML,
        source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
    )
    assert payload["guide"]["slug"] == "mistweaver-monk-pve-healing-guide"
    assert payload["guide"]["author"] == "Dhaubbs"
    assert payload["guide"]["content_family"] == "spec_guide"
    assert payload["guide"]["traversal_scope"] == "family_navigation"
    assert payload["guide"]["last_updated"] == "2026-03-05T05:19:00+00:00"
    assert payload["page"]["page_type"] == "guides"
    assert payload["navigation"][0]["active"] is True
    assert payload["page_toc"][0]["title"] == "Mistweaver Overview"
    assert payload["article"]["intro_text"].startswith("General Information")
    assert payload["article"]["sections"][0]["title"] == "Mistweaver Overview"
    assert [row["title"] for row in payload["article"]["headings"]] == ["Mistweaver Overview", "Talents"]
    linked = {(row["type"], row["id"]) for row in payload["linked_entities"]}
    assert ("spell", 116670) in linked
    assert ("page", "mistweaver-monk-pve-healing-stat-priority") in linked
    rows_by_key = {(row["type"], row["id"]): row for row in payload["linked_entities"]}
    spell_identity = rows_by_key[("spell", 116670)]["ability_identity"]
    assert spell_identity["kind"] == "ability_identity"
    assert spell_identity["status"] == "canonical"
    assert spell_identity["identity"]["spell_id"] == 116670
    assert spell_identity["source"] == {"provider": "icy-veins", "source": "guide_linked_entity"}
    # Non-spell entity rows (icy-veins internal page refs) are unchanged.
    assert "ability_identity" not in rows_by_key[("page", "mistweaver-monk-pve-healing-stat-priority")]
    assert payload["build_references"][0]["build_code"] == "ABC123"
    assert payload["build_references"][0]["build_identity"]["class_spec_identity"]["identity"] == {
        "actor_class": "monk", "spec": "mistweaver"}


def test_classify_guide_slug_distinguishes_supported_families() -> None:
    assert classify_guide_slug("monk-guide") == "class_hub"
    assert classify_guide_slug("healing-guide") == "role_guide"
    assert classify_guide_slug("mistweaver-monk-pve-healing-guide") == "spec_guide"
    assert classify_guide_slug("fury-warrior-pve-dps-easy-mode") == "easy_mode"
    assert classify_guide_slug("mistweaver-monk-leveling-guide") == "leveling"
    assert classify_guide_slug("mistweaver-monk-pvp-guide") == "pvp"
    assert classify_guide_slug("mistweaver-monk-pve-healing-stat-priority") == "stat_priority"
    assert classify_guide_slug("mistweaver-monk-pve-healing-nerub-ar-palace-raid-guide") == "raid_guide"
    assert classify_guide_slug("mistweaver-monk-the-war-within-pve-guide") == "expansion_guide"
    assert classify_guide_slug("mistweaver-monk-mists-of-pandaria-remix-guide") == "special_event_guide"
    assert classify_guide_slug("news-roundup") is None


def test_score_family_match_boosts_broad_and_specialized_families() -> None:
    class_score, class_reasons = score_family_match("monk", content_family="class_hub")
    easy_score, easy_reasons = score_family_match("fury warrior easy mode", content_family="easy_mode")

    assert class_score == 18
    assert class_reasons == ["family_class_hub"]
    assert easy_score >= 28
    assert "family_easy_mode" in easy_reasons


def test_score_family_match_penalizes_broad_hubs_for_specialized_queries() -> None:
    score, reasons = score_family_match("monk leveling", content_family="class_hub")

    assert score == -14
    assert reasons == ["penalty_broad_hub"]


def test_icy_veins_search_command_uses_sitemap_guides(monkeypatch) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["search", "mistweaver monk guide", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 3
    assert payload["results"][0]["id"] == "mistweaver-monk-pve-healing-guide"
    assert payload["results"][0]["metadata"]["content_family"] == "spec_guide"
    assert payload["results"][0]["follow_up"]["recommended_command"] == "icy-veins guide mistweaver-monk-pve-healing-guide"


def test_icy_veins_search_command_boosts_broad_hubs_for_broad_queries(monkeypatch) -> None:
    sitemap = parse_sitemap_guides(SITEMAP_XML) + [
        {
            "slug": "monk-guide",
            "name": "Monk Guide",
            "url": "https://www.icy-veins.com/wow/monk-guide",
            "content_family": "class_hub",
        },
    ]
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: sitemap)
    result = runner.invoke(app, ["search", "monk guide", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["results"][0]["id"] == "monk-guide"
    assert "family_class_hub" in payload["results"][0]["ranking"]["match_reasons"]


def test_icy_veins_resolve_command_returns_best_guide(monkeypatch) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["resolve", "mistweaver monk guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["next_command"] == "icy-veins guide mistweaver-monk-pve-healing-guide"


def test_icy_veins_resolve_command_prefers_role_hubs_for_broad_role_queries(monkeypatch) -> None:
    sitemap = parse_sitemap_guides(SITEMAP_XML) + [
        {
            "slug": "healing-guide",
            "name": "Healing Guide",
            "url": "https://www.icy-veins.com/wow/healing-guide",
            "content_family": "role_guide",
        },
    ]
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: sitemap)
    result = runner.invoke(app, ["resolve", "healing guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "healing-guide"
    assert "family_role_guide" in payload["match"]["ranking"]["match_reasons"]


def test_icy_veins_resolve_command_prefers_easy_mode_when_query_matches(monkeypatch) -> None:
    sitemap = parse_sitemap_guides(SITEMAP_XML) + [
        {
            "slug": "fury-warrior-pve-dps-easy-mode",
            "name": "Fury Warrior PvE DPS Easy Mode",
            "url": "https://www.icy-veins.com/wow/fury-warrior-pve-dps-easy-mode",
            "content_family": "easy_mode",
        },
        {
            "slug": "warrior-guide",
            "name": "Warrior Guide",
            "url": "https://www.icy-veins.com/wow/warrior-guide",
            "content_family": "class_hub",
        },
    ]
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: sitemap)
    result = runner.invoke(app, ["resolve", "fury warrior easy mode"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["match"]["id"] == "fury-warrior-pve-dps-easy-mode"
    assert payload["resolved"] is True
    assert payload["next_command"] == "icy-veins guide fury-warrior-pve-dps-easy-mode"


def test_icy_veins_resolve_confidence_helper_covers_easy_mode_and_intro_paths() -> None:
    easy_mode_top = {"ranking": {"score": 35, "match_reasons": ["family_easy_mode"]}}
    easy_mode_second = {"ranking": {"score": 24, "match_reasons": []}}
    assert resolve_is_confident(easy_mode_top, easy_mode_second) is True

    intro_top = {"ranking": {"score": 30, "match_reasons": ["intro_guide"]}}
    intro_second = {"ranking": {"score": 23, "match_reasons": []}}
    assert resolve_is_confident(intro_top, intro_second) is True

    weak_top = {"ranking": {"score": 29, "match_reasons": []}}
    weak_second = {"ranking": {"score": 25, "match_reasons": []}}
    assert resolve_is_confident(weak_top, weak_second) is False


def test_icy_veins_resolve_search_payload_uses_confidence_helper() -> None:
    payload = resolve_payload(
        query="fury warrior easy mode",
        search_query="fury warrior easy mode",
        results=[
            {
                "id": "fury-warrior-pve-dps-easy-mode",
                "name": "Fury Warrior PvE DPS Easy Mode",
                "ranking": {"score": 35, "match_reasons": ["family_easy_mode"]},
                "follow_up": {"recommended_command": "icy-veins guide fury-warrior-pve-dps-easy-mode"},
            },
            {
                "id": "warrior-guide",
                "name": "Warrior Guide",
                "ranking": {"score": 24, "match_reasons": []},
                "follow_up": {"recommended_command": "icy-veins guide warrior-guide"},
            },
        ],
        total_count=2,
        scope_hint=None,
    )

    assert payload["resolved"] is True
    assert payload["next_command"] == "icy-veins guide fury-warrior-pve-dps-easy-mode"


def test_icy_veins_search_penalizes_broad_hubs_for_specialized_queries(monkeypatch) -> None:
    sitemap = parse_sitemap_guides(SITEMAP_XML) + [
        {
            "slug": "fury-warrior-pve-dps-easy-mode",
            "name": "Fury Warrior PvE DPS Easy Mode",
            "url": "https://www.icy-veins.com/wow/fury-warrior-pve-dps-easy-mode",
            "content_family": "easy_mode",
        },
        {
            "slug": "warrior-guide",
            "name": "Warrior Guide",
            "url": "https://www.icy-veins.com/wow/warrior-guide",
            "content_family": "class_hub",
        },
    ]
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: sitemap)
    result = runner.invoke(app, ["search", "fury warrior easy mode", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["results"][0]["id"] == "fury-warrior-pve-dps-easy-mode"
    last_match = next(row for row in payload["results"] if row["id"] == "warrior-guide")
    assert "penalty_broad_hub" in last_match["ranking"]["match_reasons"]


def test_icy_veins_search_returns_scope_hint_for_unsupported_query_family(monkeypatch) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["search", "patch notes", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["scope_hint"]["code"] == "patch_notes"


def test_icy_veins_resolve_returns_scope_hint_for_unsupported_query_family(monkeypatch) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["resolve", "latest class changes", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is False
    assert payload["count"] == 0
    assert payload["candidates"] == []
    assert payload["scope_hint"]["code"] == "class_changes"


def test_icy_veins_guide_and_guide_full(monkeypatch) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.fetch_guide_page", lambda self, guide_ref: _fake_fetch_guide_page(guide_ref))
    guide_result = runner.invoke(app, ["guide", "mistweaver-monk-pve-healing-guide"])
    assert guide_result.exit_code == 0
    guide_payload = json.loads(guide_result.stdout)
    assert guide_payload["guide"]["slug"] == "mistweaver-monk-pve-healing-guide"
    assert guide_payload["linked_entities"]["count"] == 2
    assert guide_payload["build_references"]["count"] == 1
    assert guide_payload["analysis_surfaces"]["count"] == 1
    assert guide_payload["page_toc"]["count"] == 2

    full_result = runner.invoke(app, ["guide-full", "mistweaver-monk-pve-healing-guide"])
    assert full_result.exit_code == 0
    full_payload = json.loads(full_result.stdout)
    assert full_payload["guide"]["page_count"] == 3
    assert full_payload["linked_entities"]["count"] >= 2
    assert full_payload["build_references"]["count"] == 1
    assert full_payload["analysis_surfaces"]["count"] == 3
    assert full_payload["pages"][1]["guide"]["section_slug"] in {
        "mistweaver-monk-leveling-guide",
        "mistweaver-monk-pve-healing-stat-priority",
    }


def test_icy_veins_guide_full_keeps_class_hubs_local(monkeypatch) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.fetch_guide_page", lambda self, guide_ref: _fake_fetch_guide_page(guide_ref))
    result = runner.invoke(app, ["guide-full", "monk-guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["guide"]["content_family"] == "class_hub"
    assert payload["guide"]["page_count"] == 1
    assert payload["navigation"]["count"] == 1
    assert payload["pages"][0]["guide"]["slug"] == "monk-guide"


def test_icy_veins_guide_export_and_query(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.fetch_guide_page", lambda self, guide_ref: _fake_fetch_guide_page(guide_ref))
    export_dir = tmp_path / "guide-mistweaver-monk"

    export_result = runner.invoke(app, ["guide-export", "mistweaver-monk-pve-healing-guide", "--out", str(export_dir)])
    assert export_result.exit_code == 0
    export_payload = json.loads(export_result.stdout)
    assert export_payload["counts"]["pages"] == 3
    assert (export_dir / "manifest.json").exists()
    manifest = json.loads((export_dir / "manifest.json").read_text())
    assert datetime.fromisoformat(manifest["exported_at"].replace("Z", "+00:00")).tzinfo is not None
    assert (export_dir / "pages" / "mistweaver-monk-pve-healing-guide.html").exists()

    query_result = runner.invoke(app, ["guide-query", str(export_dir), "vivify", "--kind", "linked_entities"])
    assert query_result.exit_code == 0
    query_payload = json.loads(query_result.stdout)
    assert query_payload["count"] == 1
    assert query_payload["top"][0]["name"] == "Vivify"

    build_query = runner.invoke(app, ["guide-query", str(export_dir), "raid build abc123", "--kind", "build_references"])
    assert build_query.exit_code == 0
    build_query_payload = json.loads(build_query.stdout)
    assert build_query_payload["count"] == 1
    assert build_query_payload["top"][0]["build_code"] == "ABC123"

    analysis_query = runner.invoke(app, ["guide-query", str(export_dir), "stat priority", "--kind", "analysis_surfaces"])
    assert analysis_query.exit_code == 0
    analysis_query_payload = json.loads(analysis_query.stdout)
    assert analysis_query_payload["count"] >= 1
    assert "stat_priority" in analysis_query_payload["top"][0]["surface_tags"]

    section_query = runner.invoke(
        app,
        ["guide-query", str(export_dir), "critical strike", "--kind", "sections", "--section-title", "stat"],
    )
    assert section_query.exit_code == 0
    section_payload = json.loads(section_query.stdout)
    assert section_payload["match_counts"]["sections"] >= 1


def test_icy_veins_invalid_guide_ref_fails_structured() -> None:
    result = runner.invoke(app, ["guide", "news-roundup"])
    assert result.exit_code == 1

    payload = json.loads(result.stderr or result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_guide_ref"


def _connect_error(*_args, **_kwargs):
    raise httpx.ConnectError("connection refused")


def _status_error(status_code: int):
    def raise_status(*_args, **_kwargs):
        request = httpx.Request("GET", "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide")
        raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status_code, request=request))

    return raise_status


NETWORK_COMMANDS = [
    ["search", "mistweaver monk"],
    ["resolve", "mistweaver monk"],
    ["guide", "mistweaver-monk-pve-healing-guide"],
    ["guide-full", "mistweaver-monk-pve-healing-guide"],
    ["guide-export", "mistweaver-monk-pve-healing-guide"],
]


@pytest.mark.parametrize("args", NETWORK_COMMANDS, ids=lambda args: args[0])
def test_icy_veins_transport_failure_emits_error_envelope(monkeypatch, args: list[str]) -> None:
    monkeypatch.setattr("icy_veins_cli.client.request_with_retries", _connect_error)
    result = runner.invoke(app, args)

    assert result.exit_code == 5
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert envelope_violations(payload) == []
    assert payload["ok"] is False
    assert payload["provider"] == "icy-veins"
    assert payload["error"]["code"] == "network_error"


def test_icy_veins_missing_guide_page_exits_not_found(monkeypatch) -> None:
    monkeypatch.setattr("icy_veins_cli.client.request_with_retries", _status_error(404))
    result = runner.invoke(app, ["guide", "mistweaver-monk-pve-healing-guide"])

    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["details"]["status_code"] == 404


def test_icy_veins_search_payload_is_a_conforming_envelope(monkeypatch) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["search", "mistweaver monk guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert envelope_violations(payload) == []
    assert payload["kind"] == "search_results"
    assert payload["command"] == "search"
    # Legacy top-level keys stay alongside the envelope so existing agents keep working.
    assert payload["results"] == payload["data"]["results"]


NEWS_LINK_HTML = """
<html>
  <head>
    <link rel="canonical" href="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide">
  </head>
  <body>
    <div class="guide-page-content">
      <h2>Overview</h2>
      <p>See the <a href="https://www.icy-veins.com/wow/news/midnight-hotfixes-march-5th">hotfix roundup</a>
      and our <a href="/wow/mistweaver-monk-pve-healing-stat-priority">Stat Priority</a> page.</p>
    </div>
  </body>
</html>
"""

FAMILY_NAV_WITH_NEWS_LINK_HTML = """
<html>
  <head>
    <link rel="canonical" href="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide">
  </head>
  <body>
    <div class="table-of-contents">
      <nav>
        <a href="/wow/mistweaver-monk-pve-healing-guide">Mistweaver Monk Guide</a>
        <a href="https://www.icy-veins.com/wow/news/midnight-hotfixes-march-5th">Latest Hotfixes</a>
        <a href="/wow/mistweaver-monk-pve-healing-stat-priority">Stat Priority</a>
      </nav>
    </div>
    <div class="guide-page-content">
      <h2>Overview</h2>
      <p>Real prose.</p>
    </div>
  </body>
</html>
"""


def _unrecognised_layout_html(slug: str) -> str:
    """A page whose prose lives in a container the parser does not know: Icy Veins layout drift."""
    return f"""
<html>
  <head>
    <link rel="canonical" href="https://www.icy-veins.com/wow/{slug}">
  </head>
  <body>
    <div class="some-new-astro-wrapper">
      <h2>Overview</h2>
      <p>Real prose that the parser cannot see.</p>
    </div>
  </body>
</html>
"""


UNRECOGNISED_LAYOUT_HTML = _unrecognised_layout_html("mistweaver-monk-pve-healing-guide")

HEALER_SITEMAP_XML = """
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide</loc></url>
  <url><loc>https://www.icy-veins.com/wow/holy-paladin-pve-healing-guide</loc></url>
  <url><loc>https://www.icy-veins.com/wow/discipline-priest-pve-healing-guide</loc></url>
  <url><loc>https://www.icy-veins.com/wow/restoration-druid-pve-healing-guide</loc></url>
</urlset>
"""


def test_icy_veins_guide_survives_a_multi_segment_wow_link_in_the_article(monkeypatch) -> None:
    """A guide that links to a news post must still return its article, not an internal_error."""
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.fetch_guide_page",
        lambda self, guide_ref: parse_guide_page(
            NEWS_LINK_HTML,
            source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
        ),
    )
    result = runner.invoke(app, ["guide", "mistweaver-monk-pve-healing-guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert [row["id"] for row in payload["linked_entities"]["items"]] == ["mistweaver-monk-pve-healing-stat-priority"]


def test_icy_veins_guide_skips_a_non_guide_link_in_the_family_navigation(monkeypatch) -> None:
    """A news link in the family switcher is not a sibling page, and must not abort the parse."""
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.fetch_guide_page",
        lambda self, guide_ref: parse_guide_page(
            FAMILY_NAV_WITH_NEWS_LINK_HTML,
            source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
        ),
    )
    result = runner.invoke(app, ["guide", "mistweaver-monk-pve-healing-guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert [row["section_slug"] for row in payload["navigation"]["items"]] == [
        "mistweaver-monk-pve-healing-guide",
        "mistweaver-monk-pve-healing-stat-priority",
    ]


def test_icy_veins_guide_fails_when_the_article_container_is_missing(monkeypatch) -> None:
    """Layout drift must be an error, not ok:true with an empty article."""
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.fetch_guide_page",
        lambda self, guide_ref: parse_guide_page(
            UNRECOGNISED_LAYOUT_HTML,
            source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
        ),
    )
    result = runner.invoke(app, ["guide", "mistweaver-monk-pve-healing-guide"])

    assert result.exit_code == 1
    payload = json.loads(result.stderr or result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "parse_failed"
    assert payload["error"]["details"]["page_url"] == "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"


def test_icy_veins_guide_full_fails_when_the_first_page_has_no_article(monkeypatch) -> None:
    """The bundle commands must reject layout drift on the entry page too, not just `guide`."""
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.fetch_guide_page",
        lambda self, guide_ref: parse_guide_page(
            UNRECOGNISED_LAYOUT_HTML,
            source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
        ),
    )
    result = runner.invoke(app, ["guide-full", "mistweaver-monk-pve-healing-guide"])

    assert result.exit_code == 1
    payload = json.loads(result.stderr or result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "parse_failed"


BROKEN_FAMILY_PAGE_CASES = [
    ("network", "network_error"),
    ("unparsable", "parse_failed"),
    ("empty_article", "parse_failed"),
]


def _family_page_fetch(failure: str):
    """Fetch stub where the leveling sibling fails in ``failure`` mode and every other page is fine."""

    def fetch(self, guide_ref: str) -> dict[str, object]:
        slug = "mistweaver-monk-leveling-guide"
        if not str(guide_ref).endswith(slug):
            return _fake_fetch_guide_page(guide_ref)
        if failure == "network":
            raise httpx.ConnectError("boom", request=httpx.Request("GET", str(guide_ref)))
        if failure == "unparsable":
            raise ValueError("Failed to clone Icy Veins article node.")
        return parse_guide_page(
            _unrecognised_layout_html(slug),
            source_url=f"https://www.icy-veins.com/wow/{slug}",
        )

    return fetch


@pytest.mark.parametrize(("failure", "expected_code"), BROKEN_FAMILY_PAGE_CASES, ids=[case[0] for case in BROKEN_FAMILY_PAGE_CASES])
def test_icy_veins_guide_full_records_a_failed_family_page_and_keeps_going(monkeypatch, failure: str, expected_code: str) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.fetch_guide_page", _family_page_fetch(failure))
    result = runner.invoke(app, ["guide-full", "mistweaver-monk-pve-healing-guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["guide"]["page_count"] == 2
    assert payload["failed_pages"]["count"] == 1
    failed = payload["failed_pages"]["items"][0]
    assert failed["section_slug"] == "mistweaver-monk-leveling-guide"
    assert failed["error"]["code"] == expected_code


def test_icy_veins_guide_export_reports_failed_family_pages(monkeypatch, tmp_path: Path) -> None:
    """An exported bundle is partial when a sibling failed; the command must say so."""
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.fetch_guide_page", _family_page_fetch("network"))
    result = runner.invoke(
        app,
        ["guide-export", "mistweaver-monk-pve-healing-guide", "--out", str(tmp_path / "bundle")],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["counts"]["pages"] == 2
    assert payload["failed_pages"]["count"] == 1
    assert payload["failed_pages"]["items"][0]["section_slug"] == "mistweaver-monk-leveling-guide"


def test_icy_veins_guide_query_accepts_comma_separated_kinds(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.fetch_guide_page", lambda self, guide_ref: _fake_fetch_guide_page(guide_ref))
    export_dir = tmp_path / "guide-mistweaver-monk"
    assert runner.invoke(app, ["guide-export", "mistweaver-monk-pve-healing-guide", "--out", str(export_dir)]).exit_code == 0

    # Surrounding blanks and a trailing comma are what a shell-quoted list actually looks like.
    result = runner.invoke(app, ["guide-query", str(export_dir), "stat priority", "--kind", " navigation , analysis_surfaces ,"])
    assert result.exit_code == 0, result.stderr or result.stdout

    payload = json.loads(result.stdout)["data"]
    assert payload["match_counts"] == {
        "sections": 0,
        "navigation": 1,
        "linked_entities": 0,
        "build_references": 0,
        "analysis_surfaces": 1,
    }


def test_icy_veins_search_does_not_privilege_any_class_on_a_role_query(monkeypatch) -> None:
    """No class or spec name may be exempt from the off-query slug penalty."""
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: parse_sitemap_guides(HEALER_SITEMAP_XML))
    result = runner.invoke(app, ["search", "pve healing"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert len(payload["results"]) == 4
    assert len({row["ranking"]["score"] for row in payload["results"]}) == 1


def test_icy_veins_resolve_stays_unresolved_on_a_tied_role_query(monkeypatch) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: parse_sitemap_guides(HEALER_SITEMAP_XML))
    result = runner.invoke(app, ["resolve", "pve healing"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["resolved"] is False


SPEC_TOKENS = frozenset(
    {
        "blood", "frost", "unholy", "havoc", "vengeance", "balance", "feral", "guardian", "restoration",
        "devastation", "preservation", "augmentation", "beast", "mastery", "marksmanship", "survival",
        "arcane", "fire", "brewmaster", "mistweaver", "windwalker", "holy", "protection", "retribution",
        "discipline", "shadow", "assassination", "outlaw", "subtlety", "elemental", "enhancement",
        "affliction", "demonology", "destruction", "arms", "fury",
    }
)


def test_neutral_slug_terms_name_no_class_or_spec() -> None:
    """Exempting one class or spec from the off-query slug penalty hands it a permanent head start."""
    class_tokens = {slug.removesuffix("-guide") for slug in CLASS_HUB_SLUGS}
    class_tokens |= {part for token in class_tokens for part in token.split("-")}

    assert NEUTRAL_SLUG_TERMS & (class_tokens | SPEC_TOKENS) == set()


EXPORT_STRING_HTML = """
<html>
  <head>
    <link rel="canonical" href="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spec-builds-talents">
  </head>
  <body>
    <div class="guide-page-content">
      <h2>Talent Builds</h2>
      <div class="export-string">
        <div class="export-string__title">Rising Mist Raid</div>
        <div class="export-string__code">C4QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB</div>
      </div>
      <div class="export-string">
        <div class="export-string__title">Rising Mist Mythic+</div>
        <div class="export-string__code">C4QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB</div>
      </div>
      <div class="export-string">
        <div class="export-string__title">Copy button</div>
        <div class="export-string__code">Copy</div>
      </div>
    </div>
  </body>
</html>
"""


def _export_string_builds() -> list[dict[str, object]]:
    payload = parse_guide_page(
        EXPORT_STRING_HTML,
        source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spec-builds-talents",
    )
    return payload["build_references"]


def test_icy_veins_ignores_export_blocks_that_hold_no_loadout_import_string() -> None:
    """Button labels and placeholders share the export-string markup; only real import strings are builds."""
    assert [row["build_code"] for row in _export_string_builds()] == ["C4QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB"]


def test_icy_veins_keeps_the_first_label_when_one_import_string_is_published_twice() -> None:
    """Two builds can share a loadout string; the first published name wins so output is stable."""
    assert [row["label"] for row in _export_string_builds()] == ["Rising Mist Raid"]


@pytest.mark.parametrize("query", ["hotfixes", "hotfix", "latest hotfixes"])
def test_icy_veins_search_hints_the_hotfix_boundary_for_realistic_queries(monkeypatch, query: str) -> None:
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["search", query])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["scope_hint"]["code"] == "hotfixes"
    assert payload["count"] == 0
