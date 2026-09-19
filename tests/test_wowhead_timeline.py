"""News, blue-tracker, and guide-listing commands for the wowhead CLI."""

from __future__ import annotations

import json
from typing import Any

from wowhead_cli.main import app

from tests.wowhead_testkit import (
    SAMPLE_BLUE_TOPIC_HTML,
    SAMPLE_BLUE_TRACKER_HTML,
    SAMPLE_GUIDE_CATEGORY_HTML,
    SAMPLE_NEWS_HTML,
    SAMPLE_NEWS_POST_HTML,
    runner,
)


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
    assert result.exit_code == 2  # invalid_argument maps to the shared usage exit code
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




def _news_html(*posts: dict[str, Any]) -> str:
    """A synthetic news listing page carrying exactly the rows a test cares about."""
    payload = {"newsPosts": list(posts), "pinnedPosts": [], "totalPages": 1, "gathered": len(posts)}
    return (
        '<html><head><script type="application/json" id="data.news.newsData">'
        f"{json.dumps(payload)}</script></head></html>"
    )


def _news_post(post_id: int, posted_full: str) -> dict[str, Any]:
    return {
        "id": post_id,
        "title": f"Post {post_id}",
        "author": "Staff",
        "postedFull": posted_full,
        "postUrl": f"/news/post-{post_id}",
        "typeId": 1,
        "typeName": "News",
    }


def test_news_counts_the_rows_whose_timestamp_it_could_not_read(monkeypatch) -> None:
    """A row a date window has to drop is reported in `scan`, never dropped silently."""
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.news_page_html",
        lambda self, page=1: _news_html(
            _news_post(1, "2026/03/13 at 12:34 PM"),
            _news_post(2, "Yesterday at teatime"),
        ),
    )
    result = runner.invoke(app, ["news", "--date-from", "2026-03-01", "--limit", "5"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert [row["id"] for row in data["results"]] == [1]
    assert data["scan"]["unparsed_timestamps"] == 1


def test_news_fails_when_a_date_window_can_read_no_timestamp_at_all(monkeypatch) -> None:
    """Wowhead switching timestamp formats must be an error, not an empty ok:true answer."""
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.news_page_html",
        lambda self, page=1: _news_html(
            _news_post(1, "Yesterday at teatime"),
            _news_post(2, "Last week"),
        ),
    )
    result = runner.invoke(app, ["news", "--date-from", "2026-03-01", "--limit", "5"])
    assert result.exit_code == 1

    error = json.loads(result.output)["error"]
    assert error["code"] == "parse_error"
    assert "--date-from" in error["message"]


def test_news_without_a_date_window_still_returns_rows_it_cannot_timestamp(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.news_page_html",
        lambda self, page=1: _news_html(_news_post(1, "Yesterday at teatime")),
    )
    result = runner.invoke(app, ["news", "--limit", "5"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert [row["id"] for row in data["results"]] == [1]
    assert data["results"][0]["posted_at"] is None
    assert data["scan"]["unparsed_timestamps"] == 1


def _news_post_html(*recent: dict[str, Any]) -> str:
    payload = {"news": list(recent), "blueTracker": [], "video": False}
    return (
        '<html><head><link rel="canonical" href="https://www.wowhead.com/news/post-1">'
        '<script type="application/json" id="data.WH.News.recentPosts">'
        f"{json.dumps(payload)}</script></head><body></body></html>"
    )


def test_news_post_reports_the_related_rows_its_limit_cut_off(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.page_html",
        lambda self, page_url: _news_post_html(
            {"name": "Roundup A", "url": "/news/roundup-a-1", "author": "Staff", "time": "1h"},
            {"name": "Roundup B", "url": "/news/roundup-b-2", "author": "Staff", "time": "2h"},
            {"name": "Roundup C", "url": "/news/roundup-c-3", "author": "Staff", "time": "3h"},
        ),
    )
    result = runner.invoke(app, ["news-post", "/news/post-1", "--related-limit", "2"])
    assert result.exit_code == 0

    news = json.loads(result.stdout)["data"]["related"]["news"]
    assert news["count"] == len(news["items"]) == 2
    assert news["total"] == 3
    assert news["truncated"] is True
    assert [row["title"] for row in news["items"]] == ["Roundup A", "Roundup B"]


def test_guides_count_describes_the_returned_rows_not_the_pre_limit_match_set(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.guide_category_page_html",
        lambda self, category: SAMPLE_GUIDE_CATEGORY_HTML,
    )
    result = runner.invoke(app, ["guides", "classes", "--limit", "1"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["count"] == len(data["results"]) == 1
    assert data["total_matches"] == 2
    assert data["truncated"] is True
