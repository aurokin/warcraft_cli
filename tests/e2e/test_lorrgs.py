"""End-to-end journeys for the ``lorrgs`` binary.

Every identifier these journeys use (spec slug, boss slug, zone id, season slug, spell id, report
code) is discovered from a Lorrgs index command at run time, so nothing here rots when a tier
turns over. The index pass lives in the module-scoped ``catalog`` fixture and is shared by the
journeys below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tests.e2e.harness import EXIT_GENERIC, EXIT_NOT_FOUND, Result, run, run_text

# Ranked parses exist only for specs people actually play on a fresh tier; walk a few before giving
# up so the report journeys always have a real code to work with.
_SPEC_RANKING_ATTEMPTS = 4

COMP_RANKING_LIMIT = 3
# How many bosses the comp-ranking journey walks before declaring the surface empty.
COMP_RANKING_SCAN_LIMIT = 16


@dataclass(frozen=True)
class Catalog:
    """Live Lorrgs identifiers plus the index payloads the journeys assert against."""

    specs: Result
    bosses: Result
    zones: Result
    season: Result
    season_slug: str
    zone_id: str
    zone_boss_slugs: tuple[str, ...]
    boss_slug: str
    spec_slug: str
    spec_ranking: Result
    report_id: str
    fight_id: int


def _spec_slugs(specs: Result) -> list[str]:
    rows = specs.data["specs"]
    # Damage specs carry the deepest ranking coverage; keep the source order Lorrgs returns.
    return [row["full_name_slug"] for row in rows if row.get("role") in {"rdps", "mdps"}]


def _first_report(ranking: Result) -> tuple[str, int] | None:
    for report in ranking.data.get("reports", []):
        fights = report.get("fights") or []
        if isinstance(report.get("report_id"), str) and fights:
            return report["report_id"], fights[0]["fight_id"]
    return None


@pytest.fixture(scope="module")
def catalog(skip_list: frozenset[str], doctor_rows: dict[str, dict[str, Any]]) -> Catalog:
    """Walk the Lorrgs index once: season -> zone -> boss -> spec -> a ranked report."""
    if "lorrgs" in skip_list:
        pytest.skip("lorrgs excluded via WARCRAFT_E2E_SKIP")
    assert "lorrgs" in doctor_rows, f"lorrgs is not registered in warcraft doctor: {sorted(doctor_rows)}"

    season = run("lorrgs", "current-season")
    raids = season.data["raids"]
    assert raids, season.describe()
    # Lorrgs orders a season's raids newest first and uses float ids (e.g. 53.1) for split zones.
    zone_id = f"{raids[0]:g}"
    zone_bosses = run("lorrgs", "zone-bosses", zone_id)
    zone_boss_slugs = tuple(zone_bosses.data)
    assert zone_boss_slugs, zone_bosses.describe()
    boss_slug = zone_boss_slugs[0]

    specs = run("lorrgs", "specs")
    bosses = run("lorrgs", "bosses")
    zones = run("lorrgs", "zones")

    for spec_slug in _spec_slugs(specs)[:_SPEC_RANKING_ATTEMPTS]:
        ranking = run("lorrgs", "spec-ranking", spec_slug, boss_slug)
        found = _first_report(ranking)
        if found is not None:
            return Catalog(
                specs=specs,
                bosses=bosses,
                zones=zones,
                season=season,
                season_slug=season.data["slug"],
                zone_id=zone_id,
                zone_boss_slugs=zone_boss_slugs,
                boss_slug=boss_slug,
                spec_slug=spec_slug,
                spec_ranking=ranking,
                report_id=found[0],
                fight_id=found[1],
            )
    raise AssertionError(f"no ranked Lorrgs parse for {boss_slug} in the first {_SPEC_RANKING_ATTEMPTS} damage specs")


def test_doctor_reports_endpoints_and_capabilities(require) -> None:
    require("lorrgs")
    result = run("lorrgs", "doctor")
    assert result.data["auth"] == {"required": False, "configured": True, "flow": "none"}
    assert result.data["endpoints"]["api"].startswith("https://")
    capabilities = result.data["capabilities"]
    assert capabilities["spec_ranking"] == "ready"
    assert capabilities["user_report"] == "ready_cached_only"
    assert result.data["notes"]


def test_roles_and_classes_describe_the_spec_taxonomy(require) -> None:
    require("lorrgs")
    roles = run("lorrgs", "roles")
    codes = {row["code"] for row in roles.data["roles"]}
    assert {"mdps", "rdps"} <= codes, roles.describe()

    classes = run("lorrgs", "classes")
    # `classes` is keyed by class slug, and each class lists the spec slugs `lorrgs spec` accepts.
    mage = classes.data["mage"]
    assert mage["name"] == "Mage"
    assert "mage-frost" in mage["specs"]


def test_specs_index_and_one_spec_agree(catalog: Catalog) -> None:
    rows = {row["full_name_slug"]: row for row in catalog.specs.data["specs"]}
    assert catalog.spec_slug in rows
    indexed = rows[catalog.spec_slug]

    spec = run("lorrgs", "spec", catalog.spec_slug)
    assert spec.data["id"] == indexed["id"]
    assert spec.data["name"] == indexed["name"]
    assert spec.data["role"]["code"] == indexed["role"]
    assert spec.data["spells"], spec.describe()


def test_spec_spells_feeds_the_spell_lookup(catalog: Catalog) -> None:
    spells = run("lorrgs", "spec-spells", catalog.spec_slug)
    # Keyed by spell id as a string; every row repeats the id as an int.
    spell_id = next(iter(spells.data))
    tracked = spells.data[spell_id]
    assert str(tracked["spell_id"]) == spell_id

    spell = run("lorrgs", "spell", spell_id)
    assert spell.data["spell_id"] == tracked["spell_id"]
    assert spell.data["name"] == tracked["name"]
    assert spell.payload["query"] == {"spell_id": int(spell_id)}


def test_seasons_resolve_to_the_same_raid_list(catalog: Catalog) -> None:
    assert catalog.season.payload["query"] == {"season_slug": "current"}
    assert isinstance(catalog.season.data["name"], str)

    by_slug = run("lorrgs", "season", catalog.season_slug)
    assert by_slug.data["slug"] == catalog.season_slug
    assert by_slug.data["raids"] == catalog.season.data["raids"]


def test_zone_index_zone_and_zone_bosses_agree(catalog: Catalog) -> None:
    indexed = next(zone for zone in catalog.zones.data["zones"] if f"{zone['id']:g}" == catalog.zone_id)

    zone = run("lorrgs", "zone", catalog.zone_id)
    assert zone.data["name_slug"] == indexed["name_slug"]

    zone_bosses = run("lorrgs", "zone-bosses", catalog.zone_id)
    assert set(zone_bosses.data) == {boss["full_name_slug"] for boss in indexed["bosses"]}
    assert catalog.boss_slug in zone_bosses.data


def test_boss_and_boss_spells_describe_the_encounter(catalog: Catalog) -> None:
    indexed = next(row for row in catalog.bosses.data["bosses"] if row["full_name_slug"] == catalog.boss_slug)

    boss = run("lorrgs", "boss", catalog.boss_slug)
    assert boss.data["id"] == indexed["id"]
    assert boss.data["full_name"] == indexed["full_name"]

    spells = run("lorrgs", "boss-spells", catalog.boss_slug)
    assert spells.data, spells.describe()
    first = next(iter(spells.data.values()))
    assert first["spell_type"] == catalog.boss_slug
    assert first["name"]


def test_trinkets_carry_wowhead_item_references(require) -> None:
    require("lorrgs")
    result = run("lorrgs", "trinkets")
    assert result.data, result.describe()
    rows = list(result.data.values())
    assert all(row["spell_type"] in {"other-trinkets", "other-potions"} for row in rows), result.describe()
    assert any(row["wowhead_data"].startswith("item=") for row in rows), result.describe()


def test_spec_ranking_and_its_info_view_describe_the_same_ranking(catalog: Catalog) -> None:
    ranking = catalog.spec_ranking
    assert ranking.data["spec_slug"] == catalog.spec_slug
    assert ranking.data["boss_slug"] == catalog.boss_slug
    assert ranking.data["difficulty"] == "mythic"
    report = next(row for row in ranking.data["reports"] if row["report_id"] == catalog.report_id)
    casts = report["fights"][0]["players"][0]["casts"]
    assert casts and all("id" in cast and "ts" in cast for cast in casts), ranking.describe()

    info = run("lorrgs", "spec-ranking-info", catalog.spec_slug, catalog.boss_slug)
    assert info.data["metric"] == ranking.data["metric"]
    assert info.data["updated"] == ranking.data["updated"]
    # The info view exists to skip the timeline payload; that is the whole point of the surface.
    assert "reports" not in info.data
    assert len(info.stdout) < len(ranking.stdout)


def _comp_ranking(boss_slug: str, *extra: str) -> Result:
    return run("lorrgs", "comp-ranking", boss_slug, "--limit", str(COMP_RANKING_LIMIT), *extra)


def _comp_ranking_candidates(catalog: Catalog) -> list[str]:
    """Bosses to try for a populated comp ranking: this tier first, then one per other zone.

    Lorrgs builds a comp ranking from the parses it has already ingested, so a raid that opened
    days ago can legitimately have none yet. Older zones keep theirs, so the surface itself is
    still exercised.
    """
    candidates = list(catalog.zone_boss_slugs)
    for zone in catalog.zones.data["zones"]:
        if f"{zone['id']:g}" == catalog.zone_id:
            continue
        bosses = zone.get("bosses") or []
        if bosses:
            candidates.append(str(bosses[0]["full_name_slug"]))
    return candidates[:COMP_RANKING_SCAN_LIMIT]


def _kill_seconds(result: Result) -> dict[str, float]:
    """Each ranked comp's kill time in seconds (Lorrgs reports fight durations in milliseconds)."""
    return {row["report_id"]: row["fights"][0]["duration"] / 1000 for row in result.data["reports"]}


