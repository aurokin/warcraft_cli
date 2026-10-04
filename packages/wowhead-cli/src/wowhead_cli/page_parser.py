from __future__ import annotations

import json
import re
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

from wowhead_cli.entity_types import PARSER_ENTITY_TYPES
from wowhead_cli.expansion_profiles import ENTITY_PATH_RE, EXPANSION_PREFIXES
from wowhead_cli.wowhead_client import WOWHEAD_BASE_URL, entity_url

GATHERER_TYPE_TO_ENTITY: dict[int, str] = {
    1: "npc",
    2: "object",
    3: "item",
    5: "quest",
    6: "spell",
}

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
# An entity page's relation tabs: `new Listview({template: 'npc', id: 'members', ..., data:[{...}]})`.
# The header runs up to its inline `data:[` array without crossing into the next Listview.
LISTVIEW_RE = re.compile(r"""new Listview\(\{(?P<head>(?:(?!new Listview\().){0,4000}?)\bdata:\s*(?=\[)""", re.DOTALL)
LISTVIEW_TEMPLATE_RE = re.compile(r"""\btemplate:\s*['"](?P<value>[^'"]+)['"]""")
LISTVIEW_ID_RE = re.compile(r"""\bid:\s*['"](?P<value>[^'"]+)['"]""")
ASSIGNMENT_RE_TEMPLATE = r"""\bvar\s+{name}\s*="""
GATHERER_RE = re.compile(r"""WH\.Gatherer\.addData\(\s*(?P<dtype>\d+)\s*,\s*(?P<tree>\d+)\s*,\s*""")
SCRIPT_ID_TEMPLATE = r"""<script\b[^>]*\bid=["']{script_id}["'][^>]*>(?P<body>.*?)</script>"""
GUIDE_HEADING_RE = re.compile(r"""\[(?P<tag>h[1-6])\b[^\]]*\](?P<body>.*?)\[/\1\]""", re.IGNORECASE | re.DOTALL)
WOWHEAD_URL_TAG_RE = re.compile(
    r"""\[url(?:=(?P<url1>[^\]]+)|\s+guide=(?P<guide_id>\d+))\](?P<label>.*?)\[/url\]""", re.IGNORECASE | re.DOTALL)
INLINE_TAG_RE = re.compile(r"""\[[^\]]+\]""")
MARKUP_ENTITY_TOKEN_RE = re.compile(r"""\[(?P<etype>[a-z-]+)=(?P<eid>\d+)[^\]]*\]""")
# `[build title="..." stats=...]`, `[key-talents="Hero" spells=1,2,3]`, `[build-items bis=1,2 alt=3]`
MARKUP_BUILD_TAG_RE = re.compile(r"""\[(?P<tag>build|key-talents|build-items)(?P<attrs>[\s=][^\]]*)\]""")
MARKUP_ATTR_RE = re.compile(r"""(?P<key>[a-z-]*)=(?:"(?P<quoted>[^"]*)"|(?P<bare>[^\s\]]+))""")
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


def parse_page_metadata(html_text: str, *, fallback_url: str | None) -> dict[str, str | None]:
    canonical = _first_group(CANONICAL_RE, html_text) or fallback_url
    og_title = _first_group(META_OG_TITLE_RE, html_text)
    description = _first_group(META_DESCRIPTION_RE, html_text)
    return {
        "canonical_url": unescape(canonical) if canonical else None,
        "title": unescape(og_title) if og_title else None,
        "description": unescape(description) if description else None,
    }


PAGE_ERROR_RE = re.compile(r"""<div id=["']inputbox-error["']>(?P<message>.*?)</div>""", re.IGNORECASE | re.DOTALL)


def parse_page_error(html_text: str) -> str | None:
    """The message of Wowhead's error page, which it serves with HTTP 200 (a missing profiler list)."""
    match = PAGE_ERROR_RE.search(html_text)
    return unescape(match["message"]).strip() if match else None


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


MARKUP_CALL_MARKER = "WH.markup.printHtml("
_PAGE_DATA_MARKER = "WH.getPageData("


def _skip_whitespace(text: str, cursor: int) -> int:
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    return cursor


def _markup_payload_from_page_data(html_text: str, cursor: int) -> tuple[str | None, int] | None:
    """Resolve ``WH.getPageData("key")`` to the embedded markup string; None when the call is malformed."""
    cursor = _skip_whitespace(html_text, cursor + len(_PAGE_DATA_MARKER))
    try:
        data_key, offset = JSON_DECODER.raw_decode(html_text[cursor:])
    except json.JSONDecodeError:
        return None
    cursor = _skip_whitespace(html_text, cursor + offset)
    if cursor >= len(html_text) or html_text[cursor] != ")":
        return None
    cursor += 1
    if not isinstance(data_key, str):
        return None
    try:
        parsed = extract_json_script(html_text, f"data.{data_key}")
    except (ValueError, json.JSONDecodeError):
        return None
    return (parsed if isinstance(parsed, str) else None), cursor


