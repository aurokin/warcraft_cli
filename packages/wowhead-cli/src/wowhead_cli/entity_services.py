"""Output-free entity retrieval and normalization services."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from warcraft_core.provider import ProviderError

from wowhead_cli import provider
from wowhead_cli.entities import (
    entity_comments_payload,
    entity_linked_entities_payload,
    entity_page_needs_fetch,
)
from wowhead_cli.entity_types import (
    ENTITY_TYPE_KEYS,
)
from wowhead_cli.expansion_profiles import (
    ExpansionProfile,
    expansion_url_policy_issues,
)
from wowhead_cli.normalization import attach_entity_normalization
from wowhead_cli.page_parser import (
    clean_markup_text,
    extract_page_facts,
    parse_page_metadata,
)
from wowhead_cli.wowhead_client import (
    WowheadClient,
    entity_url,
)

BRACKET_FRAGMENT_RE = re.compile(r"""\[[^\]]*\]""")

ADJACENT_SENTENCE_RE = re.compile(r"""(?P<sentence>[A-Z][^.?!]{8,}?)(?:[.?!])\s+(?P=sentence)(?:[.?!])""")

FLAVOR_QUOTE_RE = re.compile(r'''\s*"[^"]{20,}"''')

PAREN_OPEN_SPACE_RE = re.compile(r"""\(\s+""")

PAREN_CLOSE_SPACE_RE = re.compile(r"""\s+\)""")

PLUS_STAT_RE = re.compile(r"""(?<!\S)\+\s+(\d)""")

MONEY_SPAN_RE = re.compile(r"""<span class="money(?P<unit>gold|silver|copper)">(?P<amount>[^<]*)</span>""")

CURRENCY_LINK_RE = re.compile(
    r"""<a\b[^>]*\bhref="[^"]*/currency=\d+[^"]*"[^>]*\baria-label="(?P<name>[^"]+)"[^>]*>.*?</a>""",
    re.DOTALL,
)

TOOLTIP_SUMMARY_MARKERS = (
    "Use:",
    "Chance on hit:",
    "Equip:",
    "Chance on strike:",
    "Chance on melee hit:",
)

TOOLTIP_DESCRIPTION_MARKERS = (
    "A ",
    "An ",
    "Calls forth",
    "Blasts",
    "Deals",
    "Heals",
    "Summons",
    "Teleport",
    "Help ",
)

TOOLTIP_METADATA_TERMS = (
    "Talent",
    "Passive",
    "Instant",
    "Requires",
    "Range",
    "Melee",
    "Cooldown",
    "Cast",
    "Runes",
)

SENTENCE_END_RE = re.compile(r"""[.?!](?:\s|$)""")

@dataclass(frozen=True, slots=True)
class EntityConfig:
    """Expansion selection needed by entity retrieval, independent of CLI presentation."""
    expansion: ExpansionProfile
    expansion_source: str = "default"


@dataclass(slots=True)
class EntityAccessPlan:
    requested_type: str
    requested_id: int
    page_entity_type: str
    page_entity_id: int
    tooltip_entity_type: str | None
    tooltip_entity_id: int | None
    tooltip_from_page_metadata: bool = False
    page_from_tooltip_redirect: bool = False

def _build_entity_access_plan(entity_type: str, entity_id: int) -> EntityAccessPlan:
    normalized_type = entity_type.strip().lower()
    if normalized_type == "faction":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type="faction",
            page_entity_id=entity_id,
            tooltip_entity_type=None,
            tooltip_entity_id=None,
            tooltip_from_page_metadata=True,
        )
    if normalized_type == "pet":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type="pet",
            page_entity_id=entity_id,
            tooltip_entity_type=None,
            tooltip_entity_id=None,
            tooltip_from_page_metadata=True,
        )
    if normalized_type == "recipe":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type="spell",
            page_entity_id=entity_id,
            tooltip_entity_type="spell",
            tooltip_entity_id=entity_id,
        )
    if normalized_type == "mount":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type=normalized_type,
            page_entity_id=entity_id,
            tooltip_entity_type="mount",
            tooltip_entity_id=entity_id,
            page_from_tooltip_redirect=True,
        )
    if normalized_type == "battle-pet":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type=normalized_type,
            page_entity_id=entity_id,
            tooltip_entity_type="battle-pet",
            tooltip_entity_id=entity_id,
            page_from_tooltip_redirect=True,
        )
    return EntityAccessPlan(
        requested_type=normalized_type,
        requested_id=entity_id,
        page_entity_type=normalized_type,
        page_entity_id=entity_id,
        tooltip_entity_type=normalized_type,
        tooltip_entity_id=entity_id,
    )

