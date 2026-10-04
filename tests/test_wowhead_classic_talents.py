"""Classic-era Wowhead talent calculator builds decoded into trees, talents and ranks.

``tests/fixtures/wowhead/talent_data_<calculator>.js`` are captured Wowhead talent data files
(``nether.wowhead.com/<prefix>/data/talents-classic``), trimmed to the classes these tests decode:
the ``WH.setPageData`` object keeps only those classes' ``talents`` and ``trees`` (and
``hashVersion``), and ``WH.Gatherer.addData`` keeps only the entries for those talents' first-rank
spells; kept entries are unchanged. Every expected value below is what Wowhead's own calculator
rendered for the build (tree totals, talents and ranks). The builds come from Wowhead's guides,
except the WoW Forever one, made in its calculator, and the packed TBC and Wrath codes, which pack
guide builds and were then loaded in Wowhead's calculator.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from wowhead_cli.classic_talents import compact_talent_data, decode_build, talent_data_url
from wowhead_cli.main import app
from wowhead_cli.wowhead_client import WowheadClient

from tests.wowhead_testkit import captured_page, runner

CLASSIC_DATA_URL = "https://nether.wowhead.com/classic/data/talents-classic?dv=29&db=1761625051"
# Synthetic: the one line of a classic calculator page the decoder reads, the data file it loads.
CLASSIC_CALC_HTML = f"""
<html><head>
<title>Warrior WoW Classic (SoD) Talent Calculator - Classic World of Warcraft</title>
<link rel="canonical" href="https://www.wowhead.com/classic/talent-calc">
</head><body>
<script src="{CLASSIC_DATA_URL.replace("&", "&amp;")}"></script>
</body></html>
"""


def _data(calculator: str) -> dict[str, Any]:
    return compact_talent_data(captured_page(f"talent_data_{calculator}.js"))


def _decode(calculator: str, actor_class: str, code: str) -> dict[str, Any]:
    return decode_build(_data(calculator), calculator=calculator, actor_class=actor_class, build_code=code)


@pytest.mark.parametrize(
    ("calculator", "actor_class", "code", "points_by_tree", "last_talents"),
    [
        ("classic", "warrior", "30305001302-05050005525010051", "17/34/0", ["Impale", "Bloodthirst", None]),
        # Season of Discovery: the runes ride after ``_`` and leave the points alone.
        (
            "classic",
            "hunter",
            "550000004-05451005503051-03_116tj57em66qa76tk86qf96y3a6qkb7avc7arf732",
            "14/34/3",
            ["Unleashed Fury", "Trueshot Aura", "Humanoid Slaying"],
        ),
        # The calculator's packed form; its base64 alphabet holds the ``_`` that otherwise starts a suffix.
        ("classic", "hunter", "A_AAzAQ40zc0AA", "20/31/0", ["Ferocity", "Trueshot Aura", None]),
        ("tbc", "mage", "2-5052120123033310531251-053002001", "2/48/11", ["Arcane Subtlety", "Dragon's Breath", "Icy Veins"]),
        ("tbc", "mage", "AcAGz3Hz9PfQAzwwQA", "2/48/11", ["Arcane Subtlety", "Dragon's Breath", "Icy Veins"]),
        ("wotlk", "deathknight", "Ag9ECMzM8zEw_R9AAQw", "12/54/5", ["Rune Tap", "Howling Blast", "Anticipation"]),
        (
            "wotlk",
            "deathknight",
            "0055101-30505050350203010300233101351-005_001xv611s8q31ts841sxd51s8g",
            "12/54/5",
            ["Rune Tap", "Howling Blast", "Anticipation"],
        ),
        # Cataclysm's Arcane tree holds an unnamed talent sharing a cell; it still takes a digit.
        (
            "cata",
            "mage",
            "003-230330221120121213231-03_001q1g11q2021q1y31q1w41q1d51q1n61rj571rj881rj4",
            "3/35/3",
            ["Netherwind Presence", "Living Bomb", "Piercing Ice"],
        ),
        ("forever", "warrior", "v2252202-5321011-2541230211002001_t0", "13/13/24", ["Improved Overpower", "Blood Craze", "Focused Rage"]),
        ("classic-ptr", "warrior", "30305001302-05050005525010051", "17/34/0", ["Impale", "Bloodthirst", None]),
    ],
)
def test_a_classic_build_decodes_to_the_calculators_tree_totals(
    calculator: str, actor_class: str, code: str, points_by_tree: str, last_talents: list[str | None]
) -> None:
    decoded = _decode(calculator, actor_class, code)
    assert decoded["decoded"] is True, decoded
    assert decoded["points_by_tree"] == points_by_tree
    assert [tree["talents"][-1]["name"] if tree["talents"] else None for tree in decoded["trees"]] == last_talents


def test_a_classic_build_lists_each_trees_talents_with_rank_and_spell() -> None:
    decoded = _decode("classic", "warrior", "30305001302-05050005525010051")
    arms = decoded["trees"][0]
    assert (arms["tree_id"], arms["name"], arms["points"]) == (161, "Arms", 17)
    assert [(row["name"], row["rank"], row["max_rank"], row["spell_id"]) for row in arms["talents"]] == [
        ("Improved Heroic Strike", 3, 3, 12664),
        ("Improved Rend", 3, 3, 12659),
        ("Tactical Mastery", 5, 5, 12679),
        ("Anger Management", 1, 1, 12296),
        ("Deep Wounds", 3, 3, 12867),
        ("Impale", 2, 2, 16494),
    ]
    assert decoded["points_total"] == 51


def test_a_mop_classic_build_names_the_talent_chosen_in_each_tier() -> None:
    decoded = _decode("mop-classic", "druid", "323222")
    assert decoded["format"] == "mop_tiers"
    assert [(tier["level"], tier["choice"], tier["talent"]["name"]) for tier in decoded["tiers"]] == [
        (15, 3, "Wild Charge"),
        (30, 2, "Renewal"),
        (45, 3, "Typhoon"),
        (60, 2, "Incarnation"),
        (75, 2, "Ursol's Vortex"),
        (90, 2, "Dream of Cenarius"),
    ]
    partial = _decode("mop-classic", "rogue", "30")
    assert [tier["choice"] for tier in partial["tiers"]] == [3, 0, 0, 0, 0, 0]
    assert partial["tiers_chosen"] == 1


@pytest.mark.parametrize(
    ("calculator", "actor_class", "code", "reason"),
    [
        ("classic", "warrior", "6", "Rank 6 is over talent"),
        ("classic", "warrior", "0-0-0-5", "names 4 trees"),
        ("classic", "warrior", "3030500130200000000000000", "more digits than the tree has talents"),
        ("classic", "warrior", "AQ", "declared byte count"),
        # Header declares one byte; its last 2-bit code starts an incomplete rank.
        ("classic", "warrior", "AQI", "second 2-bit value"),
        ("forever", "warrior", "2252202-5321011", "start with 'v2'"),
        ("cata", "mage", "AAAA", "not decoded for the cata calculator"),
        ("classic-ptr", "warrior", "AcAEOMPzNAL4Mw", "not decoded for the classic-ptr calculator"),
        ("mop-classic", "druid", "3242", "0-3 choice"),
    ],
)
def test_a_build_code_the_data_cannot_account_for_is_not_decoded(calculator: str, actor_class: str, code: str, reason: str) -> None:
    decoded = _decode(calculator, actor_class, code)
    assert decoded["decoded"] is False
    assert reason in decoded["reason"]


def test_the_calculator_page_names_its_versioned_data_file() -> None:
    assert talent_data_url(CLASSIC_CALC_HTML) == CLASSIC_DATA_URL
    assert talent_data_url("<html></html>") is None


def _serve(
    monkeypatch: pytest.MonkeyPatch, *, data_url: str = CLASSIC_DATA_URL, fixture: str = "talent_data_classic.js", data_text: str | None = None
) -> list[str]:
    """Serve a calculator page naming ``data_url`` and the captured data file there; returns the URLs requested."""
    requested: list[str] = []
    html = CLASSIC_CALC_HTML.replace(CLASSIC_DATA_URL.replace("&", "&amp;"), data_url.replace("&", "&amp;"))

    def fake_request(self: WowheadClient, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        requested.append(url)
        body = (data_text if data_text is not None else captured_page(fixture)) if url == data_url else html
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(WowheadClient, "_request_with_retries", fake_request)
    return requested


def test_talent_calc_decodes_a_mop_classic_url_with_its_glyph_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a spec segment MoP Classic's calculator opens the class's first spec; the tiers are the class's."""
    _serve(monkeypatch, data_url="https://nether.wowhead.com/mop-classic/data/talents-classic?dv=29&db=1786100712", fixture="talent_data_mop-classic.js")

    result = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/mop-classic/talent-calc/rogue/321213/AA4FnLB4bilC4FnTD4F6WE4F6RF4F6Q"])

    assert result.exit_code == 0, result.output
    talents = json.loads(result.stdout)["data"]["talents"]
    assert talents["glyphs_code"] == "AA4FnLB4bilC4FnTD4F6WE4F6RF4F6Q"
    assert [tier["talent"]["name"] for tier in talents["tiers"]] == [
        "Shadow Focus", "Nerve Strike", "Cheat Death", "Shadowstep", "Prey on the Weak", "Anticipation",
    ]