def test_comp_ranking_returns_ranked_comps_and_honours_the_killtime_filter(catalog: Catalog) -> None:
    """A comp ranking with rows in it, and kill-time bounds that each provably remove a row.

    An empty ``reports`` list used to pass this journey, which made it blind to the command
    returning nothing at all. Bosses are walked until one has rows; if none does, Lorrgs is not
    serving this surface and that is reported rather than absorbed. Each bound is set one second
    inside the unfiltered extremes, so the slowest (or fastest) comp has to disappear and every row
    that comes back has to sit inside the bound; an ignored flag returns the same rows and fails.
    """
    scanned: list[str] = []
    for boss_slug in _comp_ranking_candidates(catalog):
        scanned.append(boss_slug)
        result = _comp_ranking(boss_slug)
        assert result.data["boss_slug"] == boss_slug, result.describe()
        assert isinstance(result.data["updated"], str) and result.data["updated"], result.describe()
        assert result.payload["query"]["limit"] == COMP_RANKING_LIMIT, result.describe()
        reports = result.data["reports"]
        if not reports:
            continue

        assert len(reports) <= COMP_RANKING_LIMIT, result.describe()
        seconds = _kill_seconds(result)
        slowest = max(seconds, key=seconds.__getitem__)
        fastest = min(seconds, key=seconds.__getitem__)

        ceiling = int(seconds[slowest]) - 1
        capped = _comp_ranking(boss_slug, "--killtime-max", str(ceiling))
        assert capped.payload["query"]["killtime_max"] == ceiling, capped.describe()
        assert slowest not in _kill_seconds(capped), capped.describe()
        assert all(value <= ceiling for value in _kill_seconds(capped).values()), capped.describe()

        floor = int(seconds[fastest]) + 1
        floored = _comp_ranking(boss_slug, "--killtime-min", str(floor))
        assert floored.payload["query"]["killtime_min"] == floor, floored.describe()
        assert fastest not in _kill_seconds(floored), floored.describe()
        assert all(value >= floor for value in _kill_seconds(floored).values()), floored.describe()
        return

    raise AssertionError(f"Lorrgs published no comp ranking rows for any of {scanned}")