def _parse_tooltip_final_ref(final_url: str) -> tuple[str, int] | None:
    parsed = urlparse(final_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0] != "tooltip":
        return None
    entity_type = parts[1]
    raw_id = parts[2]
    if not raw_id.isdigit():
        return None
    return entity_type, int(raw_id)

def _build_tooltip_from_page_metadata(metadata: dict[str, str | None]) -> tuple[str | None, dict[str, Any]]:
    title = metadata.get("title")
    description = metadata.get("description")
    parts = [part.strip() for part in (title, description) if isinstance(part, str) and part.strip()]
    payload: dict[str, Any] = {}
    if parts:
        entity_name = title if isinstance(title, str) and title.strip() else None
        tooltip_text = _clean_tooltip_text(" ".join(parts))
        payload["text"] = tooltip_text
        tooltip_summary = _build_tooltip_summary(tooltip_text, entity_name=entity_name)
        if tooltip_summary:
            payload["summary"] = tooltip_summary
    return title if isinstance(title, str) and title.strip() else None, payload

def _expansion_policy_notes(profile: ExpansionProfile, *urls: str | None) -> list[str]:
    notes: list[str] = []
    for url in urls:
        notes.extend(expansion_url_policy_issues(url, profile=profile))
    return notes

def _label_tooltip_money(html: str) -> str:
    """Spell out the units Wowhead marks only in markup: money spans and currency icon links.

    Flattening the HTML drops both, so ``87 50`` silver/copper or a ``180`` ticket cost would read as bare numbers.
    """
    html = MONEY_SPAN_RE.sub(lambda match: f"{match.group('amount')}{match.group('unit')[0]}", html)
    return CURRENCY_LINK_RE.sub(lambda match: f" {match.group('name')}", html)

def _clean_tooltip_text(text: str) -> str:
    cleaned = BRACKET_FRAGMENT_RE.sub(" ", text)
    cleaned = FLAVOR_QUOTE_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("[", " ").replace("]", " ")
    cleaned = PAREN_OPEN_SPACE_RE.sub("(", cleaned)
    cleaned = PAREN_CLOSE_SPACE_RE.sub(")", cleaned)
    cleaned = PLUS_STAT_RE.sub(r"+\1", cleaned)
    cleaned = cleaned.replace(" .", ".").replace(" ,", ",")
    cleaned = " ".join(cleaned.split())
    while True:
        collapsed = ADJACENT_SENTENCE_RE.sub(r"\g<sentence>.", cleaned)
        if collapsed == cleaned:
            break
        cleaned = collapsed
    return cleaned.strip()

def _strip_leading_entity_name(text: str, *, entity_name: str | None) -> str:
    if not entity_name:
        return text
    if not text.startswith(entity_name):
        return text
    remainder = text[len(entity_name):].lstrip(" :-")
    return remainder.strip() or text

def _prefer_tooltip_summary_span(text: str) -> str:
    best_index: int | None = None
    for marker in TOOLTIP_SUMMARY_MARKERS:
        index = text.find(marker)
        if index <= 0:
            continue
        if best_index is None or index < best_index:
            best_index = index
    if best_index is None:
        return text
    prefix = text[:best_index].strip()
    if len(prefix) < 24:
        return text
    return text[best_index:].strip()

def _prefer_first_summary_sentence(text: str) -> str:
    match = SENTENCE_END_RE.search(text)
    if match is None:
        return text
    sentence = text[: match.end()].strip()
    if len(sentence) < 24:
        return text
    return sentence

