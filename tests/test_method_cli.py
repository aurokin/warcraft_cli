from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from method_cli.main import app
from method_cli.page_parser import classify_guide_family, parse_guide_page, parse_sitemap_guides
from method_cli.provider import PROVIDER
from typer.testing import CliRunner
from warcraft_core.envelope import envelope_violations

runner = CliRunner()

INTRO_HTML = """
<html>
  <head>
    <title>Method Mistweaver Monk Guide - Introduction - Midnight 12.0.1</title>
    <meta name="description" content="Learn the Mistweaver Monk basics.">
    <meta property="og:title" content="Method Mistweaver Monk Guide - Introduction - Midnight 12.0.1">
    <link rel="canonical" href="https://www.method.gg/guides/mistweaver-monk">
  </head>
  <body>
    <nav>
      <ul class="guide-navigation">
        <li class="active"><a href="/guides/mistweaver-monk">Introduction</a></li>
        <li><a href="/guides/mistweaver-monk/talents">Talents</a></li>
      </ul>
    </nav>
    <div class="guides-titles">
      <span class="guide-author">Patch 12.0.1</span>
      <span class="guide-update-date"><strong>Last Updated: </strong>26th Feb, 2026</span>
    </div>
    <div class="guides-author-block">
      <span class="author-name">Tincell</span>
    </div>
    <article class="guide-main-content">
      <div class="guide-section-title"><h2>Introduction</h2></div>
      <p>Intro copy for Mistweaver Monk.</p>
      <h3>Mistweaver Monk Overview</h3>
      <p>Use <a href="https://www.wowhead.com/spell=116670/vivify">Vivify</a> well.</p>
      <p>Reference build: <a href="https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123">Raid Build</a>.</p>
    </article>
  </body>
</html>
"""

TALENTS_HTML = """
<html>
  <head>
    <title>Method Mistweaver Monk Guide - Talents - Midnight 12.0.1</title>
    <meta name="description" content="Learn the Mistweaver Monk talents.">
    <meta property="og:title" content="Method Mistweaver Monk Guide - Talents - Midnight 12.0.1">
    <link rel="canonical" href="https://www.method.gg/guides/mistweaver-monk/talents">
  </head>
  <body>
    <nav>
      <ul class="guide-navigation">
        <li><a href="/guides/mistweaver-monk">Introduction</a></li>
        <li class="active"><a href="/guides/mistweaver-monk/talents">Talents</a></li>
      </ul>
    </nav>
    <div class="guides-titles">
      <span class="guide-author">Patch 12.0.1</span>
      <span class="guide-update-date"><strong>Last Updated: </strong>26th Feb, 2026</span>
    </div>
    <div class="guides-author-block">
      <span class="author-name">Tincell</span>
    </div>
    <article class="guide-main-content">
      <div class="guide-section-title"><h2>Talents</h2></div>
      <p>Talent page copy.</p>
      <h3>Raid Talents</h3>
      <p>Pick <a href="https://www.wowhead.com/spell=388020/tea-of-serenity">Tea of Serenity</a>.</p>
      <p>Alternative build: <a href="https://www.wowhead.com/talent-calc/monk/mistweaver/DEF456">Mythic Plus Build</a>.</p>
    </article>
  </body>
</html>
"""

INTRO_HTML_WITH_FALLBACK_METADATA = """
<html>
  <head>
    <title>Method Mistweaver Monk Guide - Introduction - Midnight 12.0.1</title>
    <meta name="description" content="Learn the Mistweaver Monk basics.">
    <meta property="og:title" content="Method Mistweaver Monk Guide - Introduction - Midnight 12.0.1">
    <link rel="canonical" href="https://www.method.gg/guides/mistweaver-monk">
  </head>
  <body>
    <nav>
      <div class="guide-navigation">
        <a href="/guides/mistweaver-monk">Introduction</a>
        <a href="/guides/mistweaver-monk/talents">Talents</a>
      </div>
    </nav>
    <div class="guides-titles">
      <div class="guide-author">Patch 12.0.1</div>
      <div class="guide-update-date">Last Updated: 26th Feb, 2026</div>
    </div>
    <div class="author-name">Tincell</div>
    <div class="guide-main-content">
      <h2>Introduction</h2>
      <p>Intro copy for Mistweaver Monk.</p>
    </div>
  </body>
</html>
"""