def test_report_overview_user_report_and_fights_share_one_report(catalog: Catalog) -> None:
    overview = run("lorrgs", "report-overview", catalog.report_id)
    assert overview.data["report_id"] == catalog.report_id
    assert overview.payload["query"]["refresh"] is False
    fights = {fight["fight_id"]: fight for fight in overview.data["fights"]}
    assert catalog.fight_id in fights, overview.describe()
    assert fights[catalog.fight_id]["boss"]["boss_slug"] == catalog.boss_slug

    cached = run("lorrgs", "user-report", catalog.report_id)
    assert cached.data["report_id"] == catalog.report_id
    assert cached.data["title"] == overview.data["title"]

    selected = run("lorrgs", "user-report-fights", catalog.report_id, "--fight", str(catalog.fight_id))
    assert [fight["fight_id"] for fight in selected.data["fights"]] == [catalog.fight_id]


def test_a_warcraftlogs_report_url_carries_the_fight_through(catalog: Catalog) -> None:
    url = f"https://www.warcraftlogs.com/reports/{catalog.report_id}?fight={catalog.fight_id}&type=damage-done"
    overview = run("lorrgs", "report-overview", url)
    assert overview.payload["query"]["report_id"] == catalog.report_id
    assert overview.payload["query"]["fight_id"] == catalog.fight_id
    assert overview.payload["query"]["report_type"] == "damage-done"

    # user-report-fights takes the fight from the URL, so no --fight is needed.
    selected = run("lorrgs", "user-report-fights", url)
    assert selected.payload["query"]["fight"] == str(catalog.fight_id)
    assert [fight["fight_id"] for fight in selected.data["fights"]] == [catalog.fight_id]


