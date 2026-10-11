"""Explicit talent references embedded in a guide's body, without prose inference."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from warcraft_content.guide_page import WOW_TALENT_EXPORT_RE, talent_export_reference
from warcraft_core.identity import RETAIL_TALENT_CALCULATORS, build_reference_payload

from wowhead_cli.expansion_profiles import EXPANSION_PREFIXES, detect_expansion_from_ref, is_wowhead_host
from wowhead_cli.page_parser import MARKUP_ATTR_RE, MARKUP_BUILD_TAG_RE, extract_markup_urls
from wowhead_cli.talent_services import _talent_calc_state


def _explicit_references(html: str, markup: str | None, *, source_url: str) -> list[dict[str, str]]:
    candidates = extract_markup_urls(markup, source_url=source_url) if markup else []
    article = BeautifulSoup(html, "html.parser").find(id="guide-body")
    if isinstance(article, Tag):
        candidates.extend(
            {
                "url": urljoin(source_url, str(anchor["href"])),
                "original_ref": str(anchor["href"]),
                "label": anchor.get_text(" ", strip=True),
                "source_url": source_url,
            }
            for anchor in article.find_all("a", href=True)
        )
    return candidates


def _published_build_reference(attributes: dict[str, str], *, source_url: str, expansion: str) -> dict[str, Any] | None:
    original = attributes.get("talents", "")
    if not original.startswith("blizzard/") and not WOW_TALENT_EXPORT_RE.fullmatch(original):
        return None
    profile = detect_expansion_from_ref(source_url)
    calculator = profile.key if profile is not None else expansion
    if calculator not in RETAIL_TALENT_CALCULATORS:
        raise ValueError("Published WoW loadout imports require a retail calculator; classic hashes are not reinterpreted.")
    if original.startswith("blizzard/"):
        prefix = f"/{calculator}" if calculator != "retail" else ""
        row = _blizzard_export_reference(
            {
                "url": urljoin(source_url, f"{prefix}/talent-calc/{original}"),
                "original_ref": original,
                "label": attributes.get("title", ""),
            },
            source_url=source_url, expansion=calculator,
        )
        assert row is not None
    else:
        row = talent_export_reference(original, label=attributes.get("title"), source_url=source_url, provider="wowhead")
    row["source"]["original_field"] = "talents"
    row["source"]["original_field_value"] = original
    row.setdefault("citations", []).append({"url": source_url, "provider": "wowhead"})
    return row


def _published_builds(markup: str | None, *, source_url: str, expansion: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for block in MARKUP_BUILD_TAG_RE.finditer(markup or ""):
        if block.group("tag") != "build":
            continue
        attributes = {
            match.group("key"): match.group("quoted") or match.group("bare") or ""
            for match in MARKUP_ATTR_RE.finditer(block.group("attrs"))
        }
        try:
            row = _published_build_reference(attributes, source_url=source_url, expansion=expansion)
        except ValueError as exc:
            excluded.append({"url": attributes["talents"], "source_url": source_url, "reason": str(exc)})
            continue
        if row is not None:
            rows.append(row)
    return rows, excluded


def _blizzard_export_reference(candidate: dict[str, str], *, source_url: str, expansion: str) -> dict[str, Any] | None:
    reference = candidate["url"]
    parsed = urlparse(reference)
    parts = parsed.path.lstrip("/").split("/")
    if parts and parts[0] in EXPANSION_PREFIXES:
        parts = parts[1:]
    if len(parts) < 2 or parts[:2] != ["talent-calc", "blizzard"]:
        return None
    profile = detect_expansion_from_ref(reference)
    calculator = profile.key if profile is not None else expansion
    if calculator not in RETAIL_TALENT_CALCULATORS:
        raise ValueError("Blizzard loadout calculator references require a retail calculator; classic hashes are not reinterpreted.")
    if parsed.scheme not in {"http", "https"} or not is_wowhead_host(parsed.hostname or ""):
        raise ValueError("Talent calculator URL must point to wowhead.com.")
    # Loadouts use base64: a slash may be literal or percent-encoded within the code.
    code = unquote("/".join(parts[2:]))
    if not WOW_TALENT_EXPORT_RE.fullmatch(code):
        raise ValueError("Blizzard calculator reference requires one explicit WoW loadout import string.")
    row = talent_export_reference(code, label=candidate["label"], source_url=source_url, provider="wowhead")
    row["source"]["original_ref"] = candidate.get("original_ref", reference)
    row["source"]["reference_url"] = reference
    row["citations"] = [{"url": reference, "provider": "wowhead", "source_url": source_url}]
    return row


def guide_build_references(
    html: str,
    markup: str | None,
    *,
    source_url: str,
    expansion: str,
) -> dict[str, Any]:
    published_rows, excluded = _published_builds(markup, source_url=source_url, expansion=expansion)
    items = {str(row["url"]): row for row in published_rows}
    for candidate in _explicit_references(html, markup, source_url=source_url):
        reference = candidate["url"]
        # Ignore ordinary guide links; keep malformed calculator references visible.
        if "talent-calc" not in urlparse(reference).path.split("/"):
            continue
        try:
            published = _blizzard_export_reference(candidate, source_url=source_url, expansion=expansion)
            if published is not None:
                existing = items.setdefault(str(published["url"]), published)
                # A build block and calculator link may publish the same code. Preserve both
                # citations even when the build block supplied the preferred label first.
                for key, value in published["source"].items():
                    existing["source"].setdefault(key, value)
                citations = existing.setdefault("citations", [])
                for citation in published.get("citations", []):
                    if citation not in citations:
                        citations.append(citation)
                continue
            state = _talent_calc_state(reference, default_expansion=expansion)
            if not state["has_build_code"] or not state["spec_slug"]:
                raise ValueError("Calculator reference needs an explicit spec and build code.")
            row = build_reference_payload(
                ref=state["state_url"],
                provider="wowhead",
                source="guide_embedded_link",
                source_url=source_url,
                label=candidate["label"],
                notes=["explicit calculator link in the guide body"],
            )
            if row is None:
                raise ValueError("Calculator reference has no supported build identity.")
        except ValueError as exc:
            excluded.append({"url": reference, "source_url": source_url, "reason": str(exc)})
            continue
        items.setdefault(str(row["url"]), dict(row))
    return {"count": len(items), "items": list(items.values()), "excluded_count": len(excluded), "excluded_references": excluded}