SITEMAP_XML = """
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.method.gg/guides/mistweaver-monk</loc></url>
  <url><loc>https://www.method.gg/guides/mistweaver-monk/talents</loc></url>
  <url><loc>https://www.method.gg/guides/tier-list</loc></url>
  <url><loc>https://www.method.gg/guides/midnight-alchemy-profession-guide</loc></url>
  <url><loc>https://www.method.gg/guides/restoration-shaman</loc></url>
</urlset>
"""

PROFESSION_HTML = """
<html>
  <head>
    <title>Midnight Alchemy Profession Guide</title>
    <meta name="description" content="Alchemy guide.">
    <meta property="og:title" content="Midnight Alchemy Profession Guide">
    <link rel="canonical" href="https://www.method.gg/guides/midnight-alchemy-profession-guide">
  </head>
  <body>
    <div class="guide-header-text">
      <h1 class="guide--title">Midnight Alchemy Profession Guide</h1>
      <span class="author-name">Written by Roguery - 5th March 2026</span>
    </div>
    <article class="guide-main-content mount-guide-content">
      <p>Alchemy intro.</p>
      <h2>Leveling Midnight Alchemy</h2>
      <p>Use herbs.</p>
    </article>
  </body>
</html>
"""


def _fake_fetch_guide_page(guide_ref: str) -> dict[str, object]:
    if str(guide_ref).endswith("/talents"):
        return parse_guide_page(TALENTS_HTML, source_url="https://www.method.gg/guides/mistweaver-monk/talents")
    return parse_guide_page(INTRO_HTML, source_url="https://www.method.gg/guides/mistweaver-monk")


def _error_payload(result) -> dict[str, object]:
    raw = result.stderr or result.output
    return json.loads(raw)


def test_parse_sitemap_guides_filters_intro_pages() -> None:
    guides = parse_sitemap_guides(SITEMAP_XML)
    assert guides == [
        {"slug": "midnight-alchemy-profession-guide", "name": "Midnight Alchemy Profession Guide",
            "url": "https://www.method.gg/guides/midnight-alchemy-profession-guide"},
        {"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"},
        {"slug": "restoration-shaman", "name": "Restoration Shaman", "url": "https://www.method.gg/guides/restoration-shaman"},
        {"slug": "tier-list", "name": "Tier List", "url": "https://www.method.gg/guides/tier-list"},
    ]


def test_parse_guide_page_extracts_sections_navigation_and_links() -> None:
    payload = parse_guide_page(INTRO_HTML, source_url="https://www.method.gg/guides/mistweaver-monk")
    assert payload["guide"]["slug"] == "mistweaver-monk"
    assert payload["guide"]["section_slug"] == "introduction"
    assert payload["guide"]["author"] == "Tincell"
    assert payload["guide"]["patch"] == "Patch 12.0.1"
    assert payload["navigation"][0]["active"] is True
    assert payload["article"]["sections"][0]["title"] == "Introduction"
    assert payload["linked_entities"][0]["type"] == "spell"
    assert payload["linked_entities"][0]["id"] == 116670
    spell_identity = payload["linked_entities"][0]["ability_identity"]
    assert spell_identity["kind"] == "ability_identity"
    assert spell_identity["status"] == "canonical"
    assert spell_identity["identity"]["spell_id"] == 116670
    assert spell_identity["identity"]["normalized_name"] == "vivify"
    assert spell_identity["source"] == {"provider": "method", "source": "guide_linked_entity"}
    assert payload["build_references"][0]["build_code"] == "ABC123"
    assert payload["build_references"][0]["build_identity"]["class_spec_identity"]["identity"] == {
        "actor_class": "monk", "spec": "mistweaver"}