def test_search_ranks_the_spec_ranking_surface_first(catalog: Catalog) -> None:
    result = run("lorrgs", "search", f"{catalog.spec_slug} {catalog.boss_slug}", "--limit", "5")
    results = result.data["results"]
    assert results, result.describe()
    top = results[0]
    assert top["kind"] == "spec_ranking"
    assert top["spec_slug"] == catalog.spec_slug
    assert top["boss_slug"] == catalog.boss_slug
    assert top["follow_up"]["command"] == f"lorrgs spec-ranking {catalog.spec_slug} {catalog.boss_slug}"


def test_resolve_turns_a_lorrgs_url_into_the_next_command(catalog: Catalog) -> None:
    url = f"https://lorrgs.io/spec_ranking/{catalog.spec_slug}/{catalog.boss_slug}"
    result = run("lorrgs", "resolve", url)
    assert result.data["resolved"] is True
    assert result.data["confidence"] == "high"
    assert result.data["next_command"] == f"lorrgs spec-ranking {catalog.spec_slug} {catalog.boss_slug}"


def test_unknown_spec_and_boss_are_not_found(require) -> None:
    require("lorrgs")
    spec = run("lorrgs", "spec", "no-such-spec-slug", expect=EXIT_NOT_FOUND, error_code="not_found")
    assert spec.payload["error"]["details"]["status_code"] == 404

    boss = run("lorrgs", "boss", "no-such-boss-slug", expect=EXIT_NOT_FOUND, error_code="not_found")
    assert "no-such-boss-slug" in boss.payload["error"]["details"]["url"]


def test_a_malformed_report_reference_is_rejected_before_the_network(require) -> None:
    require("lorrgs")
    result = run("lorrgs", "user-report", "not a report", expect=EXIT_GENERIC, error_code="invalid_report_ref")
    assert "Warcraft Logs report URL" in result.payload["error"]["message"]


def test_help_names_every_documented_surface(require) -> None:
    require("lorrgs")
    text = run_text("lorrgs", "--help").stdout
    for command in ("spec-ranking-info", "comp-ranking", "user-report-fights", "current-season", "trinkets"):
        assert command in text, text
