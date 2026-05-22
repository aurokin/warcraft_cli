from __future__ import annotations

import json

from typer.testing import CliRunner

from wowhead_cli.comments_intelligence import (
    build_comments_intelligence,
    detect_near_duplicate_groups,
    filter_raw_comments,
)
from wowhead_cli.main import app

runner = CliRunner()

SAMPLE_COMMENTS = [
    {
        "id": 1,
        "user": "Alice",
        "body": "This weapon is incredible for raiding",
        "date": "2024-06-01T00:00:00-06:00",
        "rating": 9,
        "nreplies": 4,
        "replies": [],
    },
    {
        "id": 2,
        "user": "Bob",
        "body": "This weapon is incredible for raiding!",
        "date": "2024-05-01T00:00:00-06:00",
        "rating": 3,
        "nreplies": 0,
        "replies": [],
    },
    {
        "id": 3,
        "user": "Carol",
        "body": "Different take entirely",
        "date": "2024-01-01T00:00:00-06:00",
        "rating": 6,
        "nreplies": 1,
        "replies": [],
    },
]


def test_filter_raw_comments_applies_date_author_and_keyword_filters() -> None:
    filtered, metadata = filter_raw_comments(
        SAMPLE_COMMENTS,
        date_from="2024-02-01",
        author="alice",
        keywords=("weapon", "raiding"),
    )
    assert len(filtered) == 1
    assert filtered[0]["id"] == 1
    assert metadata["keywords"] == ["weapon", "raiding"]


def test_detect_near_duplicate_groups_groups_similar_bodies() -> None:
    payload = detect_near_duplicate_groups(
        SAMPLE_COMMENTS,
        page_url="https://www.wowhead.com/item=19019",
    )
    assert payload["group_count"] == 1
    assert set(payload["groups"][0]["comment_ids"]) == {1, 2}


def test_build_comments_intelligence_returns_cited_insights() -> None:
    payload = build_comments_intelligence(
        page_url="https://www.wowhead.com/item=19019",
        embedded_total=3,
        filtered_comments=SAMPLE_COMMENTS,
        filters={"keywords": []},
        insight_limit=3,
    )
    assert payload["sample"]["filtered_count"] == 3
    assert payload["insights"][0]["kind"] == "top_rated"
    assert payload["insights"][0]["citation_url"].endswith("#comments:id=1")
    assert "sentiment_score" not in payload
    assert "consensus_score" not in payload


def test_comments_command_supports_insights_and_filters(monkeypatch) -> None:
    html = """
    <html><body>
      <script>
        var lv_comments0 = [
          {"id": 1, "number": 0, "user": "Alice", "body": "BiS for fire mages", "date": "2024-06-01T00:00:00-06:00", "rating": 9, "nreplies": 2, "replies": []},
          {"id": 2, "number": 1, "user": "Bob", "body": "Not worth it", "date": "2024-01-01T00:00:00-06:00", "rating": 2, "nreplies": 0, "replies": []}
        ];
      </script>
    </body></html>
    """

    monkeypatch.setattr(
        "wowhead_cli.main._fetch_entity_page",
        lambda ctx, client, page_type, page_id: (html, {"canonical_url": "https://www.wowhead.com/item=19019"}),
    )

    result = runner.invoke(
        app,
        [
            "comments",
            "item",
            "19019",
            "--insights",
            "--author",
            "alice",
            "--keyword",
            "fire",
            "--limit",
            "5",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["counts"]["embedded_comments"] == 2
    assert payload["counts"]["filtered_comments"] == 1
    assert payload["intelligence"]["insights"][0]["comment_id"] == 1
    assert payload["intelligence"]["freshness"]["comment_count"] == 1
