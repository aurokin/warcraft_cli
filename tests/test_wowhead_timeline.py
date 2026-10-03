"""News, blue-tracker, and guide-listing commands for the wowhead CLI."""

from __future__ import annotations

import json
from typing import Any

import pytest
from wowhead_cli.main import app
from wowhead_cli.ranking import listing_match_score

from tests.wowhead_testkit import (
    SAMPLE_BLUE_TOPIC_HTML,
    SAMPLE_BLUE_TRACKER_HTML,
    SAMPLE_GUIDE_CATEGORY_HTML,
    SAMPLE_NEWS_HTML,
    SAMPLE_NEWS_POST_HTML,
    runner,
)


def test_news_command_filters_by_query_and_date(monkeypatch) -> None:
    def fake_news_page(self, *, page: int = 1):
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
    assert payload["data"]["count"] == 1
    assert payload["data"]["results"][0]["id"] == 380785
    assert payload["data"]["results"][0]["preview"] == "Class bugfixes and more."
    assert payload["data"]["scan"]["pages_scanned"] == 1
    assert payload["data"]["scan"]["total_pages"] == 1637
    assert payload["data"]["news_url"] == "https://www.wowhead.com/news"
    assert payload["data"]["facets"]["authors"] == ["Staff"]
    assert payload["data"]["facets"]["types"] == ["News"]



