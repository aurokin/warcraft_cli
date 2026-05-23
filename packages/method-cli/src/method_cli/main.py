from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from warcraft_content.article_bundle import (
    default_article_export_dir,
    load_article_bundle,
    query_article_bundle,
    write_article_bundle,
)
from warcraft_content.article_discovery import (
    article_candidate,
    merge_article_build_references,
    merge_article_linked_entities,
    sort_article_candidates,
)
from warcraft_content.article_provider_cli import (
    build_article_resolve_response,
    build_article_search_response,
    fail_with_error,
    unsupported_guide_surface_message,
)
from warcraft_content.guide_analysis import extract_guide_analysis_surfaces, merge_guide_analysis_surfaces
from warcraft_core.output import emit

from method_cli.client import MethodClient, guide_ref_parts, load_method_cache_settings_from_env
from method_cli.page_parser import UNSUPPORTED_ROOT_GUIDE_SLUGS, classify_guide_family

app = typer.Typer(add_completion=False, help="Method.gg guide CLI.")

SEARCH_TYPE_NAME = "Guide"
FAMILY_QUERY_KEYWORDS = {
    "profession_guide": {
        "profession",
        "professions",
        "alchemy",
        "blacksmithing",
        "enchanting",
        "engineering",
        "herbalism",
        "inscription",
        "jewelcrafting",
        "leatherworking",
        "mining",
        "skinning",
        "tailoring",
        "fishing",
        "cooking",
    },
    "delve_guide": {"delve", "delves"},
    "reputation_guide": {"renown", "reputation"},
}
UNSUPPORTED_QUERY_HINTS = {
    "tier_list": {
        "keywords": {"tier", "list"},
        "message": "Method tier-list index roots are currently out of scope for the supported Method surface.",
    },
}
SUPPORTED_SCOPE = {
    "content_families": [
        "class_guide",
        "profession_guide",
        "delve_guide",
        "reputation_guide",
        "article_guide",
    ],
    "url_patterns": ["/guides/<slug>", "/guides/<slug>/<section>"],
    "unsupported_roots": sorted(UNSUPPORTED_ROOT_GUIDE_SLUGS),
    "notes": [
        "search and resolve are limited to the currently supported Method guide families",
        "tier-list and world-of-warcraft roots are intentionally excluded because they currently use unsupported index-style templates",
        "premium, login, and non-guide Method surfaces are intentionally out of scope",
    ],
}


@dataclass(slots=True)
class RuntimeConfig:
    pretty: bool = False


def _cfg(ctx: typer.Context) -> RuntimeConfig:
    obj = ctx.obj
    if isinstance(obj, RuntimeConfig):
        return obj
    return RuntimeConfig()


def _emit(ctx: typer.Context, payload: dict[str, Any], *, err: bool = False) -> None:
    emit(payload, pretty=_cfg(ctx).pretty, err=err)


def _fail(ctx: typer.Context, code: str, message: str, *, status: int = 1) -> None:
    fail_with_error(lambda payload, err: _emit(ctx, payload, err=err), code=code, message=message, status=status)


def _client(ctx: typer.Context) -> MethodClient:
    try:
        return MethodClient()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        raise AssertionError("unreachable") from None


def _guide_ref_parts_or_fail(ctx: typer.Context, guide_ref: str) -> tuple[str, str | None]:
    try:
        return guide_ref_parts(guide_ref)
    except ValueError as exc:
        _fail(ctx, "invalid_guide_ref", str(exc))
        raise AssertionError("unreachable") from None


def _fetch_guide_page_or_fail(ctx: typer.Context, client: MethodClient, guide_ref: str) -> dict[str, Any]:
    try:
        payload = client.fetch_guide_page(guide_ref)
    except ValueError as exc:
        _fail(ctx, "invalid_guide_ref", str(exc))
        raise AssertionError("unreachable") from None
    if payload["guide"].get("supported_surface") is False:
        _fail(
            ctx,
            "unsupported_guide_surface",
            unsupported_guide_surface_message(
                provider_name="Method",
                slug=payload["guide"]["slug"],
                content_family=payload["guide"].get("content_family"),
            ),
        )
        raise AssertionError("unreachable") from None
    return payload


