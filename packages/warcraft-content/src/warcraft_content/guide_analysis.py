from __future__ import annotations

from typing import Any

from warcraft_core.wow_normalization import normalized_text

SURFACE_TAGS_BY_CONTENT_FAMILY: dict[str, list[str]] = {
    "class_hub": ["overview"],
    "role_guide": ["overview"],
    "spec_guide": ["overview"],
    "easy_mode": ["easy_mode", "rotation"],
    "spec_builds_talents": ["builds_talents", "talent_recommendations"],
    "rotation_guide": ["rotation", "cooldowns", "abilities"],
    "stat_priority": ["stat_priority", "stat_context"],
    "gems_enchants_consumables": ["gems_enchants_consumables", "gear_context"],
    "gear_best_in_slot": ["gear_best_in_slot", "gear_context"],
    "spell_summary": ["spell_summary", "abilities"],
    "resources": ["resources"],
    "mythic_plus_tips": ["mythic_plus"],
    "macros_addons": ["macros_addons"],
    "simulations": ["simulations"],
    "leveling": ["leveling"],
    "pvp": ["pvp"],
    "raid_guide": ["raid_guide"],
    "expansion_guide": ["expansion_guide"],
    "special_event_guide": ["special_event"],
    "profession_guide": ["profession"],
    "delve_guide": ["delve"],
    "reputation_guide": ["reputation"],
}

KEYWORD_RULES: tuple[tuple[tuple[str, ...], list[str]], ...] = (
    (("introduction", "overview", "general information"), ["overview"]),
    (("talent", "talents", "build", "builds"), ["builds_talents", "talent_recommendations"]),
    (("rotation",), ["rotation"]),
    (("cooldown", "cooldowns"), ["cooldowns"]),
    (("ability", "abilities"), ["abilities"]),
    (("stat priority", "stats"), ["stat_priority", "stat_context"]),
    (("gems", "enchants", "consumables"), ["gems_enchants_consumables", "gear_context"]),
    (("gear", "best in slot", "bis"), ["gear_best_in_slot", "gear_context"]),
    (("spell summary", "spell list", "spells"), ["spell_summary", "abilities"]),
    (("resources",), ["resources"]),
    (("mythic plus", "mythic+", "mythic"), ["mythic_plus"]),
    (("macros", "addons", "add-ons", "ui"), ["macros_addons"]),
    (("simulation", "simulations", "sim"), ["simulations"]),
    (("leveling",), ["leveling"]),
    (("pvp",), ["pvp"]),
    (("raid",), ["raid_guide"]),
    (("profession",), ["profession"]),
    (("delve", "delves"), ["delve"]),
    (("reputation", "renown"), ["reputation"]),
)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split()).strip()
    return text or None


def _preview_text(article: dict[str, Any]) -> str:
    source = _clean_text(str(article.get("intro_text") or "")) or _clean_text(str(article.get("text") or "")) or ""
    if len(source) <= 280:
        return source
    return source[:277].rstrip() + "..."


def _keyword_tags(*values: str | None) -> tuple[list[str], list[str]]:
    haystack_parts: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if cleaned is None:
            continue
        haystack_parts.append(normalized_text(cleaned))
    haystack = " ".join(part for part in haystack_parts if part)
    if not haystack:
        return [], []
    padded_haystack = f" {haystack} "
    token_set = {part for part in haystack.split() if part}
    tags: list[str] = []
    reasons: list[str] = []
    for phrases, row_tags in KEYWORD_RULES:
        matched = next(
            (
                phrase
                for phrase in phrases
                if (
                    (normalized_text(phrase) in token_set)
                    if " " not in normalized_text(phrase)
                    else f" {normalized_text(phrase)} " in padded_haystack
                )
            ),
            None,
        )
        if matched is None:
            continue
        for tag in row_tags:
            if tag not in tags:
                tags.append(tag)
        reasons.append(f"keyword:{matched}")
    return tags, reasons


