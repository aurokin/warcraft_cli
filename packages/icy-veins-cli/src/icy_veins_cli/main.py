"""Typer surface for the ``icy-veins`` binary: thin wrappers over ``icy_veins_cli.provider``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.envelope import Envelope
from warcraft_core.provider import ProviderError

from icy_veins_cli import provider
from icy_veins_cli.client import IcyVeinsClient
from icy_veins_cli.search import PROVIDER_NAME

# Re-exported so ``monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient...")`` keeps working.
__all__ = ["IcyVeinsClient", "app", "run"]

app = typer.Typer(add_completion=False, help="Icy Veins WoW guide CLI.")
install_common_callback(app, provider=PROVIDER_NAME)


def _emit_surface(ctx: typer.Context, build: Callable[[], Envelope]) -> None:
    """Run a pure provider call and turn ``ProviderError`` into the shared error envelope."""
    try:
        payload = build()
    except ProviderError as exc:
        fail(ctx, exc.code, exc.message, exit_code=exc.exit_code, details=exc.details)
    emit(ctx, payload)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report Icy Veins capabilities and the resolved HTTP cache configuration."""
    _emit_surface(ctx, provider.doctor)


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Query text to match against Icy Veins WoW guide slugs and page titles."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
) -> None:
    """Rank Icy Veins WoW guides from the sitemap, the site-wide guide menu and the site index against a free-text query."""
    _emit_surface(ctx, lambda: provider.search(query, limit=limit))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query to resolve to a single Icy Veins guide."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to list; ambiguity is judged over all of them."),
) -> None:
    """Resolve a free-text query to the best Icy Veins guide, with the candidate list attached."""
    _emit_surface(ctx, lambda: provider.resolve(query, limit=limit))


@app.command("index-refresh")
def index_refresh(
    ctx: typer.Context,
    max_requests: int = typer.Option(
        provider.DEFAULT_INDEX_MAX_REQUESTS,
        "--max-requests",
        min=1,
        max=5000,
        help="Most uncached page requests to make; a run that reaches it is partial and the next run resumes.",
    ),
) -> None:
    """Crawl Icy Veins (one request a second) for pages its frozen sitemap lacks and merge them into the local site index."""
    _emit_surface(ctx, lambda: provider.index_refresh(max_requests=max_requests))


@app.command("guide")
def guide(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(..., help="Guide slug or Icy Veins guide URL."),
) -> None:
    """Fetch and summarize a single Icy Veins guide page."""
    _emit_surface(ctx, lambda: provider.guide(guide_ref))


@app.command("guide-full")
def guide_full(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(..., help="Guide slug or Icy Veins guide URL."),
) -> None:
    """Fetch every page in the guide's family navigation and merge them into one payload."""
    _emit_surface(ctx, lambda: provider.guide_full(guide_ref))


@app.command("guide-export")
def guide_export(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(..., help="Guide slug or Icy Veins guide URL."),
    out: Path | None = typer.Option(None, "--out", help="Output directory. Defaults to ./icy-veins_exports/guide-<slug>."),
) -> None:
    """Write the full guide bundle (pages, entities, analysis surfaces) to a local export directory."""
    _emit_surface(ctx, lambda: provider.guide_export(guide_ref, out=out))


@app.command("guide-query")
def guide_query(
    ctx: typer.Context,
    bundle: Path = typer.Argument(
        ...,
        # No ``exists=True`` or ``file_okay=False``: a missing bundle or a file is answered by the provider
        # (not_found, invalid_argument), the same answer ``method guide-query`` gives, with the query echoed.
        help="Directory produced by 'icy-veins guide-export'.",
    ),
    query: str = typer.Argument(..., help="Query text to match against the exported article bundle."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum matches to return per kind."),
    kind: list[str] = typer.Option(
        [],
        "--kind",
        help=(
            "Kinds to search. Repeat or pass comma-separated values from: "
            "sections, navigation, linked_entities, build_references, analysis_surfaces."
        ),
    ),
    section_title: str | None = typer.Option(None, "--section-title", help="Restrict section matches to a title substring."),
) -> None:
    """Search a previously exported guide bundle without touching the network."""
    kinds = [item.strip() for raw in kind for item in raw.split(",") if item.strip()]
    _emit_surface(
        ctx,
        lambda: provider.guide_query(bundle, query, limit=limit, kinds=kinds, section_title=section_title),
    )


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()