def test_talent_calc_decodes_a_classic_url_with_its_selection_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL copied from Wowhead's calculator after clicking talents carries the click order after the code."""
    _serve(monkeypatch)
    url = "https://www.wowhead.com/classic/talent-calc/warrior/30305001302-05050005525010051/1aaabb0cC"

    result = runner.invoke(app, ["talent-calc", url])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert (data["tool"]["build_code"], data["tool"]["extra_segment"]) == ("30305001302-05050005525010051", "1aaabb0cC")
    talents = data["talents"]
    assert (talents["decoded"], talents["points_by_tree"], talents["selection_order"]) == (True, "17/34/0", "1aaabb0cC")
    assert talents["data_url"] == CLASSIC_DATA_URL


def test_the_trimmed_data_file_is_cached_across_clients(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path / "cache"))
    requested = _serve(monkeypatch)

    first = WowheadClient(expansion="classic").talent_calc_data(CLASSIC_DATA_URL)
    second = WowheadClient(expansion="classic").talent_calc_data(CLASSIC_DATA_URL)

    assert requested == [CLASSIC_DATA_URL]
    assert first == second
    assert set(first) == {"hash_version", "trees", "class_tiers", "names"}


@pytest.mark.parametrize("corruption", [{}, {"161": [None]}, {"161": [[1, 0, 0, []]]}])
def test_a_malformed_talent_data_cache_entry_is_fetched_again(monkeypatch: pytest.MonkeyPatch, corruption: dict[str, Any]) -> None:
    cached = {"hash_version": "", "trees": corruption, "class_tiers": {}, "names": {}} if corruption else {}
    monkeypatch.setattr(WowheadClient, "_read_cache", lambda self, key: cached)
    requested = _serve(monkeypatch)

    with WowheadClient(expansion="classic") as client:
        data = client.talent_calc_data(CLASSIC_DATA_URL)
        again = client.talent_calc_data(CLASSIC_DATA_URL)

    assert requested == [CLASSIC_DATA_URL]
    assert data == again
    assert decode_build(data, calculator="classic", actor_class="warrior", build_code="30305001302-05050005525010051")["points_by_tree"] == "17/34/0"


