"""Live parser canaries for pinned Wowhead entity pages and the news listing (AUR-359).

``tests/conftest.py`` skips every ``live``-marked test unless ``WOWHEAD_LIVE_TESTS`` is set.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from wowhead_cli.entity_types import suggestion_entity_type_from_type_id
from wowhead_cli.expansion_profiles import (
    build_entity_url,
    build_search_suggestions_url,
    resolve_expansion,
)
from wowhead_cli.listing_filters import parse_date_bound, parse_iso8601_utc
from wowhead_cli.main import _extract_news_page_data, _normalize_news_row, app
from wowhead_cli.page_parser import (
    extract_comments_dataset,
    extract_json_ld,
    extract_linked_entities_from_href,
    parse_page_meta_json,
    parse_page_metadata,
)
from wowhead_cli.wowhead_client import news_url

from tests.fixtures.wowhead_canaries import (
    PARSER_CANARIES,
    SUGGESTION_TYPE_NAME_TO_ENTITY,
    ParserCanary,
)
from tests.wowhead_testkit import runner

pytestmark = pytest.mark.live


def _fetch_entity_html(canary: ParserCanary) -> tuple[str, str, float]:
    profile = resolve_expansion(canary.expansion)
    url = build_entity_url(profile, canary.entity_type, canary.entity_id)
    started = time.perf_counter()
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        response = client.get(url)
        latency_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
    return url, response.text, latency_ms


@pytest.mark.parametrize("canary", PARSER_CANARIES, ids=lambda row: row.case_id)
def test_live_wowhead_parser_canary_page(canary: ParserCanary) -> None:
    profile = resolve_expansion(canary.expansion)
    url, html, latency_ms = _fetch_entity_html(canary)

    assert latency_ms < 15000, f"canary page fetch too slow ({latency_ms:.0f}ms) for {canary.case_id}"

    meta = parse_page_metadata(html, fallback_url=url)
    assert isinstance(meta.get("canonical_url"), str)
    assert meta["canonical_url"].startswith(profile.wowhead_base)

    page_meta = parse_page_meta_json(html)
    data_env = page_meta.get("dataEnv") if isinstance(page_meta, dict) else None
    assert isinstance(data_env, dict)
    assert data_env.get("env") == profile.data_env

    linked = extract_linked_entities_from_href(html, source_url=meta["canonical_url"])
    assert len(linked) > 0, f"no linked entities parsed for {canary.case_id}"

    comments = extract_comments_dataset(html)
    assert isinstance(comments, list)


def _fetch(url: str) -> str:
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def test_live_news_date_from_window_returns_the_posts_inside_it() -> None:
    """The regression this guards: `news --date-from` exiting 0 with zero rows for every window."""
    date_from = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    result = runner.invoke(app, ["news", "--date-from", date_from, "--pages", "2", "--limit", "20"])
    assert result.exit_code == 0, result.stdout

    data = json.loads(result.stdout)["data"]
    assert data["count"] > 0, "no Wowhead news post fell inside a seven-day window"
    assert data["scan"]["unparsed_timestamps"] == 0

    bound = parse_date_bound(date_from, end_of_day=False)
    for row in data["results"]:
        posted_at = parse_iso8601_utc(row["posted_at"])
        assert posted_at is not None and posted_at >= bound, row


def test_live_news_listing_timestamps_drive_the_date_window() -> None:
    """The date window is only as good as the listing timestamp, which Wowhead renders, not ISO-formats."""
    time.sleep(3)
    rows, _total_pages = _extract_news_page_data(_fetch(news_url(page=1)))
    normalized = [row for row in (_normalize_news_row(raw) for raw in rows) if row is not None]
    assert len(normalized) >= 10

    unparsed = [row["posted"] for row in normalized if row["posted_at"] is None]
    assert unparsed == [], f"unparsed Wowhead listing timestamps: {unparsed}"

    now = datetime.now(UTC)
    posted = [parse_iso8601_utc(row["posted_at"]) for row in normalized]
    assert max(posted) <= now + timedelta(hours=1), "listing timestamps parse into the future"

    # The same window `news --date-from` builds must still select the front page's newest posts.
    date_from = parse_date_bound((now - timedelta(days=7)).date().isoformat(), end_of_day=False)
    in_window = [row for row, at in zip(normalized, posted, strict=True) if at >= date_from]
    assert in_window, "no front-page post falls inside a 7-day window"

    # Cross-check one row against the article's own ISO timestamp: this is what pins the timezone.
    newest_row, newest_posted_at = max(zip(normalized, posted, strict=True), key=lambda pair: pair[1])
    article = extract_json_ld(_fetch(newest_row["url"]))
    assert isinstance(article, dict)
    article_published = parse_iso8601_utc(article.get("datePublished"))
    assert article_published is not None
    assert abs((article_published - newest_posted_at).total_seconds()) <= 60


def test_live_suggestion_type_ids_still_mean_what_the_routing_table_says() -> None:
    """A renumbered `type` would mislabel entities and emit wrong follow-up URLs, silently."""
    # Four queries so the check reaches more than the handful of types one query happens to return.
    seen: dict[str, str] = {}
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        for index, query in enumerate(("thunderfury", "un'goro", "valorstones", "fury warrior guide")):
            if index:
                time.sleep(3)
            response = client.get(
                build_search_suggestions_url(resolve_expansion(None)),
                params={"q": query},
            )
            response.raise_for_status()
            payload = response.json()

            rows = list(payload["results"])
            for category_rows in payload.get("categories", {}).values():
                rows.extend(category_rows)
            assert rows, query

            for row in rows:
                derived = suggestion_entity_type_from_type_id(row["type"])
                if derived is None:
                    continue
                assert derived == SUGGESTION_TYPE_NAME_TO_ENTITY[row["typeName"]], (query, row)
                seen[row["typeName"]] = derived

    assert seen, "no routed suggestion row came back at all"