def _prefer_descriptive_summary_span(text: str) -> str:
    if any(text.startswith(marker) for marker in TOOLTIP_SUMMARY_MARKERS):
        return text
    prefix_lower = text.lower()
    if not any(term.lower() in prefix_lower for term in TOOLTIP_METADATA_TERMS):
        return text

    best_index: int | None = None
    for marker in TOOLTIP_DESCRIPTION_MARKERS:
        index = text.find(marker)
        if index < 12:
            continue
        if best_index is None or index < best_index:
            best_index = index

    if best_index is None:
        return text
    return text[best_index:].strip()

def _build_tooltip_summary(text: str, *, entity_name: str | None, max_chars: int = 220) -> str | None:
    if not text:
        return None
    summary = _strip_leading_entity_name(text, entity_name=entity_name)
    summary = _prefer_tooltip_summary_span(summary)
    summary = _prefer_descriptive_summary_span(summary)
    summary = _prefer_first_summary_sentence(summary)
    if len(summary) <= max_chars:
        return summary
    clipped = summary[: max_chars - 3]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" ,;:-") + "..."

def _normalize_tooltip_payload(tooltip: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    entity_name = tooltip.get("name")
    name = entity_name if isinstance(entity_name, str) and entity_name.strip() else None

    tooltip_payload = dict(tooltip)
    tooltip_html = tooltip_payload.pop("tooltip", None)
    tooltip_payload.pop("name", None)

    if isinstance(tooltip_html, str):
        tooltip_payload["html"] = tooltip_html
        tooltip_text = _clean_tooltip_text(clean_markup_text(_label_tooltip_money(tooltip_html)))
        tooltip_payload["text"] = tooltip_text
        tooltip_summary = _build_tooltip_summary(tooltip_text, entity_name=name)
        if tooltip_summary:
            tooltip_payload["summary"] = tooltip_summary
    elif isinstance(tooltip_payload.get("text"), str):
        tooltip_text = _clean_tooltip_text(str(tooltip_payload["text"]))
        tooltip_payload["text"] = tooltip_text
        tooltip_summary = _build_tooltip_summary(tooltip_text, entity_name=name)
        if tooltip_summary:
            tooltip_payload["summary"] = tooltip_summary

    return name, tooltip_payload

def _entity_page(client: WowheadClient, entity_type: str, entity_id: int) -> tuple[str, dict[str, str | None]]:
    """Fetch an entity page and its metadata; a failed request raises ``ProviderError``."""
    with provider.transport_errors():
        html = client.entity_page_html(entity_type, entity_id)
    metadata = parse_page_metadata(html, fallback_url=entity_url(entity_type, entity_id, expansion=client.expansion))
    canonical_url = metadata.get("canonical_url") or ""
    # Wowhead sends `/items=19019` to its item listing rather than answering 404; a real page of a
    # type the table lacks keeps the id in its canonical URL (`/title/private-1`).
    if entity_type not in ENTITY_TYPE_KEYS and re.search(rf"(?<!\d){entity_id}(?!\d)", urlparse(canonical_url).path) is None:
        raise provider.unknown_entity_type_error(entity_type, details={"canonical_url": canonical_url})
    return html, metadata

def _entity_tooltip(client: WowheadClient, plan: EntityAccessPlan, *, data_env: int | None) -> tuple[dict[str, Any], str | None]:
    """Fetch the tooltip the access plan points at; a failed or unreadable response raises ``ProviderError``."""
    if plan.tooltip_entity_type is None or plan.tooltip_entity_id is None:
        return {}, None
    try:
        with provider.transport_errors():
            if plan.page_from_tooltip_redirect:
                return client.tooltip_with_metadata(plan.tooltip_entity_type, plan.tooltip_entity_id, data_env=data_env)
            return client.tooltip(plan.tooltip_entity_type, plan.tooltip_entity_id, data_env=data_env), None
    except ValueError as exc:
        raise ProviderError("parse_failed", str(exc)) from exc

def _tooltip_and_page_plan(
    client: WowheadClient, entity_type: str, entity_id: int, *, data_env: int | None = None
) -> tuple[EntityAccessPlan, dict[str, Any]]:
    """The entity's tooltip and access plan, with the page target a mount or battle-pet tooltip redirects to.

    A failed or unreadable response raises ``ProviderError``.
    """
    plan = _build_entity_access_plan(entity_type, entity_id)
    tooltip, tooltip_final_url = _entity_tooltip(client, plan, data_env=data_env)
    if plan.page_from_tooltip_redirect and tooltip_final_url is not None:
        resolved = _parse_tooltip_final_ref(tooltip_final_url)
        if resolved is None:
            raise ProviderError("invalid_response", f"Could not resolve a page target for {entity_type} {entity_id}.")
        plan.page_entity_type, plan.page_entity_id = resolved
    return plan, tooltip

def _entity_payload_blocks(
    payload: dict[str, Any],
    *,
    policy_notes: list[str],
    tooltip_payload: dict[str, Any],
    comments_citations: dict[str, Any] | None,
    linked_entities_payload: dict[str, Any] | None,
    comments_payload: dict[str, Any] | None,
    facts: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach the optional entity blocks, omitting the ones this request did not produce."""
    optional = {
        "notes": policy_notes or None,
        "tooltip": tooltip_payload or None,
        "facts": facts,
        "citations": comments_citations,
        "linked_entities": linked_entities_payload,
        "comments": comments_payload,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload

def build_entity_payload(
    cfg: EntityConfig,
    client: WowheadClient,
    *,
    entity_type: str,
    entity_id: int,
    data_env: int | None,
    include_comments: bool,
    include_all_comments: bool,
    linked_entity_preview_limit: int,
) -> dict[str, Any]:
    """Build the ``entity`` payload; a failed Wowhead request raises ``ProviderError`` for the caller to report."""
    cached_payload = client.get_cached_entity_response(
        requested_type=entity_type,
        requested_id=entity_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    if cached_payload is not None:
        # The cache key leaves out how this run picked its expansion, so report this run's.
        return {**cached_payload, "expansion_source": cfg.expansion_source}

    plan, tooltip = _tooltip_and_page_plan(client, entity_type, entity_id, data_env=data_env)
    canonical = entity_url(plan.page_entity_type, plan.page_entity_id, expansion=cfg.expansion)
    page_url = canonical
    entity_name, tooltip_payload = _normalize_tooltip_payload(tooltip)
    html: str | None = None
    metadata: dict[str, str | None] | None = None

    if entity_page_needs_fetch(
        include_comments=include_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
        tooltip_from_page_metadata=plan.tooltip_from_page_metadata,
    ):
        html, metadata = _entity_page(client, plan.page_entity_type, plan.page_entity_id)
        page_url = metadata["canonical_url"] or canonical

    if plan.tooltip_from_page_metadata and metadata is not None:
        entity_name, tooltip_payload = _build_tooltip_from_page_metadata(metadata)
    comments_payload, comments_citations = entity_comments_payload(
        html=html,
        page_url=page_url,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
    )

    payload = _entity_payload_blocks(
        {
            "expansion": cfg.expansion.key,
            "expansion_source": cfg.expansion_source,
            "entity": {
                "type": entity_type,
                "id": entity_id,
                "name": entity_name,
                "page_url": page_url,
            },
        },
        policy_notes=_expansion_policy_notes(cfg.expansion, page_url, canonical),
        tooltip_payload=tooltip_payload,
        comments_citations=comments_citations,
        linked_entities_payload=entity_linked_entities_payload(
            html=html,
            page_url=page_url,
            page_entity_type=plan.page_entity_type,
            page_entity_id=plan.page_entity_id,
            requested_entity_type=entity_type,
            requested_entity_id=entity_id,
            linked_entity_preview_limit=linked_entity_preview_limit,
            expansion=cfg.expansion,
        ),
        comments_payload=comments_payload,
        # The compact summary gives each location's spawn count; entity-page lists the coordinates.
        facts=extract_page_facts(
            html,
            page_url=page_url,
            page_entity_type=plan.page_entity_type,
            page_entity_id=plan.page_entity_id,
            coords=False,
        )
        if html is not None
        else None,
    )

    payload = attach_entity_normalization(payload, entity_type=entity_type, tooltip=tooltip, page=metadata)
    client.set_cached_entity_response(
        payload,
        requested_type=entity_type,
        requested_id=entity_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    return payload