def _fetch_guide_pages_or_fail(ctx: typer.Context, client: MethodClient, guide_ref: str) -> dict[str, Any]:
    try:
        payload = _fetch_guide_pages(client, guide_ref)
    except ValueError as exc:
        _fail(ctx, "invalid_guide_ref", str(exc))
        raise AssertionError("unreachable") from None
    if payload["guide"].get("supported_surface") is False:
        _fail(
            ctx,
            "unsupported_guide_surface",
            unsupported_guide_surface_message(
                provider_name="Method",
                slug=payload["guide"]["slug"],
                content_family=payload["guide"].get("content_family"),
            ),
        )
        raise AssertionError("unreachable") from None
    return payload


def _normalize_query(query: str) -> str:
    normalized = re.sub(r"\b(method|guide|guides)\b", " ", query.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or query.strip().lower()


def _score_text_match(query: str, candidate: str) -> int:
    if not query or not candidate:
        return 0
    score = 0
    if candidate == query:
        score += 40
    if candidate.startswith(query):
        score += 15
    if query in candidate:
        score += 10
    query_terms = [term for term in query.split() if term]
    if query_terms and all(term in candidate for term in query_terms):
        score += 8
    return score


def _query_terms(query: str) -> set[str]:
    return {term for term in query.split() if term}


def _family_score_boost(content_family: str, query: str) -> tuple[int, list[str]]:
    terms = _query_terms(query)
    if not terms:
        return 0, []
    reasons: list[str] = []
    score = 0
    for family, keywords in FAMILY_QUERY_KEYWORDS.items():
        if not (terms & keywords):
            continue
        if family != content_family:
            continue
        score += 12
        reasons.append("content_family_match")
    return score, reasons


def _unsupported_scope_hint(query: str) -> dict[str, Any] | None:
    terms = _query_terms(query)
    if not terms:
        return None
    for code, config in UNSUPPORTED_QUERY_HINTS.items():
        keywords = set(config["keywords"])
        if keywords <= terms:
            return {
                "code": code,
                "message": config["message"],
            }
    return None


def _search_results(client: MethodClient, query: str, *, limit: int) -> tuple[str, list[dict[str, Any]], int]:
    normalized_query = _normalize_query(query)
    scope_hint = _unsupported_scope_hint(normalized_query)
    if scope_hint is not None:
        return normalized_query, [], 0, scope_hint
    matches: list[dict[str, Any]] = []
    for row in client.sitemap_guides():
        slug = row["slug"]
        content_family = classify_guide_family(slug)
        if content_family == "unsupported_index":
            continue
        name = row["name"]
        candidate = f"{name.lower()} {slug.replace('-', ' ')}"
        score = _score_text_match(normalized_query, candidate)
        reasons: list[str] = []
        if name.lower() == normalized_query:
            reasons.append("exact_name")
        if candidate.startswith(normalized_query):
            reasons.append("name_prefix")
        if normalized_query in candidate:
            reasons.append("name_contains_query")
        if normalized_query and all(term in candidate for term in normalized_query.split()):
            reasons.append("all_terms_match")
        family_boost, family_reasons = _family_score_boost(content_family, normalized_query)
        score += family_boost
        reasons.extend(family_reasons)
        if score <= 0:
            continue
        candidate_row = article_candidate(
            ref=slug,
            name=name,
            url=row["url"],
            score=score,
            reasons=reasons,
            provider_command="method",
        )
        candidate_row["metadata"]["content_family"] = content_family
        matches.append(candidate_row)
    sort_article_candidates(matches)
    return normalized_query, matches[:limit], len(matches), None


def _build_guide_summary(page_payload: dict[str, Any]) -> dict[str, Any]:
    guide = dict(page_payload["guide"])
    page = dict(page_payload["page"])
    navigation = list(page_payload["navigation"])
    article = dict(page_payload["article"])
    linked_entities = list(page_payload["linked_entities"])
    build_references = list(page_payload.get("build_references") or [])
    analysis_surfaces = list(page_payload.get("analysis_surfaces") or [])
    return {
        "guide": guide,
        "page": page,
        "navigation": {
            "count": len(navigation),
            "items": navigation,
        },
        "article": {
            "text": article["text"],
            "headings": article["headings"],
            "section_count": len(article["sections"]),
            "section_preview": [
                {
                    "title": section["title"],
                    "level": section["level"],
                    "ordinal": section["ordinal"],
                }
                for section in article["sections"][:5]
            ],
        },
        "linked_entities": {
            "count": len(linked_entities),
            "items": linked_entities[:10],
            "more_available": len(linked_entities) > 10,
            "fetch_more_command": f"method guide-full {guide['slug']}",
        },
        "build_references": {
            "count": len(build_references),
            "items": build_references[:10],
            "more_available": len(build_references) > 10,
            "fetch_more_command": f"method guide-full {guide['slug']}",
        },
        "analysis_surfaces": {
            "count": len(analysis_surfaces),
            "items": analysis_surfaces[:10],
            "more_available": len(analysis_surfaces) > 10,
            "fetch_more_command": f"method guide-full {guide['slug']}",
        },
        "citations": {
            "page": guide["page_url"],
        },
    }


def _fetch_guide_pages(client: MethodClient, guide_ref: str) -> dict[str, Any]:
    initial = client.fetch_guide_page(guide_ref)
    nav_items = initial["navigation"] or [
        {
            "title": initial["guide"]["section_title"],
            "url": initial["guide"]["page_url"],
            "section_slug": initial["guide"]["section_slug"],
            "active": True,
            "ordinal": 1,
        }
    ]
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in nav_items:
        page_url = item["url"]
        if page_url in seen:
            continue
        seen.add(page_url)
        page_payload = client.fetch_guide_page(page_url)
        page_payload["analysis_surfaces"] = extract_guide_analysis_surfaces(page_payload, provider="method")
        pages.append(page_payload)
    if initial["guide"]["page_url"] not in seen:
        initial["analysis_surfaces"] = extract_guide_analysis_surfaces(initial, provider="method")
        pages.insert(0, initial)
    elif "analysis_surfaces" not in initial:
        initial["analysis_surfaces"] = extract_guide_analysis_surfaces(initial, provider="method")
    guide = dict(initial["guide"])
    guide["page_count"] = len(pages)
    linked_entities = merge_article_linked_entities(pages)
    build_references = merge_article_build_references(pages)
    analysis_surfaces = merge_guide_analysis_surfaces(pages)
    return {
        "guide": guide,
        "page": dict(initial["page"]),
        "navigation": {
            "count": len(nav_items),
            "items": nav_items,
        },
        "pages": [
            {
                "guide": page["guide"],
                "page": page["page"],
                "article": page["article"],
                "build_references": page.get("build_references") or [],
                "analysis_surfaces": page.get("analysis_surfaces") or [],
            }
            for page in pages
        ],
        "linked_entities": {
            "count": len(linked_entities),
            "items": linked_entities,
        },
        "build_references": {
            "count": len(build_references),
            "items": build_references,
        },
        "analysis_surfaces": {
            "count": len(analysis_surfaces),
            "items": analysis_surfaces,
        },
        "citations": {
            "page": guide["page_url"],
            "pages": [page["guide"]["page_url"] for page in pages],
        },
    }


def _default_export_dir(guide_slug: str) -> Path:
    return default_article_export_dir("method", guide_slug)


@app.callback()
def main_callback(
    ctx: typer.Context,
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output."),
) -> None:
    ctx.obj = RuntimeConfig(pretty=pretty)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    try:
        settings, sitemap_ttl, page_ttl = load_method_cache_settings_from_env()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        return
    _emit(
        ctx,
        {
            "provider": "method",
            "status": "ready",
            "command": "doctor",
            "installed": True,
            "language": "python",
            "capabilities": {
                "search": "ready",
                "resolve": "ready",
                "guide": "ready",
                "guide_full": "ready",
                "guide_export": "ready",
                "guide_query": "ready",
            },
            "supported_scope": SUPPORTED_SCOPE,
            "cache": {
                "enabled": settings.enabled,
                "backend": settings.backend,
                "cache_dir": str(settings.cache_dir),
                "redis_url": settings.redis_url,
                "prefix": settings.prefix,
                "ttls": {
                    "sitemap": sitemap_ttl,
                    "page_html": page_ttl,
                },
            },
        },
    )


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Query text to match against Method guide slugs."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
) -> None:
    with _client(ctx) as client:
        normalized_query, results, total_count, scope_hint = _search_results(client, query, limit=limit)
    payload = build_article_search_response(
        query=query,
        search_query=normalized_query,
        results=results,
        total_count=total_count,
        scope_hint=scope_hint,
    )
    _emit(ctx, payload)


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Resolve a free-text query to the best Method guide match."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to inspect."),
) -> None:
    with _client(ctx) as client:
        normalized_query, results, total_count, scope_hint = _search_results(client, query, limit=limit)
    top = results[0] if results else None
    second = results[1] if len(results) > 1 else None
    top_score = top["ranking"]["score"] if top else 0
    second_score = second["ranking"]["score"] if second else 0
    resolved = top is not None and (top_score >= 50 or top_score >= second_score + 15)
    _emit(
        ctx,
        build_article_resolve_response(
            provider_command="method",
            query=query,
            search_query=normalized_query,
            results=results,
            total_count=total_count,
            resolved=resolved,
            scope_hint=scope_hint,
        ),
    )