def _markup_payload_literal(html_text: str, cursor: int) -> tuple[str | None, int] | None:
    """Read an inline JSON string argument; None when it is not valid JSON."""
    try:
        parsed, offset = JSON_DECODER.raw_decode(html_text[cursor:])
    except json.JSONDecodeError:
        return None
    return (parsed if isinstance(parsed, str) else None), cursor + offset


def _markup_call_target(html_text: str, cursor: int) -> tuple[Any] | None:
    """Read the second ``WH.markup.printHtml`` argument, wrapped in a 1-tuple so ``None`` stays a value."""
    cursor = _skip_whitespace(html_text, cursor)
    if cursor >= len(html_text) or html_text[cursor] != ",":
        return None
    cursor = _skip_whitespace(html_text, cursor + 1)
    try:
        found_target, _offset = JSON_DECODER.raw_decode(html_text[cursor:])
    except json.JSONDecodeError:
        return None
    return (found_target,)


def extract_markup_by_target(html_text: str, *, target: str) -> str | None:
    """Return the markup string that ``WH.markup.printHtml`` renders into ``target``, if the page has one."""
    start = 0
    while True:
        index = html_text.find(MARKUP_CALL_MARKER, start)
        if index < 0:
            return None
        start = index + len(MARKUP_CALL_MARKER)
        cursor = _skip_whitespace(html_text, start)
        if html_text.startswith(_PAGE_DATA_MARKER, cursor):
            parsed = _markup_payload_from_page_data(html_text, cursor)
        else:
            parsed = _markup_payload_literal(html_text, cursor)
        if parsed is None:
            continue
        payload, cursor = parsed
        found = _markup_call_target(html_text, cursor)
        if found is not None and found[0] == target and payload is not None:
            return payload


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


def entity_names(records: list[dict[str, Any]]) -> dict[tuple[str, int], str]:
    """Map ``(entity_type, id)`` to the name each linked-entity record carries, for ``guide_markup_text``."""
    return {
        (record["entity_type"], record["id"]): record["name"]
        for record in records
        if isinstance(record.get("name"), str) and record["name"]
    }


def _render_build_tag(match: re.Match[str], names: dict[tuple[str, int], str]) -> str:
    """Spell out a build block's title, stat priority, and the talents and items it lists by id."""
    attrs = {row["key"]: row["quoted"] or row["bare"] or "" for row in MARKUP_ATTR_RE.finditer(match.group("attrs"))}
    id_type = "spell" if match.group("tag") == "key-talents" else "item"
    listed = [
        names[(id_type, int(entity_id))]
        for key in ("spells", "bis", "alt", "list")
        for entity_id in re.findall(r"(?:^|,)(\d+)", attrs.get(key, ""))
        if (id_type, int(entity_id)) in names
    ]
    parts = [attrs.get("title", ""), attrs.get("", ""), f"stats {attrs['stats']}" if "stats" in attrs else "", *listed]
    return " " + ", ".join(part for part in parts if part) + " "


def guide_markup_text(markup_text: str, names: dict[tuple[str, int], str]) -> str:
    """Plain text of guide markup that keeps what its tags name, unlike ``clean_markup_text``.

    ``[spell=184367]`` becomes the entity's name, and build blocks keep their title, stat priority,
    key talents, and listed items. A token whose entity has no known name is dropped.
    """
    text = MARKUP_BUILD_TAG_RE.sub(lambda match: _render_build_tag(match, names), markup_text)
    text = MARKUP_ENTITY_TOKEN_RE.sub(
        lambda match: f" {names.get((match.group('etype'), int(match.group('eid'))), '')} ", text
    )
    return clean_markup_text(text)


def extract_guide_section_chunks(
    markup_text: str, names: dict[tuple[str, int], str] | None = None
) -> list[dict[str, Any]]:
    """Split guide markup at its headings; ``names`` (from ``entity_names``) fills in inline entity tokens."""
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
                "content_text": guide_markup_text(raw_content, names or {}),
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


LISTVIEW_ROW_FIELDS = ("count", "outof", "cost", "stock")


