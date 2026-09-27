from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

from wowhead_cli.page_parser import canonical_comment_url


def _parse_comment_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_boundary_timestamp(value: str, *, end_of_day: bool) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if end_of_day and len(value.strip()) <= 10:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


@dataclass(frozen=True, slots=True)
class _CommentFilters:
    """Comment filter inputs resolved once (timestamps parsed, needles lowered) before scanning rows."""

    boundary_from: datetime | None
    boundary_to: datetime | None
    min_replies: int | None
    author_needle: str | None
    keyword_needles: tuple[str, ...]


def _has_enough_replies(row: dict[str, Any], min_replies: int | None) -> bool:
    if min_replies is None:
        return True
    reply_count = row.get("nreplies")
    return isinstance(reply_count, int) and reply_count >= min_replies


def _matches_author(row: dict[str, Any], author_needle: str | None) -> bool:
    return author_needle is None or author_needle in str(row.get("user") or "").lower()


def _within_window(row: dict[str, Any], filters: _CommentFilters) -> bool:
    if filters.boundary_from is None and filters.boundary_to is None:
        return True
    timestamp = _parse_comment_timestamp(row.get("date"))
    if timestamp is None:
        return False
    if filters.boundary_from is not None and timestamp < filters.boundary_from:
        return False
    return not (filters.boundary_to is not None and timestamp > filters.boundary_to)


def _matches_keywords(row: dict[str, Any], keyword_needles: tuple[str, ...]) -> bool:
    if not keyword_needles:
        return True
    body = str(row.get("body") or "").lower()
    return all(needle in body for needle in keyword_needles)


def filter_raw_comments(
    comments: list[dict[str, Any]],
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    min_replies: int | None = None,
    author: str | None = None,
    keywords: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    filters = _CommentFilters(
        boundary_from=_parse_boundary_timestamp(date_from, end_of_day=False) if date_from else None,
        boundary_to=_parse_boundary_timestamp(date_to, end_of_day=True) if date_to else None,
        min_replies=min_replies,
        author_needle=author.strip().lower() if isinstance(author, str) and author.strip() else None,
        keyword_needles=tuple(part.strip().lower() for part in keywords if part.strip()),
    )
    filtered = [
        row
        for row in comments
        if _has_enough_replies(row, filters.min_replies)
        and _matches_author(row, filters.author_needle)
        and _within_window(row, filters)
        and _matches_keywords(row, filters.keyword_needles)
    ]
    return filtered, {
        "date_from": date_from,
        "date_to": date_to,
        "min_replies": min_replies,
        "author": author,
        "keywords": list(filters.keyword_needles),
    }


def _body_signature(body: Any) -> str:
    text = str(body or "").lower()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())[:240]