@pytest.mark.parametrize("talent", [{"col": 0, "ranks": [123]}, {"row": 0, "col": 0, "ranks": [None]}])
def test_malformed_provider_talent_data_returns_an_undecoded_reason(monkeypatch: pytest.MonkeyPatch, talent: dict[str, Any]) -> None:
    malformed = 'WH.setPageData("wow.talentCalcClassic.classic.data",' + json.dumps({"talents": {"161": {"1": talent}}}) + ");"
    _serve(monkeypatch, data_text=malformed)

    result = runner.invoke(app, ["talent-calc", "classic/warrior/30305001302-05050005525010051"])

    assert result.exit_code == 0, result.output
    talents = json.loads(result.stdout)["data"]["talents"]
    assert talents["decoded"] is False
    assert "did not parse" in talents["reason"]
    assert "malformed talent row" in talents["reason"]


def test_talent_calc_says_why_a_build_is_not_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch)

    spec_named = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"])
    retail = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/talent-calc/druid/balance/CYGAAAAAAAAAAAAAAAAAAAA"])

    assert spec_named.exit_code == 0, spec_named.output
    assert json.loads(spec_named.stdout)["data"]["talents"] == {
        "decoded": False,
        "reason": "The classic calculator's paths name no spec, so this spec-named path is not one of its builds.",
    }
    assert retail.exit_code == 0, retail.output
    assert json.loads(retail.stdout)["data"]["talents"]["decoded"] is False
    assert "talent-describe" in json.loads(retail.stdout)["data"]["talents"]["reason"]


def test_talent_calc_reports_an_unfetchable_data_file_without_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(self: WowheadClient, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url == CLASSIC_DATA_URL:
            raise httpx.HTTPStatusError("blocked", request=request, response=httpx.Response(403, request=request))
        return httpx.Response(200, text=CLASSIC_CALC_HTML, request=request)

    monkeypatch.setattr(WowheadClient, "_request_with_retries", fake_request)

    result = runner.invoke(app, ["talent-calc", "classic/warrior/30305001302-05050005525010051"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["tool"]["build_code"] == "30305001302-05050005525010051"
    assert data["talents"]["decoded"] is False
    assert data["talents"]["fetch_error"]["message"] == "Wowhead returned HTTP 403"
