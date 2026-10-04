from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from icy_veins_cli.client import IcyVeinsClient
from icy_veins_cli.page_parser import parse_guide_page
from icy_veins_cli.talent_calculator import (
    TREE_DATA_URL,
    ConversionError,
    convert_calculator_builds,
    same_talents,
    talent_export_string,
)

from tests.article_provider_testkit import load_fixture_text

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "icy_veins"
WARRIOR_TREE_URL = TREE_DATA_URL.format(class_slug="warrior")
DEMON_HUNTER_TREE_URL = TREE_DATA_URL.format(class_slug="demon_hunter")
ARMS_PVP_URL = "https://www.icy-veins.com/wow/arms-warrior-pvp-talents-and-builds"
# The Slayer build on the captured Arms PvP page, and the string the live calculator's own export
# button gave for it (2026-10-04); simc decodes it to the same Arms talents.
ARMS_SLAYER_HASH = "HB-DJTGheCKroW4cSoRiLTSKKqYECC0ED77CAA-CDGKCIAQSYWCD9qqcekAl9RJC2qZqzE-AAAA-PCYoMgAQsRYBzC-"
ARMS_SLAYER_EXPORT = (
    "CcEAAAAAAAAAAAAAAAAAAAAAAgZmZmFzYmZGAAAghphZGGLMzMzYGzMDAAAAgxyMDMhxy2AbgBMDTgZwGwMWMLzglZ2GgZGAmZYA"
)
# Captured from devourer-demon-hunter-pve-dps-easy-mode (2026-10-04): the single-target build's
# calculator hash, the page's import string for it, and the page's AoE import string. The page's
# string is the game's own export, which marks the unchosen hero tree's root as granted; the
# calculator's export leaves it unselected. Both load the same talents.
DEVOURER_ST_HASH = "IX-NOQUfEKehmFIIJLklAPOYyyAHTFMmmsu20C-AHKXGMSsIUw48MVVllpFEs0IJx5JSAB-AAAA-UWYaKMiMJQYgAA-"
DEVOURER_ST_PUBLISHED = (
    "CgcBAAAAAAAAAAAAAAAAAAAAAAA2MmZmZmZmxwMAAAAAAAegxsNYGAAAAAAAAmxMMzMzMjZmZmZmFzYsolFmZmZ2abmZGAzYAIgxghB"
)
DEVOURER_ST_CALCULATOR_EXPORT = (
    "CgcBAAAAAAAAAAAAAAAAAAAAAAA2MmZmZmZmxwMAAAAAAAegxsNYGAAAAAAAAmxMMzMzMjZmZmZmFzYsolFmZmZ2abmZGAzYAAwYwwA"
)
DEVOURER_AOE_PUBLISHED = (
    "CgcBAAAAAAAAAAAAAAAAAAAAAAA2MmZmZmZmxwMAAAAAAAegxsNYGAAAAAAAAmxMMzMzMzMzMDzsYGjFtswMzMzWbzMzAYGDABMGMmB"
)


def _tree(name: str) -> dict[str, Any]:
    return json.loads(load_fixture_text(FIXTURE_DIR, f"talent_tree_{name}.json"))


TREES = {WARRIOR_TREE_URL: "warrior", DEMON_HUNTER_TREE_URL: "demon_hunter"}


def _load_tree(url: str) -> Mapping[str, Any]:
    return _tree(TREES[url])


def _calculator_row(code: str, *, label: str | None = "Build", source_url: str = ARMS_PVP_URL) -> dict[str, Any]:
    return {
        "kind": "build_reference",
        "reference_type": "icy_veins_talent_calc_url",
        "url": f"https://www.icy-veins.com/wow/midnight-talent-calculator#{code}",
        "label": label,
        "build_code": code,
        "source_url": source_url,
        "build_identity": {},
        "source": {"provider": "icy-veins", "source": "guide_talent_calculator_embed"},
    }


def test_converts_a_calculator_hash_to_the_string_the_calculators_export_button_gives() -> None:
    assert talent_export_string(ARMS_SLAYER_HASH, _tree("warrior")) == ARMS_SLAYER_EXPORT
    assert talent_export_string(DEVOURER_ST_HASH, _tree("demon_hunter")) == DEVOURER_ST_CALCULATOR_EXPORT


