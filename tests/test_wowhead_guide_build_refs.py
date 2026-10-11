"""Explicit guide builds survive extraction and an exported-bundle handoff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner
from warcraft_content.article_bundle import load_article_bundle, query_article_bundle
from wowhead_cli.guide_builds import guide_build_references
from wowhead_cli.main import app

from tests.wowhead_testkit import SAMPLE_GUIDE_HTML

SOURCE = "https://www.wowhead.com/guide/classes/druid/balance/talent-builds-pve-dps"


def test_explicit_calculator_links_preserve_identity_and_deduplicate() -> None:
    url = "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    block = guide_build_references(
        f'<div id="guide-body"><a href="{url}">Raid</a></div>',
        f"[url={url}]Raid[/url]", source_url=SOURCE, expansion="retail",
    )
    assert block["count"] == 1 and block["excluded_count"] == 0
    row = block["items"][0]
    assert (row["url"], row["source_url"], row["build_code"]) == (url, SOURCE, "ABC123")
    assert (row["build_identity"]["class_spec_identity"]["identity"]["actor_class"], row["build_identity"]["class_spec_identity"]["identity"]["spec"]) == ("druid", "balance")


@pytest.mark.parametrize(
    ("reference", "reason"),
    [("druid/balance", "explicit spec and build"),
     ("druid/balance/C0QAAAAAAAAAAAAAAAAAAAA", "monk/windwalker"),
     ("druid/made-up/ABC123", "spec")],
)
def test_unusable_calculator_links_remain_disclosed(reference: str, reason: str) -> None:
    block = guide_build_references(
        "", f"[url=https://www.wowhead.com/talent-calc/{reference}]Build[/url]",
        source_url=SOURCE, expansion="retail",
    )
    assert block["count"] == 0 and block["excluded_count"] == 1
    assert reason in block["excluded_references"][0]["reason"].lower()


def test_builds_in_navigation_and_comments_are_not_guide_recommendations() -> None:
    url = "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    block = guide_build_references(
        f'<nav><a href="{url}">Sidebar</a></nav><div class="comment"><a href="{url}">Comment</a></div>',
        "A guide that only discusses talent choices.", source_url=SOURCE, expansion="retail",
    )
    assert block["items"] == []


def test_explicit_published_loadouts_keep_the_original_calculator_reference() -> None:
    code = "CYG" + "A" * 80
    url = f"https://www.wowhead.com/talent-calc/blizzard/{code}"
    block = guide_build_references("", f"[url={url}]Raid[/url]", source_url=SOURCE, expansion="retail")
    assert block["count"] == 1
    row = block["items"][0]
    assert (row["reference_type"], row["build_code"], row["url"]) == ("wow_talent_export", code, code)
    assert row["build_identity"]["class_spec_identity"]["identity"]["actor_class"] is None
    assert row["source"]["original_ref"] == url


@pytest.mark.parametrize("encoded", [False, True])
def test_native_loadout_links_preserve_base64_slashes_and_plus(encoded: bool) -> None:
    code = "CYG" + "A" * 40 + "/+" + "B" * 40
    path_code = code.replace("/", "%2F").replace("+", "%2B") if encoded else code
    url = f"https://www.wowhead.com/talent-calc/blizzard/{path_code}"
    block = guide_build_references("", f"[url={url}]Raid[/url]", source_url=SOURCE, expansion="retail")
    assert block["count"] == 1 and block["excluded_count"] == 0
    row = block["items"][0]
    assert row["build_code"] == code
    assert row["source"]["original_ref"] == url
    assert row["citations"][0]["url"] == url


def test_published_markup_builds_are_explicit_and_keep_unknown_identity() -> None:
    code = "CYG" + "A" * 80
    block = guide_build_references(
        "", f'[build title="Raid" talents="{code}" stats="Haste"]', source_url=SOURCE, expansion="retail",
    )
    row = block["items"][0]
    assert row["label"] == "Raid" and row["build_code"] == code
    assert row["build_identity"]["confidence"] == "none"


def test_captured_build_tag_calculator_fields_are_reusable_loadouts() -> None:
    fixture = Path(__file__).parent / "fixtures" / "wowhead" / "guide_3087_build_tags.json"
    captured = json.loads(fixture.read_text())
    markup = "\n".join(captured["build_opening_tags"])
    block = guide_build_references("", markup, source_url=captured["source_url"], expansion="retail")
    assert block["count"] == 2 and block["excluded_count"] == 0
    for row in block["items"]:
        assert row["reference_type"] == "wow_talent_export"
        assert row["source"]["original_field"] == "talents"
        assert row["source"]["original_field_value"] == f"blizzard/{row['build_code']}"
        assert row["source"]["original_ref"] == row["source"]["original_field_value"]
        assert row["build_identity"]["confidence"] == "none"
        assert {citation["url"] for citation in row["citations"]} == {
            captured["source_url"], f"https://www.wowhead.com/talent-calc/blizzard/{row['build_code']}",
        }


@pytest.mark.parametrize("expansion", ["retail", "ptr", "beta"])
def test_explicit_build_tag_loadouts_follow_the_retail_calculator(expansion: str) -> None:
    code = "CYG" + "A" * 80
    expected_prefix = "" if expansion == "retail" else f"/{expansion}"
    source = f"https://www.wowhead.com{expected_prefix}/guide/synthetic"
    block = guide_build_references("", f'[build title="Raid" talents="blizzard/{code}"]', source_url=source, expansion=expansion)
    assert block["count"] == 1
    assert block["items"][0]["source"]["reference_url"] == f"https://www.wowhead.com{expected_prefix}/talent-calc/blizzard/{code}"


@pytest.mark.parametrize("talents", ["blizzard/" + "A" * 80, "0" * 80])
def test_classic_build_fields_are_not_reinterpreted_as_retail_exports(talents: str) -> None:
    block = guide_build_references("", f'[build talents="{talents}"]', source_url="https://www.wowhead.com/classic/guide/synthetic", expansion="classic")
    assert block["count"] == 0 and block["excluded_count"] == 1
    assert "classic hashes are not reinterpreted" in block["excluded_references"][0]["reason"]


def test_malformed_explicit_blizzard_build_field_is_disclosed() -> None:
    block = guide_build_references("", '[build talents="blizzard/not-a-loadout"]', source_url=SOURCE, expansion="retail")
    assert block["count"] == 0 and block["excluded_count"] == 1
    assert block["excluded_references"][0]["url"] == "blizzard/not-a-loadout"


def test_duplicate_build_block_and_calculator_link_keep_both_sources() -> None:
    code = "CYG" + "A" * 80
    url = f"https://www.wowhead.com/talent-calc/blizzard/{code}"
    block = guide_build_references(
        f'<div id="guide-body"><a href="{url}">Calculator</a></div>',
        f'[build title="Raid" talents="{code}"] [url={url}]Calculator[/url]',
        source_url=SOURCE, expansion="retail",
    )
    assert block["count"] == 1
    row = block["items"][0]
    assert row["label"] == "Raid"
    assert row["source"]["original_ref"] == url
    assert row["source"]["reference_url"] == url
    assert {citation["url"] for citation in row["citations"]} == {SOURCE, url}


def test_exported_wowhead_builds_are_queryable_by_the_shared_bundle_reader(monkeypatch, tmp_path: Path) -> None:
    html = SAMPLE_GUIDE_HTML.replace(
        '"[h2 toc=', '"[url=https://www.wowhead.com/talent-calc/death-knight/frost/ABC123]Raid[/url] [h2 toc=',
        1,
    )
    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient.guide_page_html", lambda self, guide_id: html)
    out = tmp_path / "bundle"
    result = CliRunner().invoke(app, ["guide-export", "3143", "--out", str(out)])
    assert result.exit_code == 0, result.stdout + result.stderr
    manifest = json.loads(result.stdout)["data"]
    assert manifest["counts"]["build_references"] == 1
    assert manifest["files"]["build_references_jsonl"] == "build-references.jsonl"
    bundle = load_article_bundle(out)
    matches = query_article_bundle(bundle, query="Raid", limit=5, kinds={"build_references"}, section_title_filter=None)
    assert matches["match_counts"]["build_references"] == 1
    assert matches["matches"]["build_references"][0]["source_url"].startswith("https://www.wowhead.com/guide/")
