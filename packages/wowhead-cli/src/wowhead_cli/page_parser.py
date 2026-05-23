from __future__ import annotations

import json
import re
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

from wowhead_cli.entity_types import PARSER_ENTITY_TYPES
from wowhead_cli.expansion_profiles import list_profiles
from wowhead_cli.wowhead_client import WOWHEAD_BASE_URL, entity_url

GATHERER_TYPE_TO_ENTITY: dict[int, str] = {
    1: "npc",
    2: "object",
    3: "item",
    5: "quest",
    6: "spell",
}

EXPANSION_PREFIXES = frozenset(
    profile.path_prefix for profile in list_profiles() if profile.path_prefix
)

JSON_DECODER = json.JSONDecoder()

CANONICAL_RE = re.compile(
    r"""<link\b(?=[^>]*\brel=["']canonical["'])(?=[^>]*\bhref=["']([^"']+)["'])[^>]*>""",
    re.IGNORECASE,
)
META_OG_TITLE_RE = re.compile(
    r"""<meta\b(?=[^>]*\bproperty=["']og:title["'])(?=[^>]*\bcontent=["']([^"']+)["'])[^>]*>""",
    re.IGNORECASE,
)
META_DESCRIPTION_RE = re.compile(
    r"""<meta\b(?=[^>]*\bname=["']description["'])(?=[^>]*\bcontent=["']([^"']+)["'])[^>]*>""",
    re.IGNORECASE,
)
A_TAG_RE = re.compile(
    r"""<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)
HREF_RE = re.compile(r"""href=(["'])(?P<href>.*?)\1""", re.IGNORECASE)
ENTITY_PATH_RE = re.compile(
    r"""^/(?:(?:[a-z]{2}(?:-[A-Z]{2})?|[a-z0-9-]+)/)?(?P<etype>[a-z-]+)=(?P<eid>\d+)""",
)
ASSIGNMENT_RE_TEMPLATE = r"""\bvar\s+{name}\s*="""
GATHERER_RE = re.compile(r"""WH\.Gatherer\.addData\(\s*(?P<dtype>\d+)\s*,\s*(?P<tree>\d+)\s*,\s*""")
SCRIPT_ID_TEMPLATE = r"""<script\b[^>]*\bid=["']{script_id}["'][^>]*>(?P<body>.*?)</script>"""
GUIDE_HEADING_RE = re.compile(r"""\[(?P<tag>h[1-6])\b[^\]]*\](?P<body>.*?)\[/\1\]""", re.IGNORECASE | re.DOTALL)
WOWHEAD_URL_TAG_RE = re.compile(
    r"""\[url(?:=(?P<url1>[^\]]+)|\s+guide=(?P<guide_id>\d+))\](?P<label>.*?)\[/url\]""", re.IGNORECASE | re.DOTALL)
INLINE_TAG_RE = re.compile(r"""\[[^\]]+\]""")
HTML_TAG_RE = re.compile(r"""<[^>]+>""")
JSON_LD_RE = re.compile(
    r"""<script\b[^>]*\btype=["']application/ld\+json["'][^>]*>(?P<body>.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
GUIDE_RATING_RE = re.compile(r"""GetStars\(\s*(?P<score>\d+(?:\.\d+)?)\s*,""", re.IGNORECASE)
GUIDE_VOTES_RE = re.compile(
    r"""id=["']guiderating-votes["']>(?P<votes>\d+)""",
    re.IGNORECASE,
)


def canonical_comment_url(page_url: str, comment_id: int) -> str:
    return f"{page_url}#comments:id={comment_id}"


def parse_page_metadata(html_text: str, *, fallback_url: str) -> dict[str, str | None]:
    canonical = _first_group(CANONICAL_RE, html_text) or fallback_url
    og_title = _first_group(META_OG_TITLE_RE, html_text)
    description = _first_group(META_DESCRIPTION_RE, html_text)
    return {
        "canonical_url": unescape(canonical),
        "title": unescape(og_title) if og_title else None,
        "description": unescape(description) if description else None,
    }


def parse_page_meta_json(html_text: str) -> dict[str, Any] | None:
    marker = 'id="data.pageMeta">'
    start_marker = html_text.find(marker)
    if start_marker < 0:
        return None
    start = start_marker + len(marker)
    end = html_text.find("</script>", start)
    if end < 0:
        return None
    payload = html_text[start:end].strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def extract_json_script(html_text: str, script_id: str) -> Any:
    pattern = re.compile(
        SCRIPT_ID_TEMPLATE.format(script_id=re.escape(script_id)),
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html_text)
    if match is None:
        raise ValueError(f"Script for {script_id!r} not found.")
    return json.loads(match.group("body").strip())


def extract_listview_data(html_text: str, listview_id: str) -> list[dict[str, Any]]:
    marker = "new Listview("
    id_marker = f'"id":"{listview_id}"'
    start = 0
    while True:
        index = html_text.find(marker, start)
        if index < 0:
            raise ValueError(f"Listview for {listview_id!r} not found.")
        object_start = html_text.find("{", index)
        if object_start < 0:
            raise ValueError(f"Listview object for {listview_id!r} not found.")
        window = html_text[object_start: object_start + 4096]
        if id_marker not in window:
            start = index + len(marker)
            continue
        data_index = html_text.find('"data":', object_start)
        if data_index < 0:
            raise ValueError(f"Listview data array for {listview_id!r} not found.")
        array_start = html_text.find("[", data_index)
        if array_start < 0:
            raise ValueError(f"Listview data array for {listview_id!r} not found.")
        parsed, _offset = JSON_DECODER.raw_decode(html_text[array_start:])
        if not isinstance(parsed, list):
            raise ValueError(f"Listview data array for {listview_id!r} was not a JSON list.")
        return [row for row in parsed if isinstance(row, dict)]


def extract_json_ld(html_text: str) -> dict[str, Any] | list[Any] | None:
    match = JSON_LD_RE.search(html_text)
    if match is None:
        return None
    payload = match.group("body").strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list)):
        return parsed
    return None


def extract_markup_by_target(html_text: str, *, target: str) -> str | None:
    marker = "WH.markup.printHtml("
    start = 0
    while True:
        index = html_text.find(marker, start)
        if index < 0:
            return None
        cursor = index + len(marker)
        while cursor < len(html_text) and html_text[cursor].isspace():
            cursor += 1

        payload: str | None = None
        if html_text.startswith("WH.getPageData(", cursor):
            cursor += len("WH.getPageData(")
            while cursor < len(html_text) and html_text[cursor].isspace():
                cursor += 1
            try:
                data_key, offset = JSON_DECODER.raw_decode(html_text[cursor:])
            except json.JSONDecodeError:
                start = index + len(marker)
                continue
            cursor += offset
            while cursor < len(html_text) and html_text[cursor].isspace():
                cursor += 1
            if cursor >= len(html_text) or html_text[cursor] != ")":
                start = index + len(marker)
                continue
            cursor += 1
            if not isinstance(data_key, str):
                start = index + len(marker)
                continue
            try:
                parsed = extract_json_script(html_text, f"data.{data_key}")
            except (ValueError, json.JSONDecodeError):
                start = index + len(marker)
                continue
            if isinstance(parsed, str):
                payload = parsed
        else:
            try:
                parsed, offset = JSON_DECODER.raw_decode(html_text[cursor:])
            except json.JSONDecodeError:
                start = index + len(marker)
                continue
            cursor += offset
            if isinstance(parsed, str):
                payload = parsed

        while cursor < len(html_text) and html_text[cursor].isspace():
            cursor += 1
        if cursor >= len(html_text) or html_text[cursor] != ",":
            start = index + len(marker)
            continue
        cursor += 1
        while cursor < len(html_text) and html_text[cursor].isspace():
            cursor += 1
        try:
            found_target, offset = JSON_DECODER.raw_decode(html_text[cursor:])
        except json.JSONDecodeError:
            start = index + len(marker)
            continue
        cursor += offset
        if found_target == target and payload is not None:
            return payload
        start = index + len(marker)
    return None


def extract_guide_sections(markup_text: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for match in GUIDE_HEADING_RE.finditer(markup_text):
        tag = match.group("tag").lower()
        sections.append(
            {
                "level": int(tag[1]),
                "title": clean_markup_text(match.group("body")),
            }
        )
    return sections


def extract_guide_section_chunks(markup_text: str) -> list[dict[str, Any]]:
    matches = list(GUIDE_HEADING_RE.finditer(markup_text))
    chunks: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        tag = match.group("tag").lower()
        title = clean_markup_text(match.group("body"))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markup_text)
        raw_content = markup_text[start:end].strip()
        chunks.append(
            {
                "ordinal": index + 1,
                "level": int(tag[1]),
                "title": title,
                "content_raw": raw_content,
                "content_text": clean_markup_text(raw_content),
            }
        )
    return chunks


def extract_markup_urls(markup_text: str, *, source_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in WOWHEAD_URL_TAG_RE.finditer(markup_text):
        raw_url = match.group("url1")
        guide_id = match.group("guide_id")
        if guide_id:
            raw_url = f"guide={guide_id}"
        if not raw_url:
            continue
        absolute = urljoin(WOWHEAD_BASE_URL, raw_url)
        key = (absolute, clean_markup_text(match.group("label")))
        if key in seen:
            continue
        seen.add(key)
        links.append(
            {
                "label": key[1],
                "url": absolute,
                "source_url": source_url,
            }
        )
    return links


def extract_guide_rating(html_text: str) -> dict[str, int | float | None]:
    score: float | None = None
    votes: int | None = None
    score_match = GUIDE_RATING_RE.search(html_text)
    if score_match is not None:
        try:
            score = float(score_match.group("score"))
        except ValueError:
            score = None
    votes_match = GUIDE_VOTES_RE.search(html_text)
    if votes_match is not None:
        try:
            votes = int(votes_match.group("votes"))
        except ValueError:
            votes = None
    return {
        "score": score,
        "votes": votes,
    }


def clean_markup_text(value: str) -> str:
    text = HTML_TAG_RE.sub(" ", value)
    text = INLINE_TAG_RE.sub(" ", text)
    text = unescape(text)
    text = text.replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())


def extract_linked_entities_from_href(html_text: str, *, source_url: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for match in A_TAG_RE.finditer(html_text):
        href_match = HREF_RE.search(match.group("attrs"))
        if href_match is None:
            continue
        href_raw = unescape(href_match.group("href"))
        parsed = _parse_entity_ref(href_raw)
        if parsed is None:
            continue
        entity_type, entity_id, absolute_url = parsed
        key = (entity_type, entity_id)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "entity_type": entity_type,
                "id": entity_id,
                "name": clean_markup_text(match.group("body")) or None,
                "url": absolute_url,
                "citation_url": absolute_url,
                "source_url": source_url,
                "source_kind": "href",
            }
        )

    for match in HREF_RE.finditer(html_text):
        href_raw = unescape(match.group("href"))
        parsed = _parse_entity_ref(href_raw)
        if parsed is None:
            continue
        entity_type, entity_id, absolute_url = parsed
        key = (entity_type, entity_id)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "entity_type": entity_type,
                "id": entity_id,
                "name": None,
                "url": absolute_url,
                "citation_url": absolute_url,
                "source_url": source_url,
                "source_kind": "href",
            }
        )
    return records