@app.command("guide")
def guide(ctx: typer.Context, guide_ref: str = typer.Argument(..., help="Guide slug or Method.gg guide URL.")) -> None:
    with _client(ctx) as client:
        page_payload = _fetch_guide_page_or_fail(ctx, client, guide_ref)
        page_payload["analysis_surfaces"] = extract_guide_analysis_surfaces(page_payload, provider="method")
        payload = _build_guide_summary(page_payload)
    _emit(ctx, payload)


@app.command("guide-full")
def guide_full(ctx: typer.Context, guide_ref: str = typer.Argument(..., help="Guide slug or Method.gg guide URL.")) -> None:
    with _client(ctx) as client:
        payload = _fetch_guide_pages_or_fail(ctx, client, guide_ref)
    _emit(ctx, payload)


@app.command("guide-export")
def guide_export(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(..., help="Guide slug or Method.gg guide URL."),
    out: Path | None = typer.Option(None, "--out", help="Output directory. Defaults to ./method_exports/guide-<slug>."),
) -> None:
    slug, _section_slug = _guide_ref_parts_or_fail(ctx, guide_ref)
    export_dir = out.expanduser() if out is not None else _default_export_dir(slug)
    with _client(ctx) as client:
        payload = _fetch_guide_pages_or_fail(ctx, client, guide_ref)
    manifest = write_article_bundle(payload, provider="method", export_dir=export_dir)
    _emit(
        ctx,
        {
            "guide": payload["guide"],
            "counts": manifest["counts"],
            "output_dir": str(export_dir),
            "manifest": manifest,
        },
    )