def test_pvp_page_keeps_the_calculator_url_next_to_its_converted_import_string() -> None:
    page = parse_guide_page(load_fixture_text(FIXTURE_DIR, "astro_pvp_talents_and_builds.html"), source_url=ARMS_PVP_URL)

    calculator, converted = convert_calculator_builds(page["build_references"], load_tree=_load_tree, keep_calculator_urls=True)

    assert calculator["reference_type"] == "icy_veins_talent_calc_url"
    assert calculator["conversion"] == {"status": "converted", "wow_talent_export": ARMS_SLAYER_EXPORT, "tree_data_url": WARRIOR_TREE_URL}
    assert converted["reference_type"] == "wow_talent_export"
    assert converted["url"] == converted["build_code"] == ARMS_SLAYER_EXPORT
    assert converted["label"] == "Slayer - Arms Warrior"
    assert converted["source_url"] == ARMS_PVP_URL
    assert converted["source"]["source"] == "guide_talent_calculator_conversion"
    assert converted["source"]["converted_from"] == calculator["url"]
    assert converted["source"]["tree_data_url"] == WARRIOR_TREE_URL
    assert converted["build_identity"]["class_spec_identity"]["identity"] == {"actor_class": "warrior", "spec": "arms"}


def test_a_build_the_page_publishes_adds_no_row_even_when_the_strings_differ() -> None:
    """The game's export and the calculator's differ in a granted bit; they buy the same talents."""
    tree = _tree("demon_hunter")
    assert same_talents(DEVOURER_ST_PUBLISHED, DEVOURER_ST_CALCULATOR_EXPORT, tree)
    assert not same_talents(DEVOURER_AOE_PUBLISHED, DEVOURER_ST_CALCULATOR_EXPORT, tree)

    published = {"reference_type": "wow_talent_export", "url": DEVOURER_ST_PUBLISHED, "build_code": DEVOURER_ST_PUBLISHED}
    rows = [published, _calculator_row(DEVOURER_ST_HASH)]

    assert convert_calculator_builds(rows, load_tree=_load_tree, keep_calculator_urls=False) == [published]
    _, calculator = convert_calculator_builds(rows, load_tree=_load_tree, keep_calculator_urls=True)
    assert calculator["conversion"]["wow_talent_export"] == DEVOURER_ST_PUBLISHED


def test_builds_that_differ_only_in_pvp_talents_share_one_import_string() -> None:
    """A WoW import string carries no PvP talents, so two such tabs convert to one row named by both."""
    rows = [_calculator_row(ARMS_SLAYER_HASH, label="Best 3v3 Build"), _calculator_row(ARMS_SLAYER_HASH + "BAA", label="Best 2v2 Build")]

    first, converted, second = convert_calculator_builds(rows, load_tree=_load_tree, keep_calculator_urls=True)

    assert first["conversion"]["wow_talent_export"] == second["conversion"]["wow_talent_export"] == ARMS_SLAYER_EXPORT
    assert (converted["url"], converted["label"]) == (ARMS_SLAYER_EXPORT, "Best 3v3 Build / Best 2v2 Build")


def test_a_pve_build_whose_hash_names_pvp_talents_keeps_its_calculator_url() -> None:
    """The import string, published or converted, has no PvP talents; only the calculator URL keeps them."""
    published = {"reference_type": "wow_talent_export", "url": DEVOURER_ST_PUBLISHED, "build_code": DEVOURER_ST_PUBLISHED}
    with_pvp = _calculator_row(DEVOURER_ST_HASH + "BEI")

    kept_published, calculator = convert_calculator_builds([published, with_pvp], load_tree=_load_tree, keep_calculator_urls=False)

    assert kept_published == published
    assert (calculator["url"], calculator["conversion"]["wow_talent_export"]) == (with_pvp["url"], DEVOURER_ST_PUBLISHED)
    # A PvP part of zeros picks no PvP talent, so that build is still just its import string.
    (converted,) = convert_calculator_builds([_calculator_row(ARMS_SLAYER_HASH + "AAA")], load_tree=_load_tree, keep_calculator_urls=False)
    assert converted["url"] == ARMS_SLAYER_EXPORT


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("HB-AAAA", "has 2 parts"),
        ("HB--z--", "names node index 51, past the 35 nodes of that tree"),
        ("HB--CC--", "spends 2 ranks on Mortal Strike, which has 1"),
        ("HB--A--", "takes Fervor of Battle without a fully ranked node leading to it"),
    ],
)
def test_a_hash_that_does_not_spell_out_a_loadable_build_keeps_its_url_and_says_why(code: str, reason: str) -> None:
    with pytest.raises(ConversionError, match=reason):
        talent_export_string(code, _tree("warrior"))

    (row,) = convert_calculator_builds([_calculator_row(code)], load_tree=_load_tree, keep_calculator_urls=False)

    assert row["reference_type"] == "icy_veins_talent_calc_url"
    assert row["conversion"]["status"] == "failed"
    assert reason in row["conversion"]["reason"]