def detect_near_duplicate_groups(
    comments: list[dict[str, Any]],
    *,
    page_url: str,
    max_groups: int = 10,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in comments:
        signature = _body_signature(row.get("body"))
        if len(signature) < 24:
            continue
        buckets.setdefault(signature, []).append(row)

    groups: list[dict[str, Any]] = []
    for signature, rows in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(rows) < 2:
            continue
        comment_ids = [int(row["id"]) for row in rows if isinstance(row.get("id"), int)]
        groups.append(
            {
                "signature": signature,
                "comment_count": len(comment_ids),
                "comment_ids": comment_ids,
                "citations": [
                    {
                        "comment_id": comment_id,
                        "citation_url": canonical_comment_url(page_url, comment_id),
                    }
                    for comment_id in comment_ids
                ],
            }
        )
        if len(groups) >= max_groups:
            break

    return {
        "group_count": len(groups),
        "groups": groups,
        "caveat": "Near-duplicate groups use normalized body text signatures; paraphrases may not match.",
    }


def _freshness_summary(comments: list[dict[str, Any]], *, sampled_at: datetime) -> dict[str, Any]:
    timestamps = [_parse_comment_timestamp(row.get("date")) for row in comments]
    valid = [value for value in timestamps if value is not None]
    if not valid:
        return {
            "sampled_at": sampled_at.isoformat(),
            "comment_count": 0,
            "caveat": "No parseable comment timestamps were available in the filtered sample.",
        }

    ages_days = sorted((sampled_at - value).total_seconds() / 86400 for value in valid)
    return {
        "sampled_at": sampled_at.isoformat(),
        "comment_count": len(valid),
        "newest_at": max(valid).isoformat(),
        "oldest_at": min(valid).isoformat(),
        "median_age_days": round(median(ages_days), 2),
        "min_age_days": round(ages_days[0], 2),
        "max_age_days": round(ages_days[-1], 2),
    }


def _insight_row(
    *,
    kind: str,
    row: dict[str, Any],
    page_url: str,
    summary: str,
) -> dict[str, Any]:
    comment_id = row.get("id")
    citation_url = canonical_comment_url(page_url, comment_id) if isinstance(comment_id, int) else None
    return {
        "kind": kind,
        "comment_id": comment_id,
        "user": row.get("user"),
        "date": row.get("date"),
        "rating": row.get("rating"),
        "nreplies": row.get("nreplies"),
        "summary": summary,
        "citation_url": citation_url,
    }


def build_comment_insights(
    comments: list[dict[str, Any]],
    *,
    page_url: str,
    insight_limit: int = 5,
) -> list[dict[str, Any]]:
    if not comments:
        return []

    insights: list[dict[str, Any]] = []

    top_rated = max(
        comments,
        key=lambda row: (
            int(row.get("rating") or 0) if isinstance(row.get("rating"), int) else 0,
            _parse_comment_timestamp(row.get("date")) or datetime.min.replace(tzinfo=UTC),
        ),
    )
    insights.append(
        _insight_row(
            kind="top_rated",
            row=top_rated,
            page_url=page_url,
            summary="Highest-rated comment in the filtered sample.",
        )
    )

    most_replies = max(
        comments,
        key=lambda row: (
            int(row.get("nreplies") or 0) if isinstance(row.get("nreplies"), int) else 0,
            int(row.get("rating") or 0) if isinstance(row.get("rating"), int) else 0,
        ),
    )
    seen_ids = {top_rated.get("id")}
    if most_replies.get("id") not in seen_ids:
        seen_ids.add(most_replies.get("id"))
        insights.append(
            _insight_row(
                kind="most_replies",
                row=most_replies,
                page_url=page_url,
                summary="Comment with the largest reply count in the filtered sample.",
            )
        )

    newest = max(comments, key=lambda row: _parse_comment_timestamp(row.get("date")) or datetime.min.replace(tzinfo=UTC))
    if newest.get("id") not in seen_ids:
        insights.append(
            _insight_row(
                kind="newest",
                row=newest,
                page_url=page_url,
                summary="Most recent comment in the filtered sample.",
            )
        )

    return insights[: max(1, insight_limit)]


def build_comments_intelligence(
    *,
    page_url: str,
    embedded_total: int,
    filtered_comments: list[dict[str, Any]],
    filters: dict[str, Any],
    insight_limit: int,
) -> dict[str, Any]:
    sampled_at = datetime.now(UTC)
    return {
        "sample": {
            "embedded_total": embedded_total,
            "filtered_count": len(filtered_comments),
            "filters": filters,
            "citations": {"comments_page": f"{page_url}#comments"},
        },
        "freshness": _freshness_summary(filtered_comments, sampled_at=sampled_at),
        "near_duplicates": detect_near_duplicate_groups(filtered_comments, page_url=page_url),
        "insights": build_comment_insights(filtered_comments, page_url=page_url, insight_limit=insight_limit),
        "caveat": "Insights are deterministic summaries over the filtered comment sample, not sentiment or consensus scores.",
    }
