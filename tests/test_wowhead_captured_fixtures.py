"""Wowhead parsing and ranking checked against captured (real, trimmed) Wowhead responses.

Everything here runs offline against `tests/fixtures/wowhead/`, which holds real responses rather
than markup a test author invented, so a Wowhead redesign or a wrong assumption about a field shows
up in the blocking suite instead of only in the weekly live canaries.

The captures are trimmed, not edited: every `<style>` element, every non-JSON `<script>`, and the
push-key/newsletter JSON blocks no parser reads are removed, and commenter display handles are
replaced with `commenter-<n>` so no third party's name ships in this repo. Everything a parser
reads is byte-identical to what Wowhead served.
"""

from __future__ import annotations

import json

from wowhead_cli.entity_types import SUGGESTION_TYPE_TO_ENTITY, suggestion_entity_type_from_type_id
from wowhead_cli.expansion_profiles import resolve_expansion
from wowhead_cli.main import app
from wowhead_cli.ranking import STALE_GUIDE_REASON, normalize_search_results

from tests.fixtures.wowhead_canaries import SUGGESTION_TYPE_NAME_TO_ENTITY
from tests.wowhead_testkit import captured_json, captured_page, runner

CAPTURED_ENTITY_PAGE = captured_page("item_19019_page.html")
CAPTURED_GUIDE_PAGE = captured_page("guide_283_page.html")
CAPTURED_NEWS_LISTING = captured_page("news_listing.html")
CAPTURED_BLUE_TRACKER_LISTING = captured_page("blue_tracker_listing.html")

# Every entity type Wowhead spells out in its own `typeName`, as the captured responses show it.
CAPTURED_SUGGESTION_FILES = (
    "search_suggestions_thunderfury.json",
    "search_suggestions_classic_thunderfury.json",
    "search_suggestions_ungoro.json",
    "search_suggestions_valorstones.json",
    "search_suggestions_fury_warrior_guide.json",
    "search_suggestions_argent_dawn.json",
    "search_suggestions_judgement_armor.json",
    "search_suggestions_spirit_beast.json",
)


def _stub_news(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.news_page_html",
        lambda self, *, page=1: CAPTURED_NEWS_LISTING,
    )


def test_news_parses_the_rendered_timestamp_wowhead_actually_emits(monkeypatch) -> None:
    _stub_news(monkeypatch)
    result = runner.invoke(app, ["news", "--pages", "1", "--limit", "20"])
    assert result.exit_code == 0

    rows = json.loads(result.stdout)["data"]["results"]
    assert len(rows) == 20
    top = rows[0]
    # Wowhead renders US Central wall-clock time; posted_at is the same instant in UTC.
    assert top["posted"] == "2026/09/18 at 3:30 PM"
    assert top["posted_at"] == "2026-09-18T20:30:00+00:00"
    assert all(row["posted_at"] is not None for row in rows)


def test_news_date_window_selects_the_posts_inside_it(monkeypatch) -> None:
    _stub_news(monkeypatch)
    result = runner.invoke(
        app,
        ["news", "--date-from", "2026-09-18", "--date-to", "2026-09-18", "--pages", "1", "--limit", "20"],
    )
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    # The window is compared in UTC, so the two posts Wowhead renders late on 09/17 US Central
    # (11:00 PM and 9:24 PM) belong to the 09/18 UTC day and are included.
    assert data["count"] == 8
    assert [row["id"] for row in data["results"]][:4] == [382931, 382963, 382989, 382924]
    assert all(row["posted_at"].startswith("2026-09-18") for row in data["results"])
    assert data["results"][-1]["posted"] == "2026/09/17 at 9:24 PM"