def extract_listview_entities(html_text: str, *, source_url: str) -> list[dict[str, Any]]:
    """Entities an entity page lists in its relation tabs (a zone's NPCs and quests, a faction's members).

    Only tabs whose template is an entity type the CLI reads count (guide tabs included);
    screenshots, sounds and models do not. A tab whose data is not a JSON array is skipped.
    A row's drop sample (``count`` of ``outof`` kills or opens) and vendor ``cost`` and ``stock``
    are kept as Wowhead sends them in ``listview_data``.
    """
    records: list[dict[str, Any]] = []
    for match in LISTVIEW_RE.finditer(html_text):
        template = LISTVIEW_TEMPLATE_RE.search(match.group("head"))
        listview_id = LISTVIEW_ID_RE.search(match.group("head"))
        if template is None or template.group("value") not in PARSER_ENTITY_TYPES:
            continue
        try:
            rows, _ = JSON_DECODER.raw_decode(html_text, match.end())
        except json.JSONDecodeError:
            continue
        entity_type = template.group("value")
        for row in rows if isinstance(rows, list) else []:
            entity_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(entity_id, int) or isinstance(entity_id, bool):
                continue
            url = _entity_url_for_source_context(source_url=source_url, entity_type=entity_type, entity_id=entity_id)
            record = {
                "entity_type": entity_type,
                "id": entity_id,
                "name": row.get("name") or row.get("displayName"),
                "url": url,
                "citation_url": url,
                "source_url": source_url,
                "source_kind": "listview",
                "listview": listview_id.group("value") if listview_id else None,
            }
            row_data = {field: row[field] for field in LISTVIEW_ROW_FIELDS if row.get(field) is not None}
            if row_data:
                record["listview_data"] = row_data
            records.append(record)
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


QUICK_FACTS_RE = re.compile(r"""<th>Quick Facts</th>.*?WH\.markup\.printHtml\("(?P<markup>(?:[^"\\]|\\.)*)\"""", re.DOTALL)
MARKUP_LI_RE = re.compile(r"""\[li\b[^\]]*\](?P<body>.*?)\[/li\]""", re.DOTALL)
MARKUP_TOOLTIP_RE = re.compile(r"""\[tooltip\b[^\]]*\].*?\[/tooltip\]""", re.DOTALL)
# `Start: [url=/npc=37120/...]Name[/url]`, or a bare token for an item-started quest: `Start: [item=12780]`.
QUICK_FACT_LINK_RE = re.compile(
    r"""\b(?P<role>Start|End):\s*(?:\[url=(?P<href>[^\]]+)\](?P<name>.*?)\[/url\]|\[(?P<etype>[a-z-]+)=(?P<eid>\d+)[^\]]*\])"""
)
# `React: [color=q10]A[/color] [color=q2]H[/color]`: the colour is how each faction's players are treated.
REACT_SIDE_RE = re.compile(r"""\[color=(?P<color>q\d*)\](?P<side>[AH])\[/color\]""")
REACT_COLORS = {"q10": "hostile", "q2": "friendly", "q": "neutral"}
REACT_SIDES = {"A": "Alliance", "H": "Horde"}
SERIES_TABLE_RE = re.compile(r"""<table class="series">(?P<body>.*?)</table>""", re.DOTALL)
SERIES_ROW_RE = re.compile(r"""<tr><th>(?P<position>\d+)\.</th><td>(?P<body>.*?)</td></tr>""", re.DOTALL)
SERIES_STEP_RE = re.compile(r"""<a href="(?P<href>[^"]+)">(?P<link>.*?)</a>|<b>(?P<current>.*?)</b>""", re.DOTALL)
LOCATIONS_SPAN_RE = re.compile(r"""<span id="locations">(?P<body>.*?)</span>""", re.DOTALL)
LOCATION_LINK_RE = re.compile(r"""zone:\s*(?P<zone>\d+),.*?>(?P<name>[^<]+)</a>""", re.DOTALL)


def _linked_entity(href: str, name: str) -> dict[str, Any] | None:
    parsed = _parse_entity_ref(href)
    if parsed is None:
        return None
    entity_type, entity_id, url = parsed
    return {"type": entity_type, "id": entity_id, "name": clean_markup_text(name) or None, "url": url}


def _quick_fact_text(body: str) -> str:
    """One Quick Facts line as text; an icon-only line ("Icon: [icondb=...]") reads as empty."""
    if "[icondb=" in body:
        return ""
    sides = REACT_SIDE_RE.findall(body)
    if sides:
        return "React: " + ", ".join(f"{REACT_SIDES[side]} {REACT_COLORS.get(color, color)}" for color, side in sides)
    # `[class=1]` and `[race=30]` render as names on the site; keep them as "class 1".
    text = MARKUP_ENTITY_TOKEN_RE.sub(
        lambda token: token["eid"] if token["etype"] == "achievementpoints" else f"{token['etype']} {token['eid']} ", body
    )
    return clean_markup_text(text).replace(" ,", ",")