def test_news_query_matches_whole_words_and_needs_every_word(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.news_page_html", lambda self, *, page=1: SAMPLE_NEWS_HTML)

    def result_ids(query: str) -> list[int]:
        result = runner.invoke(app, ["news", query, "--pages", "1"])
        assert result.exit_code == 0, result.output
        return [row["id"] for row in json.loads(result.stdout)["data"]["results"]]

    assert result_ids("midnight hotfixes") == [380785]
    # Words match up to a plural ending, in either direction.
    assert result_ids("hotfix") == [380785]
    assert result_ids("roundups") == [380700]
    # "fix" is inside "Hotfixes" and "bugfixes", and "hot" starts "Hotfixes", but neither is that word.
    assert result_ids("fix") == []
    assert result_ids("hot") == []
    # "tuning" matches the other post, but "hotfixes" is not in it.
    assert result_ids("tuning hotfixes") == []


def test_listing_query_word_matches_a_possessive_but_not_a_longer_word() -> None:
    assert listing_match_score("mage", "Mage's Tower Returns") > 0
    assert listing_match_score("mage", "Damage Meter Changes") == 0
    assert listing_match_score("mage", "Magelord Rommath Returns") == 0
    assert listing_match_score("patch", "Patches Roundup") > 0
    # "-es" is a plural ending only after a sibilant: "notes" is not "Not", "cap" is not "Capes".
    assert listing_match_score("notes", "Patch 12.1 Is Not Live Yet") == 0
    assert listing_match_score("cap", "New Capes in Patch 12.1") == 0


def test_news_command_filters_by_author_and_type(monkeypatch) -> None:
    def fake_news_page(self, *, page: int = 1):
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
    assert payload["data"]["filters"]["authors"] == ["staff"]
    assert payload["data"]["filters"]["types"] == ["news"]
    assert payload["data"]["count"] == 2
    assert payload["data"]["facets"]["authors"] == ["Staff"]
    assert payload["data"]["facets"]["types"] == ["News"]



def test_blue_tracker_command_filters_by_topic_and_date(monkeypatch) -> None:
    def fake_blue_page(self, *, page: int = 1):
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
    assert payload["data"]["count"] == 1
    assert payload["data"]["results"][0]["id"] == 610948
    assert payload["data"]["results"][0]["region"] == "eu"
    assert payload["data"]["results"][0]["body_preview"] == "Druid and Priest updates."
    assert payload["data"]["scan"]["pages_scanned"] == 1
    assert payload["data"]["scan"]["total_pages"] == 671
    assert payload["data"]["blue_tracker_url"] == "https://www.wowhead.com/blue-tracker"
    assert payload["data"]["facets"]["regions"] == ["eu"]
    assert payload["data"]["facets"]["forums"] == ["General Discussion"]



def test_blue_tracker_command_filters_by_author_region_and_forum(monkeypatch) -> None:
    def fake_blue_page(self, *, page: int = 1):
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
    assert payload["data"]["filters"]["authors"] == ["blizzard"]
    assert payload["data"]["filters"]["regions"] == ["eu"]
    assert payload["data"]["filters"]["forums"] == ["general discussion"]
    assert payload["data"]["count"] == 1
    assert payload["data"]["results"][0]["id"] == 610948
    assert payload["data"]["facets"]["authors"] == ["Blizzard"]



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
    def fake_guides_page(self, category: str):
        assert category == "classes"
        return SAMPLE_GUIDE_CATEGORY_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_category_page_html", fake_guides_page)
    result = runner.invoke(app, ["guides", "classes", "death knight", "--limit", "5"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["category"] == "classes"
    assert payload["data"]["count"] == 1
    assert payload["data"]["results"][0]["id"] == 32000
    assert payload["data"]["results"][0]["url"].endswith("/frost/overview-pve-dps")
    assert payload["data"]["facets"]["authors"] == ["Khazakdk"]



def test_guides_command_filters_by_author_and_patch(monkeypatch) -> None:
    def fake_guides_page(self, category: str):
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
    assert payload["data"]["filters"]["authors"] == ["khazakdk"]
    assert payload["data"]["filters"]["patch_min"] == 120001
    assert payload["data"]["count"] == 1
    assert payload["data"]["results"][0]["id"] == 32000



def test_guides_command_sorts_by_rating(monkeypatch) -> None:
    def fake_guides_page(self, category: str):
        assert category == "classes"
        return SAMPLE_GUIDE_CATEGORY_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_category_page_html", fake_guides_page)
    result = runner.invoke(app, ["guides", "classes", "--sort", "rating", "--limit", "2"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["filters"]["sort"] == "rating"
    assert [row["id"] for row in payload["data"]["results"]] == [32000, 33131]



@pytest.mark.parametrize(
    ("command", "url", "html"),
    [
        ("news-post", "https://www.wowhead.com/classic/news/midnight-hotfixes-380785", SAMPLE_NEWS_POST_HTML),
        ("blue-topic", "https://www.wowhead.com/classic/blue-tracker/topic/us/class-tuning-1", SAMPLE_BLUE_TOPIC_HTML),
    ],
)
def test_article_commands_report_the_expansion_their_url_names(monkeypatch, command: str, url: str, html: str) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, page_url: html)

    result = runner.invoke(app, [command, url])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["expansion"] == "classic"


def test_news_post_command_extracts_markup_and_author(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url == "https://www.wowhead.com/news/midnight-hotfixes-380785"
        return SAMPLE_NEWS_POST_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["news-post", "/news/midnight-hotfixes-380785"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["post"]["page_url"] == "https://www.wowhead.com/news/midnight-hotfixes-380785"
    assert payload["data"]["content"]["section_count"] == 1
    assert payload["data"]["author"]["username"] == "staff"
    assert payload["data"]["related"]["news"]["count"] == 1
    assert payload["data"]["related"]["blueTracker"]["items"][0]["is_blue_tracker"] is True
    assert "Death Knight fixes" in payload["data"]["content"]["text"]



def test_blue_topic_command_extracts_posts(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url == "https://www.wowhead.com/blue-tracker/topic/eu/class-tuning-incoming-18-march-610948"
        return SAMPLE_BLUE_TOPIC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["blue-topic", "/blue-tracker/topic/eu/class-tuning-incoming-18-march-610948"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["posts"]["count"] == 1
    first = payload["data"]["posts"]["items"][0]
    assert first["author"] == "Kaivax"
    assert first["author_page"] == "https://www.wowhead.com/blue-tracker/author/Kaivax"
    assert first["blue"] is True
    assert first["body_text"].startswith("The first few days of Midnight")
    assert payload["data"]["summary"]["participants"] == ["Kaivax"]
    assert payload["data"]["summary"]["blue_authors"] == ["Kaivax"]


@pytest.mark.parametrize("command", ["news-post", "blue-topic"])
def test_a_reference_that_is_not_a_wowhead_page_is_a_usage_error(command: str) -> None:
    result = runner.invoke(app, [command, "https://example.com/news/some-post"])

    assert (result.exit_code, json.loads(result.stderr)["error"]["code"]) == (2, "invalid_ref")


@pytest.mark.parametrize(
    ("command", "url"),
    [
        ("news-post", "https://www.wowhead.com/item=19019"),
        ("news-post", "https://www.wowhead.com/blue-tracker/news/us/hotfixes-october-1-2026-24296142"),
        ("blue-topic", "https://www.wowhead.com/blue-tracker/news/us/hotfixes-october-1-2026-24296142"),
    ],
)
def test_an_article_command_refuses_a_wowhead_url_for_another_page(command: str, url: str) -> None:
    """news-post used to read an item page as a news article, and blue-topic failed parse_error (exit 1)."""
    result = runner.invoke(app, [command, url])

    assert (result.exit_code, json.loads(result.stderr)["error"]["code"]) == (2, "invalid_ref")


def test_news_post_fails_when_the_page_has_no_article_body(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, url: "<html><head><title>x</title></head></html>")

    result = runner.invoke(app, ["news-post", "/news/post-1"])

    assert (result.exit_code, json.loads(result.stderr)["error"]["code"]) == (1, "parse_error")


def test_news_post_notes_that_a_wow_forever_post_has_no_expansion_profile(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, url: SAMPLE_NEWS_POST_HTML)
    url = "https://www.wowhead.com/forever/news/ghost-wolf-383241"

    result = runner.invoke(app, ["news-post", url])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["notes"] == [f"Could not infer expansion from URL {url!r}."]


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
        f"{json.dumps(payload)}</script></head>"
        '<body><script>WH.markup.printHtml("Post body.", "news-post");</script></body></html>'
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


def test_guides_fails_not_found_when_wowhead_serves_its_index_for_an_unknown_category(monkeypatch) -> None:
    """Wowhead redirects /guides/class to /guides; that used to answer ok with every site guide labelled `class`."""
    index_html = SAMPLE_GUIDE_CATEGORY_HTML.replace(
        "<html>", '<html><head><link rel="canonical" href="https://www.wowhead.com/guides"></head>', 1
    )
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_category_page_html", lambda self, category: index_html)

    result = runner.invoke(app, ["guides", "class"])

    assert result.exit_code == 4
    assert json.loads(result.stderr)["error"]["code"] == "not_found"


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
