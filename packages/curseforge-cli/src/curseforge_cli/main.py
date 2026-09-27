from __future__ import annotations

import httpx
import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.exit_codes import EXIT_AUTH, EXIT_NOT_FOUND, error_code_for_http_status, exit_code_for

from curseforge_cli.client import CurseForgeClientError, verification_note
from curseforge_cli.provider import PROVIDER, PROVIDER_NAME, addon_envelope

app = typer.Typer(
    add_completion=False,
    # The verification sentence comes from the client so --help, doctor and provenance never
    # disagree about whether the endpoints are confirmed.
    help=(
        "Public CurseForge addon API CLI (World of Warcraft). Experimental tier: the surface is one "
        "addon lookup plus doctor and search/resolve are stubs. " + verification_note()
    ),
)
install_common_callback(app, provider=PROVIDER_NAME)

# Exit codes for the client's own error codes; every other code maps through exit_code_for.
_EXIT_CODE_BY_ERROR_CODE = {
    "missing_api_key": EXIT_AUTH,
    "addon_not_found": EXIT_NOT_FOUND,
}


def _error_detail(exc: CurseForgeClientError | httpx.HTTPError) -> tuple[str, str]:
    if isinstance(exc, CurseForgeClientError):
        return exc.code, exc.message
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return error_code_for_http_status(status), f"CurseForge API returned HTTP {status} for {exc.request.url}."
    # httpx.RequestError (timeouts, connection failures) after retries are exhausted.
    return "network_error", f"CurseForge API request failed: {exc}."


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report install state, CurseForge API-key auth posture, and capability metadata."""
    emit(ctx, PROVIDER.doctor())


@app.command("addon")
def addon(
    ctx: typer.Context,
    slug_or_id: str = typer.Argument(..., help="CurseForge addon slug (e.g. deadly-boss-mods) or numeric mod id."),
) -> None:
    """Fetch a WoW addon's metadata, latest files, and latest changelog by slug or mod id."""
    try:
        payload = addon_envelope(slug_or_id)
    except (CurseForgeClientError, httpx.HTTPError) as exc:
        code, message = _error_detail(exc)
        fail(ctx, code, message, exit_code=_EXIT_CODE_BY_ERROR_CODE.get(code, exit_code_for(code)))
    emit(ctx, payload)


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query. Addon discovery search is not implemented yet."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Unused until curseforge search ships."),
) -> None:
    """Coming soon: structured addon discovery search is not implemented yet."""
    emit(ctx, PROVIDER.search(query, limit=limit))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query. Conservative resolution is not implemented yet."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Unused until curseforge resolve ships."),
) -> None:
    """Coming soon: conservative single-addon resolution is not implemented yet."""
    emit(ctx, PROVIDER.resolve(query, limit=limit))


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()