def test_news_count_describes_the_returned_rows_not_the_pre_limit_match_set(monkeypatch) -> None:
    _stub_news(monkeypatch)
    result = runner.invoke(app, ["news", "--pages", "1", "--limit", "3"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["count"] == len(data["results"]) == 3
    assert data["total_matches"] == 20
    assert data["truncated"] is True


def _stub_blue_tracker(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.blue_tracker_page_html",
        lambda self, *, page=1: CAPTURED_BLUE_TRACKER_LISTING,
    )


def test_blue_tracker_reads_its_offset_less_timestamps_as_us_central(monkeypatch) -> None:
    """The listing sends "2026-09-18 18:48:08"; the topic page dates it 18:03:10-05:00, not UTC."""
    _stub_blue_tracker(monkeypatch)
    result = runner.invoke(app, ["blue-tracker", "--pages", "1", "--limit", "50"])
    assert result.exit_code == 0

    rows = json.loads(result.stdout)["data"]["results"]
    assert rows[0]["posted"] == "2026-09-18 18:48:08"
    assert rows[0]["posted_at"] == "2026-09-18T23:48:08+00:00"
    assert rows[0]["author"] == "Linxy"
    assert rows[0]["title"] == "Class Tuning Incoming -- September 22"
    assert all(row["posted_at"] is not None for row in rows)


def test_blue_tracker_date_window_compares_the_central_instant(monkeypatch) -> None:
    _stub_blue_tracker(monkeypatch)
    result = runner.invoke(
        app,
        ["blue-tracker", "--date-from", "2026-09-18T20:00:00Z", "--pages", "1", "--limit", "50"],
    )
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    # Only the two 6:4x PM Central posts fall after 20:00 UTC; the noon ones (17:00 UTC) do not.
    assert [row["id"] for row in data["results"]] == [2354340, 629804]
    assert data["count"] == data["total_matches"] == 2
    assert data["scan"]["stop_reason"] == "date_from_reached"


def test_news_date_from_stops_the_page_scan_once_it_passes_the_window(monkeypatch) -> None:
    _stub_news(monkeypatch)
    result = runner.invoke(
        app,
        ["news", "--date-from", "2026-09-18", "--pages", "5", "--limit", "20"],
    )
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["scan"]["pages_scanned"] == 1
    assert data["scan"]["stop_reason"] == "date_from_reached"


def test_entity_page_parses_a_real_wowhead_item_page(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.entity_page_html",
        lambda self, entity_type, entity_id: CAPTURED_ENTITY_PAGE,
    )
    result = runner.invoke(app, ["entity-page", "item", "19019"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["page"]["title"] == "Thunderfury, Blessed Blade of the Windseeker"
    assert data["page"]["canonical_url"] == (
        "https://www.wowhead.com/item=19019/thunderfury-blessed-blade-of-the-windseeker"
    )
    links = data["linked_entities"]
    assert links["count"] == len(links["items"]) == links["total"]
    assert links["truncated"] is False
    by_ref = {(row["entity_type"], row["id"]): row for row in links["items"]}
    # Only Wowhead's Gatherer payload names Baron Geddon; the href link carries no usable text.
    assert by_ref[("npc", 12056)]["name"] == "Baron Geddon"
    assert by_ref[("npc", 12056)]["sources"] == ["gatherer", "href"]
    assert ("quest", 7786) in by_ref
    assert data["normalized"]["item"]["name"]["value"] == "Thunderfury, Blessed Blade of the Windseeker"


def test_entity_page_reports_truncation_of_a_real_link_list(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.entity_page_html",
        lambda self, entity_type, entity_id: CAPTURED_ENTITY_PAGE,
    )
    result = runner.invoke(app, ["entity-page", "item", "19019", "--max-links", "5"])
    assert result.exit_code == 0

    links = json.loads(result.stdout)["data"]["linked_entities"]
    assert links["count"] == len(links["items"]) == 5
    assert links["total"] > 5
    assert links["truncated"] is True


def test_comments_parses_the_real_embedded_comment_dataset(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.entity_page_html",
        lambda self, entity_type, entity_id: CAPTURED_ENTITY_PAGE,
    )
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.tooltip",
        lambda self, entity_type, entity_id, data_env=None: {"name": "Thunderfury"},
    )
    result = runner.invoke(app, ["comments", "item", "19019", "--sort", "rating", "--limit", "5"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["counts"]["embedded_comments"] == 5
    top = data["comments"][0]
    assert top["id"] == 360
    # Commenter handles are the one thing these captures do not keep verbatim; see the module note.
    assert top["user"] == "commenter-2"
    assert top["rating"] == 262
    assert top["citation_url"].endswith("#comments:id=360")
    assert [row["rating"] for row in data["comments"]] == sorted(
        (row["rating"] for row in data["comments"]), reverse=True
    )


def test_guide_full_parses_a_real_wowhead_guide_page(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.guide_page_html",
        lambda self, guide_id: CAPTURED_GUIDE_PAGE,
    )
    result = runner.invoke(app, ["guide-full", "283"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["guide"]["id"] == 283
    assert data["page"]["title"] == "A guide to Loremaster"
    assert data["page"]["canonical_url"] == "https://www.wowhead.com/guide/a-guide-to-loremaster-283"
    assert data["author"]["name"] == "Toroy"
    assert data["rating"] == {"score": 4.3535, "votes": 38}
    assert len(data["body"]["sections"]) == 12
    assert data["body"]["sections"][0] == {"level": 2, "title": "About this guide"}
    links = data["linked_entities"]
    assert links["count"] == len(links["items"]) == links["total"] == 9
    assert links["truncated"] is False
    assert links["source_counts"] == {"href": 9, "gatherer": 0, "merged": 9}
    assert data["comments"]["count"] == 5


def test_suggestion_type_ids_derive_the_entity_type_wowhead_labels_the_row() -> None:
    """Check the derived entity type against each captured row's own `typeName`, id by id."""
    seen: dict[str, str] = {}
    for name in CAPTURED_SUGGESTION_FILES:
        payload = captured_json(name)
        rows = list(payload["results"])
        for category_rows in payload.get("categories", {}).values():
            rows.extend(category_rows)
        for row in rows:
            derived = suggestion_entity_type_from_type_id(row["type"])
            if derived is None:
                continue
            expected = SUGGESTION_TYPE_NAME_TO_ENTITY[row["typeName"]]
            assert derived == expected, (name, row["type"], row["typeName"], derived)
            seen[row["typeName"]] = derived

    assert seen == SUGGESTION_TYPE_NAME_TO_ENTITY
    # `companion` (112) is the one routed id no captured suggestion response has produced; every
    # other type this repo routes on is checked above against Wowhead's own label for the row.
    assert set(SUGGESTION_TYPE_TO_ENTITY.values()) - set(seen.values()) == {"companion"}


def test_search_routes_a_real_news_suggestion_to_an_openable_url(monkeypatch) -> None:
    payload = captured_json("search_suggestions_classic_thunderfury.json")
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: payload)
    result = runner.invoke(app, ["--expansion", "classic", "search", "thunderfury", "--limit", "50"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    news_row = next(row for row in data["results"] if row["id"] == 375994)
    assert news_row["type_name"] == "News Post"
    assert news_row["entity_type"] == "news"
    assert news_row["url"] == "https://www.wowhead.com/classic/news=375994"
    assert news_row["follow_up"]["recommended_command"] == (
        "wowhead --expansion classic news-post https://www.wowhead.com/classic/news=375994"
    )
    # Every ranked row is reachable: nothing comes back with a null url a caller cannot open.
    assert all(row["url"] for row in data["results"])


def test_search_routes_a_real_world_event_suggestion_to_an_openable_url(monkeypatch) -> None:
    payload = captured_json("search_suggestions_ungoro.json")
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: payload)
    result = runner.invoke(app, ["search", "un'goro", "--limit", "50"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    event_row = next(row for row in data["results"] if row["type_name"] == "World Event")
    assert event_row["entity_type"] == "event"
    # Confirmed live: this redirects to /event=644/ungoro-madness.
    assert event_row["url"] == "https://www.wowhead.com/event=644"
    # An event has no follow-up command, which is not the same thing as being unopenable.
    assert "follow_up" not in event_row


def test_the_only_ranked_rows_left_without_a_url_are_ones_an_id_cannot_address() -> None:
    """Rank every row the captured responses contain and check each one is openable."""
    unroutable: set[str] = set()
    for name in CAPTURED_SUGGESTION_FILES:
        payload = captured_json(name)
        rows = list(payload["results"])
        for category_rows in payload.get("categories", {}).values():
            rows.extend(category_rows)
        ranked = normalize_search_results(rows, query=payload["search"], expansion=resolve_expansion(None))
        assert ranked, name
        unroutable.update(row["type_name"] for row in ranked if row["url"] is None)
    # Wowhead addresses these only as /trading-post-activity/<slug>-<id>; the id alone is not enough.
    assert unroutable == {"Trading Post Activity"}


def test_resolve_does_not_recommend_a_guide_the_response_shows_is_stale(monkeypatch) -> None:
    payload = captured_json("search_suggestions_fury_warrior_guide.json")
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: payload)
    result = runner.invoke(app, ["resolve", "Fury Warrior guide", "--limit", "10"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    # Wowhead's top-scoring title match is a retired Legion Remix guide from January.
    assert data["match"]["id"] == 31608
    assert data["match"]["metadata"]["updated"] == "2026-01-18"
    assert STALE_GUIDE_REASON in data["match"]["ranking"]["match_reasons"]
    assert data["confidence"] == "medium"
    assert data["resolved"] is False
    assert data["next_command"] is None
    assert data["fallback_search_command"] == "wowhead search 'Fury Warrior guide'"
    current = next(row for row in data["candidates"] if row["id"] == 3087)
    assert current["metadata"]["updated"] == "2026-08-20"
    assert STALE_GUIDE_REASON not in current["ranking"]["match_reasons"]
