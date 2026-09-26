from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.envelope import Envelope
from warcraft_core.provider import ProviderError

from raidbots_cli.provider import PROVIDER_NAME
from raidbots_cli.provider import doctor as provider_doctor
from raidbots_cli.provider import explain_input as provider_explain_input
from raidbots_cli.provider import inspect_report as provider_inspect_report
from raidbots_cli.provider import report_input as provider_report_input

app = typer.Typer(add_completion=False, help="Raidbots report consumption and local SimC handoff CLI.")
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
    """Report Raidbots capabilities, cache configuration, and the resolved report URL templates."""
    _emit_surface(ctx, provider_doctor)


@app.command("inspect-report")
def inspect_report(
    ctx: typer.Context,
    reference: str = typer.Argument(..., metavar="URL_OR_ID", help="Raidbots report URL or bare report ID."),
    no_raw: bool = typer.Option(
        False, "--no-raw", help="Omit the raw data.json payload (recommended for large Top Gear/Droptimizer reports)."),
) -> None:
    """Fetch and parse a Raidbots report into a kind-aware summary with freshness and citations."""
    _emit_surface(ctx, lambda: provider_inspect_report(reference, include_raw=not no_raw))


@app.command("input")
def report_input(
    ctx: typer.Context,
    reference: str = typer.Argument(..., metavar="URL_OR_ID", help="Raidbots report URL or bare report ID."),
) -> None:
    """Fetch a report's SimC input and suggest the local `simc` commands that consume it."""
    _emit_surface(ctx, lambda: provider_report_input(reference))


def _explain_input_text(ctx: typer.Context, text: str | None, file: str | None) -> str:
    if text is not None and file is not None:
        fail(ctx, "invalid_query", "Provide only one of --text or --file.")
    if text is not None:
        return text
    if file is None:
        return sys.stdin.read()
    try:
        return Path(file).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        fail(ctx, "invalid_query", f"Could not read input file: {exc}")


@app.command("explain-input")
def explain_input(
    ctx: typer.Context,
    text: str | None = typer.Option(None, "--text", help="Inline SimC addon/profile text."),
    file: str | None = typer.Option(None, "--file", help="Path to a file containing SimC addon/profile text."),
) -> None:
    """Classify SimC addon/profile text locally and explain the Raidbots-to-simc handoff. No network."""
    content = _explain_input_text(ctx, text, file)
    _emit_surface(ctx, lambda: provider_explain_input(content))


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()