def extract_gatherer_entities(html_text: str, *, source_url: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for match in GATHERER_RE.finditer(html_text):
        data_type = int(match.group("dtype"))
        json_start = match.end()
        try:
            parsed, _ = JSON_DECODER.raw_decode(html_text[json_start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue

        entity_type = GATHERER_TYPE_TO_ENTITY.get(data_type)
        if entity_type is None:
            continue

        for entity_id_raw, entity_payload in parsed.items():
            try:
                entity_id = int(entity_id_raw)
            except (TypeError, ValueError):
                continue
            key = (entity_type, entity_id)
            if key in seen:
                continue
            seen.add(key)
            name = None
            if isinstance(entity_payload, dict):
                name = entity_payload.get("name_enus") or entity_payload.get("name")
            entity_page_url = _entity_url_for_source_context(
                source_url=source_url,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            records.append(
                {
                    "entity_type": entity_type,
                    "id": entity_id,
                    "name": name,
                    "url": entity_page_url,
                    "citation_url": entity_page_url,
                    "source_url": source_url,
                    "source_kind": "gatherer",
                    "gatherer_data_type": data_type,
                }
            )
    return records


def extract_json_assignment(html_text: str, var_name: str) -> Any:
    pattern = re.compile(ASSIGNMENT_RE_TEMPLATE.format(name=re.escape(var_name)))
    match = pattern.search(html_text)
    if match is None:
        raise ValueError(f"Assignment for {var_name!r} not found.")
    index = match.end()
    while index < len(html_text) and html_text[index].isspace():
        index += 1
    parsed, _ = JSON_DECODER.raw_decode(html_text[index:])
    return parsed


def extract_comments_dataset(html_text: str) -> list[dict[str, Any]]:
    parsed = extract_json_assignment(html_text, "lv_comments0")
    if not isinstance(parsed, list):
        raise ValueError("lv_comments0 is not a list.")
    return [row for row in parsed if isinstance(row, dict)]


def normalize_comments(
    comments: list[dict[str, Any]],
    *,
    page_url: str,
    include_replies: bool,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []

    for row in comments:
        comment_id = row.get("id")
        if not isinstance(comment_id, int):
            continue

        replies = row.get("replies")
        normalized_replies: list[dict[str, Any]] = []
        if include_replies and isinstance(replies, list):
            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                reply_id = reply.get("id")
                if not isinstance(reply_id, int):
                    continue
                normalized_replies.append(
                    {
                        "id": reply_id,
                        "comment_id": comment_id,
                        "user": reply.get("username"),
                        "date": reply.get("creationdate"),
                        "rating": reply.get("rating"),
                        "body": reply.get("body"),
                        "citation_url": canonical_comment_url(page_url, comment_id),
                        "source_url": page_url,
                    }
                )

        normalized.append(
            {
                "id": comment_id,
                "number": row.get("number"),
                "user": row.get("user"),
                "date": row.get("date"),
                "rating": row.get("rating"),
                "body": row.get("body"),
                "nreplies": row.get("nreplies"),
                "deleted": row.get("deleted"),
                "outofdate": row.get("outofdate"),
                "citation_url": canonical_comment_url(page_url, comment_id),
                "source_url": page_url,
                "replies": normalized_replies if include_replies else [],
            }
        )
    return normalized


def sort_comments(comments: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    if mode == "rating":
        return sorted(
            comments,
            key=lambda row: (_int_or_zero(row.get("rating")), _safe_iso_ts(row.get("date"))),
            reverse=True,
        )
    if mode == "oldest":
        return sorted(comments, key=lambda row: _safe_iso_ts(row.get("date")))
    return sorted(comments, key=lambda row: _safe_iso_ts(row.get("date")), reverse=True)


def _int_or_zero(value: Any) -> int:
    if isinstance(value, int):
        return value
    return 0


def _safe_iso_ts(value: Any) -> float:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    return float("-inf")


def _parse_entity_ref(href: str) -> tuple[str, int, str] | None:
    if not href or href.startswith("#"):
        return None
    absolute = urljoin(WOWHEAD_BASE_URL, href)
    parsed = urlparse(absolute)
    host = parsed.hostname or ""
    if host and "wowhead.com" not in host:
        return None
    match = ENTITY_PATH_RE.match(parsed.path)
    if match is None:
        return None
    entity_type = match.group("etype")
    if entity_type not in PARSER_ENTITY_TYPES:
        return None
    entity_id = int(match.group("eid"))
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts[0] in EXPANSION_PREFIXES:
        canonical = f"{WOWHEAD_BASE_URL}/{path_parts[0]}/{entity_type}={entity_id}"
    else:
        canonical = entity_url(entity_type, entity_id)
    return entity_type, entity_id, canonical


def _entity_url_for_source_context(*, source_url: str, entity_type: str, entity_id: int) -> str:
    parsed = urlparse(source_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts[0] in EXPANSION_PREFIXES:
        return f"{WOWHEAD_BASE_URL}/{path_parts[0]}/{entity_type}={entity_id}"
    return entity_url(entity_type, entity_id)


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    return match.group(1)