def _clean_field(mapping: dict[str, Any], key: str) -> str | None:
    return _clean_text(str(mapping.get(key) or "")) or None


def _append_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def extract_guide_analysis_surfaces(page_payload: dict[str, Any], *, provider: str) -> list[dict[str, Any]]:
    guide = dict(page_payload.get("guide") or {})
    page = dict(page_payload.get("page") or {})
    article = dict(page_payload.get("article") or {})
    content_family = _clean_field(guide, "content_family")
    section_slug = _clean_field(guide, "section_slug")
    section_title = _clean_field(guide, "section_title")
    page_title = _clean_field(page, "title")

    tags = list(SURFACE_TAGS_BY_CONTENT_FAMILY.get(content_family or "", []))
    reasons = [f"content_family:{content_family}"] if tags and content_family else []
    confidence = "high" if tags else "medium"
    source_kind = "content_family" if tags else "title_slug_heuristic"

    keyword_tags, keyword_reasons = _keyword_tags(section_slug, section_title, page_title)
    _append_unique(tags, keyword_tags)
    _append_unique(reasons, keyword_reasons)

    if not tags:
        return []

    preview = _preview_text(article)
    return [
        {
            "kind": "guide_analysis_surface",
            "surface_tags": tags,
            "confidence": confidence,
            "source_kind": source_kind,
            "provider": provider,
            "content_family": content_family,
            "page_url": guide.get("page_url"),
            "section_slug": section_slug,
            "section_title": section_title,
            "page_title": page_title,
            "text_preview": preview,
            "match_reasons": reasons,
            "citation": {
                "page_url": guide.get("page_url"),
                "section_title": section_title,
                "page_title": page_title,
            },
        }
    ]


def extract_section_chunk_analysis_surfaces(
    *,
    provider: str,
    page_url: str,
    page_title: str | None,
    section_chunks: list[dict[str, Any]],
    content_family: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in section_chunks:
        section_title = _clean_text(str(row.get("title") or "")) or None
        if section_title is None:
            continue
        section_ordinal = row.get("ordinal")
        section_preview = _clean_text(str(row.get("content_text") or "")) or ""
        title_tags, title_reasons = _keyword_tags(section_title)
        if not title_tags:
            continue
        rows.append(
            {
                "kind": "guide_analysis_surface",
                "surface_tags": title_tags,
                "confidence": "high",
                "source_kind": "section_heading",
                "provider": provider,
                "content_family": content_family,
                "page_url": page_url,
                "section_slug": None,
                "section_title": section_title,
                "section_ordinal": section_ordinal if isinstance(section_ordinal, int) else None,
                "page_title": _clean_text(page_title),
                "text_preview": section_preview[:280].rstrip() + ("..." if len(section_preview) > 280 else ""),
                "match_reasons": title_reasons,
                "citation": {
                    "page_url": page_url,
                    "section_title": section_title,
                    "section_ordinal": section_ordinal if isinstance(section_ordinal, int) else None,
                    "page_title": _clean_text(page_title),
                },
            }
        )
    return rows


def merge_guide_analysis_surfaces(pages: list[dict[str, Any]], *, page_key: str = "guide") -> list[dict[str, Any]]:
    merged: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for page in pages:
        page_url = str((page.get(page_key) or {}).get("page_url") or "")
        for row in page.get("analysis_surfaces") or []:
            key = (page_url, tuple(str(tag) for tag in row.get("surface_tags") or []))
            record = merged.get(key)
            if record is None:
                merged[key] = dict(row)
                continue
            existing_reasons = list(record.get("match_reasons") or [])
            for reason in row.get("match_reasons") or []:
                if reason not in existing_reasons:
                    existing_reasons.append(str(reason))
            record["match_reasons"] = existing_reasons
            if not record.get("text_preview") and row.get("text_preview"):
                record["text_preview"] = row["text_preview"]
    return sorted(
        merged.values(),
        key=lambda row: (
            str(row.get("page_url") or ""),
            ",".join(str(tag) for tag in row.get("surface_tags") or []),
        ),
    )