def _serve(responses: dict[str, str | Exception], requested: list[str]):
    def request(_client: httpx.Client, url: str, **_kwargs: Any) -> httpx.Response:
        requested.append(url)
        body = responses[url]
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))

    return request


PVE_PAGE_URL = "https://www.icy-veins.com/wow/arms-warrior-pve-dps-easy-mode"
PVE_PAGE = (
    '<html><head><link rel="canonical" href="{url}"></head><body><div class="guide-page-content">'
    '<h2>Raid Talents</h2><div id="midnight-skill-builder-1"></div>'
    '<script>const args = ["midnight-skill-builder-1", "#{code}"]; new MidnightTalentCalculator(...args);</script>'
    "</div></body></html>"
)


def test_the_client_converts_page_builds_and_reads_the_tree_data_once(monkeypatch) -> None:
    """Synthetic easy-mode page with one calculator build."""
    requested: list[str] = []
    other_url = PVE_PAGE_URL.replace("easy-mode", "mythic-plus-tips")
    responses: dict[str, str | Exception] = {
        PVE_PAGE_URL: PVE_PAGE.format(url=PVE_PAGE_URL, code=ARMS_SLAYER_HASH),
        other_url: PVE_PAGE.format(url=other_url, code=ARMS_SLAYER_HASH),
        WARRIOR_TREE_URL: load_fixture_text(FIXTURE_DIR, "talent_tree_warrior.json"),
    }
    monkeypatch.setattr("icy_veins_cli.client.request_with_retries", _serve(responses, requested))

    with IcyVeinsClient() as client:
        (row,) = client.fetch_guide_page("arms-warrior-pve-dps-easy-mode")["build_references"]
        client.fetch_guide_page("arms-warrior-pve-dps-mythic-plus-tips")

    assert (row["reference_type"], row["url"], row["label"]) == ("wow_talent_export", ARMS_SLAYER_EXPORT, "Raid Talents")
    assert requested.count(WARRIOR_TREE_URL) == 1


@pytest.mark.parametrize(
    ("tree_response", "reason"),
    [
        (httpx.ConnectError("connection refused"), "could not read the calculator's tree data"),
        ("<html>Attention Required! | Cloudflare</html>", "is not the JSON tree it used to be"),
    ],
)
def test_unreadable_tree_data_keeps_the_calculator_url_and_the_page(monkeypatch, tree_response: str | Exception, reason: str) -> None:
    """The failed read is not repeated for the next build, so a hanging static host costs one retry cycle."""
    requested: list[str] = []
    other_url = PVE_PAGE_URL.replace("easy-mode", "mythic-plus-tips")
    responses = {
        PVE_PAGE_URL: PVE_PAGE.format(url=PVE_PAGE_URL, code=ARMS_SLAYER_HASH),
        other_url: PVE_PAGE.format(url=other_url, code=ARMS_SLAYER_HASH),
        WARRIOR_TREE_URL: tree_response,
    }
    monkeypatch.setattr("icy_veins_cli.client.request_with_retries", _serve(responses, requested))

    with IcyVeinsClient() as client:
        (row,) = client.fetch_guide_page("arms-warrior-pve-dps-easy-mode")["build_references"]
        (other,) = client.fetch_guide_page("arms-warrior-pve-dps-mythic-plus-tips")["build_references"]

    for page_row in (row, other):
        assert page_row["reference_type"] == "icy_veins_talent_calc_url"
        assert page_row["conversion"]["status"] == "failed"
        assert reason in page_row["conversion"]["reason"]
    assert requested.count(WARRIOR_TREE_URL) == 1