@app.command("guide-query")
def guide_query(
    ctx: typer.Context,
    bundle_ref: str = typer.Argument(..., help="Exported bundle directory."),
    query: str = typer.Argument(..., help="Query text to search within the exported Method bundle."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum matches to return."),
    kind: list[str] = typer.Option(
        [],
        "--kind",
        help=(
            "Restrict search kinds. Repeat or pass comma-separated values from: "
            "sections, navigation, linked_entities, build_references, analysis_surfaces."
        ),
    ),
    section_title: str | None = typer.Option(
        None,
        "--section-title",
        help="Restrict section searching to section titles containing this text.",
    ),
) -> None:
    export_dir = Path(bundle_ref).expanduser()
    if not export_dir.exists():
        _fail(ctx, "invalid_bundle", f"Bundle directory not found: {export_dir}")
    bundle = load_article_bundle(export_dir)
    selected_kinds = {item.strip() for raw in kind for item in raw.split(",") if item.strip()} or {
        "sections",
        "navigation",
        "linked_entities",
        "build_references",
        "analysis_surfaces",
    }
    invalid = sorted(selected_kinds - {"sections", "navigation", "linked_entities", "build_references", "analysis_surfaces"})
    if invalid:
        _fail(ctx, "invalid_kind", f"Unsupported guide-query kinds: {', '.join(invalid)}")
    payload = query_article_bundle(
        bundle,
        query=query,
        limit=limit,
        kinds=selected_kinds,
        section_title_filter=section_title.strip().lower() if isinstance(section_title, str) and section_title.strip() else None,
    )
    payload["guide"] = bundle["manifest"]["guide"]
    payload["output_dir"] = str(export_dir)
    _emit(ctx, payload)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