def test_linked_entities_ability_identity_only_on_spell_rows() -> None:
    html = """
    <html><body><article class="guide-main-content">
      <div class="guide-section-title"><h2>Gear</h2></div>
      <p>Use <a href="https://www.wowhead.com/spell=116670/vivify">Vivify</a> and
         equip <a href="https://www.wowhead.com/item=12345/trinket">Trinket</a>.</p>
    </article></body></html>
    """
    payload = parse_guide_page(html, source_url="https://www.method.gg/guides/mistweaver-monk")
    by_type = {row["type"]: row for row in payload["linked_entities"]}
    assert "ability_identity" in by_type["spell"]
    # Non-spell entity rows are unchanged (no ability_identity key).
    assert "ability_identity" not in by_type["item"]


def test_parse_guide_page_supports_metadata_and_article_fallback_selectors() -> None:
    payload = parse_guide_page(
        INTRO_HTML_WITH_FALLBACK_METADATA,
        source_url="https://www.method.gg/guides/mistweaver-monk",
    )
    assert payload["guide"]["author"] == "Tincell"
    assert payload["guide"]["patch"] == "Patch 12.0.1"
    assert payload["guide"]["last_updated"] == "Last Updated: 26th Feb, 2026"
    assert payload["navigation"][0]["title"] == "Introduction"
    assert payload["article"]["sections"][0]["title"] == "Introduction"


def test_parse_guide_page_normalizes_profession_author_and_family() -> None:
    payload = parse_guide_page(
        PROFESSION_HTML,
        source_url="https://www.method.gg/guides/midnight-alchemy-profession-guide",
    )
    assert payload["guide"]["author"] == "Roguery"
    assert payload["guide"]["last_updated"] == "5th March 2026"
    assert payload["guide"]["content_family"] == "profession_guide"
    assert payload["guide"]["supported_surface"] is True


def test_classify_guide_family_marks_unsupported_indexes() -> None:
    assert classify_guide_family("mistweaver-monk") == "class_guide"
    assert classify_guide_family("midnight-alchemy-profession-guide") == "profession_guide"
    assert classify_guide_family("tier-list") == "unsupported_index"
    assert classify_guide_family("world-of-warcraft") == "unsupported_index"


