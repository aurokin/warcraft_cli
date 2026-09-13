"""Typer layer for the ``warcraft-wiki`` binary; all behavior lives in ``warcraft_wiki_cli.provider``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.envelope import Envelope
from warcraft_core.provider import ProviderError

from warcraft_wiki_cli import provider as wiki_provider
from warcraft_wiki_cli.client import (
    WarcraftWikiClient as WarcraftWikiClient,  # re-exported: tests and the wrapper patch warcraft_wiki_cli.main.WarcraftWikiClient
)
from warcraft_wiki_cli.provider import PROVIDER
from warcraft_wiki_cli.search import PROVIDER_NAME

app = typer.Typer(add_completion=False, help="Warcraft Wiki reference CLI.")
install_common_callback(app, provider=PROVIDER_NAME)


def _emit_or_fail(ctx: typer.Context, build: Callable[[], Envelope]) -> None:
    """Run a pure provider call and turn ProviderError into the shared error envelope plus its exit code."""
    try:
        envelope = build()
    except ProviderError as exc:
        fail(ctx, exc.code, exc.message, exit_code=exc.exit_code, details=exc.details)
    emit(ctx, envelope)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report Warcraft Wiki CLI readiness, per-command capabilities, and cache configuration."""
    _emit_or_fail(ctx, PROVIDER.doctor)


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Query text to match against Warcraft Wiki article titles."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
) -> None:
    """Search Warcraft Wiki articles by free text, ranked by title match and content family."""
    _emit_or_fail(ctx, lambda: PROVIDER.search(query, limit=limit))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query to resolve to the single best Warcraft Wiki article."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to inspect."),
) -> None:
    """Resolve a free-text query to the best matching wiki article plus the follow-up command."""
    _emit_or_fail(ctx, lambda: PROVIDER.resolve(query, limit=limit))


@app.command("article")
def article(
    ctx: typer.Context,
    article_ref: str = typer.Argument(..., help="Wiki article title or warcraft.wiki.gg URL."),
) -> None:
    """Fetch one wiki article with extracted text, headings, and previews of navigation and linked entities."""
    _emit_or_fail(ctx, lambda: wiki_provider.article(article_ref))


@app.command("article-full")
def article_full(
    ctx: typer.Context,
    article_ref: str = typer.Argument(..., help="Wiki article title or warcraft.wiki.gg URL."),
) -> None:
    """Fetch one wiki article with every extracted section and the complete linked-entity list."""
    _emit_or_fail(ctx, lambda: wiki_provider.article(article_ref, full=True))


@app.command("api")
def api_reference(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="API or programming reference query, article title, or URL."),
) -> None:
    """Resolve a query to an API, framework, CVar, or XML reference page and return its summary."""
    _emit_or_fail(ctx, lambda: wiki_provider.typed_reference(query, surface="api"))


@app.command("api-full")
def api_reference_full(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="API or programming reference query, article title, or URL."),
) -> None:
    """Resolve a query to an API reference page and return every extracted section."""
    _emit_or_fail(ctx, lambda: wiki_provider.typed_reference(query, surface="api", full=True))


@app.command("event")
def event_reference(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Event or UI handler query, article title, or URL."),
) -> None:
    """Resolve a query to a UI handler or event reference page and return its summary."""
    _emit_or_fail(ctx, lambda: wiki_provider.typed_reference(query, surface="event"))


@app.command("event-full")
def event_reference_full(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Event or UI handler query, article title, or URL."),
) -> None:
    """Resolve a query to a UI handler reference page and return every extracted section."""
    _emit_or_fail(ctx, lambda: wiki_provider.typed_reference(query, surface="event", full=True))


@app.command("article-export")
def article_export(
    ctx: typer.Context,
    article_ref: str = typer.Argument(..., help="Wiki article title or warcraft.wiki.gg URL."),
    out: Path | None = typer.Option(None, "--out", help="Output directory. Defaults to ./warcraft-wiki_exports/article-<slug>."),
) -> None:
    """Write a wiki article to a local bundle directory for offline querying."""
    _emit_or_fail(ctx, lambda: wiki_provider.article_export(article_ref, out=out))


@app.command("article-query")
def article_query(
    ctx: typer.Context,
    bundle: str = typer.Argument(..., help="Exported bundle directory produced by article-export."),
    query: str = typer.Argument(..., help="Query text to match against the exported article bundle."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum matches to return per kind."),
    kind: list[str] = typer.Option(
        [],
        "--kind",
        help="Restrict search kinds. Repeat or pass comma-separated values from: sections, navigation, linked_entities.",
    ),
    section_title: str | None = typer.Option(None, "--section-title", help="Restrict section matches to a title substring."),
) -> None:
    """Search an exported wiki article bundle on disk without touching the network."""
    kinds = {item.strip() for raw in kind for item in raw.split(",") if item.strip()}
    _emit_or_fail(
        ctx,
        lambda: wiki_provider.article_query(bundle, query, limit=limit, kinds=kinds, section_title=section_title),
    )


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()