def _quick_facts(html_text: str) -> dict[str, Any]:
    """The infobox's Quick Facts lines as text, plus a quest's start and end entities."""
    match = QUICK_FACTS_RE.search(html_text)
    if match is None:
        return {}
    try:
        markup = json.loads(f'"{match.group("markup")}"')
    except json.JSONDecodeError:
        return {}
    facts: dict[str, Any] = {"quick_facts": []}
    for item in MARKUP_LI_RE.finditer(markup):
        body = re.sub(r"""\[img\b[^\]]*\]""", "", MARKUP_TOOLTIP_RE.sub("", item.group("body"))).strip()
        text = _quick_fact_text(body)
        if text:
            facts["quick_facts"].append(text)
        link = QUICK_FACT_LINK_RE.search(body)
        if link is None or link["role"].lower() in facts:
            continue
        linked = _linked_entity(link["href"], link["name"]) if link["href"] else _linked_entity(f"/{link['etype']}={link['eid']}", "")
        if linked:
            facts[link["role"].lower()] = linked
    return facts


def _series(html_text: str, *, page_url: str, page_entity_type: str, page_entity_id: int) -> list[list[dict[str, Any]]]:
    """Each chain the page's Series boxes list (quests, achievements), in order; the page's own entity is ``current``."""
    chains: list[list[dict[str, Any]]] = []
    for table in SERIES_TABLE_RE.finditer(html_text):
        chain: list[dict[str, Any]] = []
        for row in SERIES_ROW_RE.finditer(table.group("body")):
            for step in SERIES_STEP_RE.finditer(row.group("body")):
                linked = _linked_entity(step["href"], step["link"]) if step["href"] else None
                if linked is None:
                    linked = {
                        "type": page_entity_type,
                        "id": page_entity_id,
                        "name": clean_markup_text(step["current"] or "") or None,
                        "url": _entity_url_for_source_context(
                            source_url=page_url, entity_type=page_entity_type, entity_id=page_entity_id
                        ),
                    }
                chain.append({"position": int(row.group("position")), **linked, "current": step["href"] is None})
        if chain:
            chains.append(chain)
    return chains


def _locations(html_text: str, *, coords: bool) -> list[dict[str, Any]]:
    """Where an NPC or object is placed: each zone and map floor from ``g_mapperData``, with its coordinates
    when `coords` is set (a common node lists thousands).

    Retail keys each zone's floors by number; classic lists them, each naming its own map.
    """
    try:
        mapper = extract_json_assignment(html_text, "g_mapperData")
    except ValueError:
        return []
    span = LOCATIONS_SPAN_RE.search(html_text)
    links = LOCATION_LINK_RE.finditer(span.group("body")) if span else iter(())
    names = {int(link["zone"]): unescape(link["name"]).strip() for link in links}
    locations: list[dict[str, Any]] = []
    for zone_id, levels in mapper.items() if isinstance(mapper, dict) else []:
        floors = levels.items() if isinstance(levels, dict) else enumerate(levels) if isinstance(levels, list) else ()
        for level, spot in floors:
            if not (zone_id.isdigit() and str(level).isdigit() and isinstance(spot, dict)):
                continue
            location = {
                "zone_id": int(zone_id),
                "zone": spot.get("uiMapName") or names.get(int(zone_id)),
                "level": int(level),
                "count": spot.get("count"),
            }
            locations.append({**location, "coords": spot.get("coords") or []} if coords else location)
    return locations


def extract_page_facts(
    html_text: str, *, page_url: str, page_entity_type: str, page_entity_id: int, coords: bool = True
) -> dict[str, Any] | None:
    """The entity page's infobox and map data: Quick Facts lines (side, level, patch), a quest's start
    and end entities, quest and achievement chains, and an NPC's or object's zones and coordinates.

    Keys appear only when the page carries them; a page with none returns None. An item page's map
    shows its vendors or drop spots rather than the item, so only NPC and object pages get locations.
    """
    facts = _quick_facts(html_text)
    series = _series(html_text, page_url=page_url, page_entity_type=page_entity_type, page_entity_id=page_entity_id)
    if series:
        facts["series"] = series
    locations = _locations(html_text, coords=coords) if page_entity_type in ("npc", "object") else []
    if locations:
        facts["locations"] = locations
    return facts or None