def test_method_search_command_uses_sitemap_guides(monkeypatch) -> None:
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["search", "mistweaver monk guide", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == "mistweaver-monk"
    assert payload["results"][0]["follow_up"]["recommended_command"] == "method guide mistweaver-monk"
    assert payload["results"][0]["metadata"]["content_family"] == "class_guide"


def test_method_resolve_command_returns_best_guide(monkeypatch) -> None:
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["resolve", "mistweaver monk"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["resolved"] is True
    assert payload["next_command"] == "method guide mistweaver-monk"


def test_method_search_excludes_unsupported_index_roots(monkeypatch) -> None:
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["search", "tier list"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["scope_hint"]["code"] == "tier_list"


def test_method_search_boosts_matching_content_family(monkeypatch) -> None:
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [
            {
                "slug": "midnight-alchemy-profession-guide",
                "name": "Midnight Alchemy Profession Guide",
                "url": "https://www.method.gg/guides/midnight-alchemy-profession-guide",
            },
            {
                "slug": "the-war-within-alchemy-leveling",
                "name": "The War Within Alchemy Leveling",
                "url": "https://www.method.gg/guides/the-war-within-alchemy-leveling",
            },
        ],
    )
    result = runner.invoke(app, ["search", "alchemy profession"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["results"][0]["id"] == "midnight-alchemy-profession-guide"
    assert "content_family_match" in payload["results"][0]["ranking"]["match_reasons"]


def test_method_guide_and_guide_full(monkeypatch) -> None:
    monkeypatch.setattr("method_cli.main.MethodClient.fetch_guide_page", lambda self, guide_ref: _fake_fetch_guide_page(guide_ref))
    guide_result = runner.invoke(app, ["guide", "mistweaver-monk"])
    assert guide_result.exit_code == 0
    guide_payload = json.loads(guide_result.stdout)["data"]
    assert guide_payload["guide"]["slug"] == "mistweaver-monk"
    assert guide_payload["linked_entities"]["count"] == 1
    assert guide_payload["build_references"]["count"] == 1
    assert guide_payload["analysis_surfaces"]["count"] == 1

    full_result = runner.invoke(app, ["guide-full", "mistweaver-monk"])
    assert full_result.exit_code == 0
    full_payload = json.loads(full_result.stdout)["data"]
    assert full_payload["guide"]["page_count"] == 2
    assert full_payload["linked_entities"]["count"] == 2
    assert full_payload["build_references"]["count"] == 2
    assert full_payload["analysis_surfaces"]["count"] == 2
    assert full_payload["pages"][1]["guide"]["section_slug"] == "talents"


def test_method_guide_export_and_query(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("method_cli.main.MethodClient.fetch_guide_page", lambda self, guide_ref: _fake_fetch_guide_page(guide_ref))
    export_dir = tmp_path / "guide-mistweaver-monk"

    export_result = runner.invoke(app, ["guide-export", "mistweaver-monk", "--out", str(export_dir)])
    assert export_result.exit_code == 0
    export_payload = json.loads(export_result.stdout)["data"]
    assert export_payload["counts"]["pages"] == 2
    assert (export_dir / "manifest.json").exists()
    manifest = json.loads((export_dir / "manifest.json").read_text())
    assert datetime.fromisoformat(manifest["exported_at"].replace("Z", "+00:00")).tzinfo is not None
    assert (export_dir / "pages" / "talents.html").exists()

    query_result = runner.invoke(app, ["guide-query", str(export_dir), "tea serenity", "--kind", "linked_entities"])
    assert query_result.exit_code == 0
    query_payload = json.loads(query_result.stdout)["data"]
    assert query_payload["count"] == 1
    assert query_payload["top"][0]["name"] == "Tea of Serenity"

    build_query = runner.invoke(app, ["guide-query", str(export_dir), "abc123", "--kind", "build_references"])
    assert build_query.exit_code == 0
    build_query_payload = json.loads(build_query.stdout)["data"]
    assert build_query_payload["count"] == 1
    assert build_query_payload["top"][0]["build_code"] == "ABC123"

    analysis_query = runner.invoke(app, ["guide-query", str(export_dir), "talent recommendations", "--kind", "analysis_surfaces"])
    assert analysis_query.exit_code == 0
    analysis_query_payload = json.loads(analysis_query.stdout)["data"]
    assert analysis_query_payload["count"] == 1
    assert analysis_query_payload["top"][0]["surface_tags"] == ["builds_talents", "talent_recommendations"]

    section_query = runner.invoke(app, ["guide-query", str(export_dir), "mistweaver",
                                  "--kind", "sections", "--section-title", "introduction"])
    assert section_query.exit_code == 0
    section_payload = json.loads(section_query.stdout)["data"]
    assert section_payload["match_counts"]["sections"] >= 1


def test_method_guide_query_answers_each_bad_bundle_path_the_way_icy_veins_does(tmp_path: Path) -> None:
    """One answer per mistake: missing target, wrong argument type, unreadable bundle."""
    empty_dir = tmp_path / "not-a-bundle"
    empty_dir.mkdir()
    file_path = tmp_path / "bundle.json"
    file_path.write_text("{}")

    answers = {}
    for label, path in (("missing", tmp_path / "gone"), ("directory", empty_dir), ("file", file_path)):
        result = runner.invoke(app, ["guide-query", str(path), "mana"])
        # A Typer-rejected argument prints its envelope before the provider handler runs.
        answers[label] = (result.exit_code, json.loads(result.stderr or result.stdout)["error"]["code"])

    assert answers == {
        "missing": (4, "not_found"),
        "directory": (1, "invalid_bundle"),
        "file": (2, "invalid_argument"),
    }


def test_guide_query_rejects_an_unknown_kind_with_the_code_icy_veins_uses(tmp_path: Path) -> None:
    from icy_veins_cli.main import app as icy_veins_app

    codes = set()
    for cli in (app, icy_veins_app):
        result = runner.invoke(cli, ["guide-query", str(tmp_path), "mana", "--kind", "bogus"])
        codes.add((result.exit_code, json.loads(result.stderr)["error"]["code"]))

    assert codes == {(1, "invalid_query_kind")}


def test_method_guide_invalid_ref_returns_structured_error() -> None:
    result = runner.invoke(app, ["guide", "https://www.method.gg/premium"])
    assert result.exit_code == 1

    payload = _error_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_guide_ref"


def test_method_guide_export_invalid_ref_returns_structured_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["guide-export", "https://www.method.gg/premium", "--out", str(tmp_path / "out")])
    assert result.exit_code == 1

    payload = _error_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_guide_ref"


def test_method_guide_unsupported_surface_returns_structured_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "method_cli.main.MethodClient.fetch_guide_page",
        lambda self, guide_ref: {
            "page": {"title": "Tier List", "description": "desc", "canonical_url": "https://www.method.gg/guides/tier-list/mythic-plus"},
            "guide": {
                "slug": "tier-list",
                "page_url": "https://www.method.gg/guides/tier-list/mythic-plus",
                "section_slug": "mythic-plus",
                "section_title": "Mythic Plus",
                "author": None,
                "last_updated": None,
                "patch": None,
                "content_family": "unsupported_index",
                "supported_surface": False,
            },
            "navigation": [],
            "article": {"html": "", "text": "", "headings": [], "sections": []},
            "linked_entities": [],
        },
    )
    result = runner.invoke(app, ["guide", "tier-list"])
    assert result.exit_code == 1

    payload = _error_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unsupported_guide_surface"


def _connect_error(*_args, **_kwargs):
    raise httpx.ConnectError("offline", request=httpx.Request("GET", "https://www.method.gg/sitemap.xml"))


def _not_found_error(*_args, **_kwargs):
    request = httpx.Request("GET", "https://www.method.gg/guides/mistweaver-monk")
    raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))


@pytest.mark.parametrize("args", [["search", "mistweaver monk"], ["resolve", "mistweaver monk"], ["guide", "mistweaver-monk"]])
def test_method_transport_failure_returns_network_envelope(monkeypatch, args: list[str]) -> None:
    monkeypatch.setattr("method_cli.client.request_with_retries", _connect_error)
    result = runner.invoke(app, args)

    assert result.exit_code == 5, result.output
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["provider"] == "method"
    assert payload["command"] == args[0]
    assert payload["schema_version"] == "1"
    assert payload["error"]["code"] == "network_error"
    assert not isinstance(result.exception, httpx.HTTPError)


def test_method_guide_upstream_404_returns_not_found_envelope(monkeypatch) -> None:
    monkeypatch.setattr("method_cli.client.request_with_retries", _not_found_error)
    result = runner.invoke(app, ["guide", "mistweaver-monk"])

    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["details"]["status_code"] == 404


@pytest.mark.parametrize(
    ("args", "kind"),
    [
        (["doctor"], "doctor"),
        (["search", "mistweaver monk"], "search_results"),
        (["resolve", "mistweaver monk"], "resolve_match"),
        (["guide", "mistweaver-monk"], "guide"),
        (["guide-full", "mistweaver-monk"], "guide_full"),
    ],
)
def test_method_commands_emit_conforming_envelope(monkeypatch, args: list[str], kind: str) -> None:
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    monkeypatch.setattr("method_cli.main.MethodClient.fetch_guide_page", lambda self, guide_ref: _fake_fetch_guide_page(guide_ref))
    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert envelope_violations(payload) == []
    assert payload["provider"] == "method"
    assert payload["command"] == args[0]
    assert payload["kind"] == kind
    assert payload["schema_version"] == "1"


def test_method_search_data_block_mirrors_legacy_top_level_keys(monkeypatch) -> None:
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    result = runner.invoke(app, ["search", "mistweaver monk guide"])

    payload = json.loads(result.stdout)
    assert payload["data"]["results"] == payload["results"]
    assert payload["data"]["count"] == payload["count"] == 1


def test_method_provider_surface_is_callable_in_process(monkeypatch) -> None:
    monkeypatch.setattr("method_cli.provider.MethodClient.sitemap_guides", lambda self: parse_sitemap_guides(SITEMAP_XML))
    envelope = PROVIDER.search("mistweaver monk guide", limit=5)

    assert PROVIDER.name == "method"
    assert envelope_violations(envelope) == []
    assert envelope["data"]["results"][0]["id"] == "mistweaver-monk"


INTRO_HTML_WITH_NON_GUIDE_NAV_LINK = """
<html>
  <head>
    <link rel="canonical" href="https://www.method.gg/guides/mistweaver-monk">
  </head>
  <body>
    <nav>
      <ul class="guide-navigation">
        <li><a href="/guides">All guides</a></li>
        <li class="active"><a href="/guides/mistweaver-monk">Introduction</a></li>
      </ul>
    </nav>
    <article class="guide-main-content">
      <h2>Introduction</h2>
      <p>Intro copy for Mistweaver Monk.</p>
    </article>
  </body>
</html>
"""

def _unrecognised_layout_html(path: str) -> str:
    """A page whose prose lives in a container the parser does not know: Method template drift."""
    return f"""
<html>
  <head>
    <link rel="canonical" href="https://www.method.gg/guides/{path}">
  </head>
  <body>
    <div class="some-new-wrapper">
      <h2>Introduction</h2>
      <p>Real prose that the parser cannot see.</p>
    </div>
  </body>
</html>
"""


UNRECOGNISED_LAYOUT_HTML = _unrecognised_layout_html("mistweaver-monk")


def test_method_guide_survives_a_non_guide_link_in_the_page_navigation(monkeypatch) -> None:
    """An 'All guides' link in Method's own nav must not make a valid slug look invalid."""
    monkeypatch.setattr(
        "method_cli.main.MethodClient.fetch_guide_page",
        lambda self, guide_ref: parse_guide_page(
            INTRO_HTML_WITH_NON_GUIDE_NAV_LINK,
            source_url="https://www.method.gg/guides/mistweaver-monk",
        ),
    )
    result = runner.invoke(app, ["guide", "mistweaver-monk"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert [row["section_slug"] for row in payload["navigation"]["items"]] == ["introduction"]


def test_method_guide_fails_when_the_article_container_is_missing(monkeypatch) -> None:
    """Layout drift must be an error, not ok:true with an empty article."""
    monkeypatch.setattr(
        "method_cli.main.MethodClient.fetch_guide_page",
        lambda self, guide_ref: parse_guide_page(
            UNRECOGNISED_LAYOUT_HTML,
            source_url="https://www.method.gg/guides/mistweaver-monk",
        ),
    )
    result = runner.invoke(app, ["guide", "mistweaver-monk"])

    assert result.exit_code == 1
    payload = _error_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "parse_failed"
    assert payload["error"]["details"]["page_url"] == "https://www.method.gg/guides/mistweaver-monk"


def test_method_guide_page_parse_failure_is_not_blamed_on_the_argument(monkeypatch) -> None:
    """A valid slug whose page will not parse is a `parse_failed`, not an `invalid_guide_ref`."""

    def fetch(self, guide_ref: str) -> dict[str, object]:
        raise ValueError("Failed to clone Method article node.")

    monkeypatch.setattr("method_cli.main.MethodClient.fetch_guide_page", fetch)
    result = runner.invoke(app, ["guide", "mistweaver-monk"])

    assert result.exit_code == 1
    payload = _error_payload(result)
    assert payload["error"]["code"] == "parse_failed"
    assert "mistweaver-monk" in payload["error"]["message"]


BROKEN_NAVIGATION_PAGE_CASES = [
    ("network", "network_error"),
    ("unparsable", "parse_failed"),
    ("empty_article", "parse_failed"),
]


def _navigation_page_fetch(failure: str):
    """Fetch stub where the /talents sibling fails in ``failure`` mode and every other page is fine."""

    def fetch(self, guide_ref: str) -> dict[str, object]:
        if not str(guide_ref).endswith("/talents"):
            return _fake_fetch_guide_page(guide_ref)
        if failure == "network":
            raise httpx.ConnectError("boom", request=httpx.Request("GET", str(guide_ref)))
        if failure == "unparsable":
            raise ValueError("Failed to clone Method article node.")
        return parse_guide_page(
            _unrecognised_layout_html("mistweaver-monk/talents"),
            source_url="https://www.method.gg/guides/mistweaver-monk/talents",
        )

    return fetch


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    BROKEN_NAVIGATION_PAGE_CASES,
    ids=[case[0] for case in BROKEN_NAVIGATION_PAGE_CASES],
)
def test_method_guide_full_records_a_failed_navigation_page_and_keeps_going(monkeypatch, failure: str, expected_code: str) -> None:
    monkeypatch.setattr("method_cli.main.MethodClient.fetch_guide_page", _navigation_page_fetch(failure))
    result = runner.invoke(app, ["guide-full", "mistweaver-monk"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["guide"]["page_count"] == 1
    assert payload["failed_pages"]["count"] == 1
    failed = payload["failed_pages"]["items"][0]
    assert failed["section_slug"] == "talents"
    assert failed["error"]["code"] == expected_code


def test_method_guide_export_reports_failed_navigation_pages(monkeypatch, tmp_path: Path) -> None:
    """An exported bundle is partial when a navigation page failed; the command must say so."""
    monkeypatch.setattr("method_cli.main.MethodClient.fetch_guide_page", _navigation_page_fetch("network"))
    bundle_dir = tmp_path / "bundle"
    result = runner.invoke(app, ["guide-export", "mistweaver-monk", "--out", str(bundle_dir)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)["data"]
    assert payload["counts"]["pages"] == 1
    assert payload["failed_pages"]["count"] == 1
    assert payload["failed_pages"]["items"][0]["section_slug"] == "talents"
    # The manifest is all a downstream bundle reader sees, so the missing page has to reach it too.
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    assert manifest["failed_pages"]["count"] == 1
    assert manifest["failed_pages"]["items"][0]["section_slug"] == "talents"


TALENT_BLOCK_HTML = """
<html>
  <head>
    <link rel="canonical" href="https://www.method.gg/guides/mistweaver-monk/talents">
  </head>
  <body>
    <article class="guide-main-content">
      <h2>Talent Builds</h2>
      <div class="df-talent-block">
        <div class="talent-title">Raid (Conduit of the Celestials)</div>
        <div class="talent-embed" data-talent="C4QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB"></div>
      </div>
      <div class="df-talent-block">
        <div class="talent-title">Mythic+ (Conduit of the Celestials)</div>
        <div class="talent-embed" data-talent="C4QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB"></div>
      </div>
      <div class="df-talent-block">
        <div class="talent-title">Placeholder</div>
        <div class="talent-embed" data-talent="coming soon"></div>
      </div>
    </article>
  </body>
</html>
"""


def _talent_block_builds() -> list[dict[str, object]]:
    payload = parse_guide_page(TALENT_BLOCK_HTML, source_url="https://www.method.gg/guides/mistweaver-monk/talents")
    return payload["build_references"]


def test_method_ignores_talent_blocks_that_hold_no_loadout_import_string() -> None:
    """Placeholder embeds share the talent-block markup; only real import strings are builds."""
    assert [row["build_code"] for row in _talent_block_builds()] == ["C4QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB"]


def test_method_keeps_the_first_label_when_one_import_string_is_published_twice() -> None:
    """Two builds can share a loadout string; the first published name wins so output is stable."""
    assert [row["label"] for row in _talent_block_builds()] == ["Raid (Conduit of the Celestials)"]
