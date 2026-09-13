"""Typer layer for the ``method`` binary; all behavior lives in ``method_cli.provider``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.envelope import Envelope
from warcraft_core.provider import ProviderError

from method_cli import provider as method_provider
from method_cli.client import MethodClient as MethodClient  # re-exported: tests and the wrapper patch method_cli.main.MethodClient
from method_cli.provider import PROVIDER, PROVIDER_NAME

app = typer.Typer(add_completion=False, help="Method.gg guide CLI.")
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
    """Report Method CLI readiness, capabilities, supported scope, and cache configuration."""
    _emit_or_fail(ctx, PROVIDER.doctor)


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Query text to match against Method guide names and slugs."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
) -> None:
    """Search the supported Method.gg guide families by free text."""
    _emit_or_fail(ctx, lambda: PROVIDER.search(query, limit=limit))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query to resolve to the single best Method guide."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to inspect."),
) -> None:
    """Resolve a free-text query to the best matching Method guide plus the follow-up command."""
    _emit_or_fail(ctx, lambda: PROVIDER.resolve(query, limit=limit))


@app.command("guide")
def guide(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(..., help="Guide slug or Method.gg guide URL."),
) -> None:
    """Fetch one Method guide page with a preview of its linked entities, builds, and analysis surfaces."""
    _emit_or_fail(ctx, lambda: method_provider.guide(guide_ref))


@app.command("guide-full")
def guide_full(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(..., help="Guide slug or Method.gg guide URL."),
) -> None:
    """Fetch every navigation page of a Method guide with merged linked entities, builds, and analysis surfaces."""
    _emit_or_fail(ctx, lambda: method_provider.guide_full(guide_ref))


@app.command("guide-export")
def guide_export(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(..., help="Guide slug or Method.gg guide URL."),
    out: Path | None = typer.Option(None, "--out", help="Output directory. Defaults to ./method_exports/guide-<slug>."),
) -> None:
    """Write every page of a Method guide to a local bundle directory for offline querying."""
    _emit_or_fail(ctx, lambda: method_provider.guide_export(guide_ref, out=out))


@app.command("guide-query")
def guide_query(
    ctx: typer.Context,
    bundle_ref: str = typer.Argument(..., help="Exported bundle directory produced by guide-export."),
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
    """Search an exported Method bundle on disk without touching the network."""
    kinds = {item.strip() for raw in kind for item in raw.split(",") if item.strip()}
    _emit_or_fail(
        ctx,
        lambda: method_provider.guide_query(bundle_ref, query, limit=limit, kinds=kinds, section_title=section_title),
    )


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()
