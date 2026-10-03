from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from warcraft_core.discovery import ResolveConfidence, SingleWordIdentity, discovery_row, resolve_data, search_data


def article_follow_up(
    provider_command: str,
    ref: str,
    *,
    surface: str = "guide",
) -> dict[str, Any]:
    quoted_ref = shlex.quote(ref)
    return {
        "command": f"{provider_command} {surface} {quoted_ref}",
        "surface": surface,
        "reason": f"{surface}_summary",
        "alternative_commands": [
            f"{provider_command} {surface}-full {quoted_ref}",
            f"{provider_command} {surface}-export {quoted_ref}",
        ],
    }


@dataclass(frozen=True, slots=True)
class ArticleKind:
    """How a provider labels its articles: the follow-up surface and the row kind."""

    surface: str = "guide"
    kind: str = "guide"


GUIDE_KIND = ArticleKind()


def article_candidate(
    *,
    ref: str,
    name: str,
    url: str,
    score: int,
    reasons: list[str],
    provider: str,
    kind: ArticleKind = GUIDE_KIND,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One article row; ``provider`` is the provider name, which is also the binary its follow-up commands run."""
    follow_up = article_follow_up(provider, ref, surface=kind.surface)
    return discovery_row(
        provider=provider,
        kind=kind.kind,
        id=ref,
        name=name,
        url=url,
        score=score,
        match_reasons=reasons,
        command=follow_up.pop("command"),
        surface=follow_up.pop("surface"),
        follow_up_extra=follow_up,
        metadata=dict(metadata or {}),
    )


def sort_article_candidates(candidates: list[dict[str, Any]]) -> None:
    """Best score first; a tie goes to the most recently updated page (undated rows last), then name.

    The date is the ISO ``metadata.sitemap_lastmod`` the Icy Veins and Method rows carry. Stable
    sorts applied in reverse priority, because the date sorts newest first while name and id sort
    ascending.
    """
    candidates.sort(key=lambda row: (row["name"], row["id"]))
    candidates.sort(key=lambda row: str(row["metadata"].get("sitemap_lastmod") or ""), reverse=True)
    candidates.sort(key=lambda row: -int(row["ranking"]["score"]))


def _payload_extra(query: str, scope_hint: dict[str, Any] | None) -> dict[str, Any]:
    return {"query": query} if scope_hint is None else {"query": query, "scope_hint": scope_hint}


def article_search_payload(
    *,
    query: str,
    search_query: str,
    matches: list[dict[str, Any]],
    limit: int,
    total_matches: int | None = None,
    scope_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``search`` data for every ranked match; ``total_matches`` defaults to their number."""
    return search_data(
        search_query=search_query, ranked=matches, limit=limit, total_matches=total_matches, **_payload_extra(query, scope_hint)
    )


def _resolve_confidence(matches: list[dict[str, Any]], *, resolved: bool) -> ResolveConfidence:
    """``high`` for a resolved match; ``low`` when the best matches tie, since nothing tells them apart."""
    if resolved:
        return "high"
    if not matches:
        return "none"
    if len(matches) > 1 and matches[0]["ranking"]["score"] == matches[1]["ranking"]["score"]:
        return "low"
    return "medium"


def article_resolve_payload(
    *,
    provider_command: str,
    query: str,
    search_query: str,
    matches: list[dict[str, Any]],
    limit: int,
    resolved: bool,
    total_matches: int | None = None,
    scope_hint: dict[str, Any] | None = None,
    single_word_identity: SingleWordIdentity | None = None,
) -> dict[str, Any]:
    """The ``resolve`` data for every ranked match; ``limit`` trims only the candidates shown, never the confidence.

    ``single_word_identity`` is the provider's own test for a one-word query (see ``resolve_data``).
    """
    return resolve_data(
        search_query=search_query,
        ranked=matches,
        limit=limit,
        confidence=_resolve_confidence(matches, resolved=resolved),
        fallback_search_command=f"{provider_command} search {shlex.quote(query)}",
        total_matches=total_matches,
        single_word_identity=single_word_identity,
        **_payload_extra(query, scope_hint),
    )


def merge_article_linked_entities(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold per-page linked entities into one row per entity, keeping every key the pages carried.

    Provider-specific keys such as ``ability_identity`` survive the merge, so ``guide-full`` and
    ``guide-export`` describe an entity exactly as the single-page ``guide`` surface does.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for page in pages:
        page_url = page["guide"]["page_url"]
        for row in page["linked_entities"]:
            key = (str(row["type"]), str(row["id"]))
            record = merged.get(key)
            if record is None:
                merged[key] = {"name": None, **{k: v for k, v in row.items() if k != "source_urls"}, "source_urls": [page_url]}
                continue
            for field_name, value in row.items():
                if field_name != "source_urls" and value and not record.get(field_name):
                    record[field_name] = value
            if page_url not in record["source_urls"]:
                record["source_urls"].append(page_url)
    return sorted(merged.values(), key=lambda row: (str(row["type"]), str(row["id"])))


def merge_article_build_references(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for page in pages:
        page_url = page["guide"]["page_url"]
        for row in page.get("build_references") or []:
            reference_url = str(row["url"])
            record = merged.get(reference_url)
            if record is None:
                merged[reference_url] = {
                    "kind": row["kind"],
                    "reference_type": row["reference_type"],
                    "url": row["url"],
                    "label": row.get("label"),
                    "build_code": row.get("build_code"),
                    "build_identity": row["build_identity"],
                    "source_urls": [page_url],
                }
                if "source" in row:
                    merged[reference_url]["source"] = row["source"]
                continue
            if not record.get("label") and row.get("label"):
                record["label"] = row["label"]
            if page_url not in record["source_urls"]:
                record["source_urls"].append(page_url)
    return sorted(merged.values(), key=lambda row: str(row["url"]))
