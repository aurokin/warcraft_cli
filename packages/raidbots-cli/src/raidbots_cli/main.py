from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
from warcraft_core.output import emit

from raidbots_cli.client import (
    InvalidReportReference,
    RaidbotsClient,
    load_raidbots_cache_settings_from_env,
    load_raidbots_urls_from_env,
    resolve_report_id,
)
from raidbots_cli.report import parse_report
from raidbots_cli.simc_input import classify_simc_input, simc_handoff

app = typer.Typer(add_completion=False, help="Raidbots report consumption and local SimC handoff CLI.")


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
    _emit(ctx, {"ok": False, "error": {"code": code, "message": message}}, err=True)
    raise typer.Exit(status)


def _client(ctx: typer.Context) -> RaidbotsClient:
    try:
        return RaidbotsClient()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        raise AssertionError("unreachable") from None


def _handle_http_error(ctx: typer.Context, exc: httpx.HTTPStatusError) -> None:
    status_code = exc.response.status_code
    code = "upstream_error"
    if status_code == 400:
        code = "invalid_query"
    elif status_code == 404:
        code = "not_found"
    elif status_code == 429:
        code = "rate_limited"
    _fail(ctx, code, f"Raidbots request failed with HTTP {status_code}.", status=1)


def _handle_request_error(ctx: typer.Context, exc: httpx.RequestError) -> None:
    # Transient transport failures (connect/read timeouts, DNS, resets) surface as
    # httpx.RequestError after retries are exhausted; map them to the structured envelope
    # instead of letting the traceback escape.
    _fail(ctx, "upstream_error", f"Raidbots request failed: {exc}", status=1)


def _resolve_report_id_or_fail(ctx: typer.Context, reference: str) -> str:
    try:
        return resolve_report_id(reference)
    except InvalidReportReference as exc:
        _fail(ctx, "invalid_report", str(exc))
        raise AssertionError("unreachable") from None


def _freshness(client: RaidbotsClient) -> dict[str, Any]:
    # retrieved_at is always accurate (when this CLI produced the response). from_cache flags
    # that the payload may be up to cache_ttl_seconds old, rather than implying a live fetch.
    return {
        "retrieved_at": datetime.now(UTC).isoformat(),
        "from_cache": client.last_from_cache,
        "cache_ttl_seconds": client.report_ttl_seconds,
    }


def _citations(client: RaidbotsClient, report_id: str) -> dict[str, Any]:
    urls = client.urls
    return {
        "report_url": urls.report_url(report_id),
        "data_json_url": urls.data_url(report_id),
        "simc_input_url": urls.input_url(report_id),
    }


@app.callback()
def main_callback(
    ctx: typer.Context,
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output."),
) -> None:
    ctx.obj = RuntimeConfig(pretty=pretty)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    try:
        settings, report_ttl = load_raidbots_cache_settings_from_env()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        return
    urls = load_raidbots_urls_from_env()
    _emit(
        ctx,
        {
            "provider": "raidbots",
            "status": "partial",
            "command": "doctor",
            "installed": True,
            "language": "python",
            "auth": {
                "required": False,
                "deferred": True,
            },
            "capabilities": {
                "search": "not_supported",
                "resolve": "not_supported",
                "inspect_report": "ready",
                "input": "ready",
                "explain_input": "ready",
                "submit": "not_supported",
            },
            "url_templates": urls.templates(),
            "cache": {
                "enabled": settings.enabled,
                "backend": settings.backend,
                "cache_dir": str(settings.cache_dir),
                "redis_url": settings.redis_url,
                "prefix": settings.prefix,
                "ttls": {
                    "report": report_ttl,
                },
            },
            "notes": [
                "Report consumption only: fetch + parse public reports and bridge to local simc.",
                "Submission is deferred (no sanctioned Raidbots API); generate input locally and paste it.",
            ],
        },
    )


@app.command("inspect-report")
def inspect_report(
    ctx: typer.Context,
    reference: str = typer.Argument(..., metavar="URL_OR_ID", help="Raidbots report URL or bare report ID."),
    no_raw: bool = typer.Option(
        False, "--no-raw", help="Omit the raw data.json payload (recommended for large Top Gear/Droptimizer reports)."),
) -> None:
    report_id = _resolve_report_id_or_fail(ctx, reference)
    try:
        with _client(ctx) as client:
            data = client.report_data(report_id)
            payload: dict[str, Any] = {
                "provider": "raidbots",
                "report": parse_report(data, report_id=report_id),
                "scope": {"type": "raidbots_report"},
                "freshness": _freshness(client),
                "citations": _citations(client, report_id),
            }
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    except httpx.RequestError as exc:
        _handle_request_error(ctx, exc)
        return
    except ValueError as exc:
        _fail(ctx, "invalid_report", str(exc))
        return
    payload["scope"]["kind"] = payload["report"].get("kind")
    if not no_raw:
        payload["raw"] = data
    _emit(ctx, payload)


@app.command("input")
def report_input(
    ctx: typer.Context,
    reference: str = typer.Argument(..., metavar="URL_OR_ID", help="Raidbots report URL or bare report ID."),
) -> None:
    report_id = _resolve_report_id_or_fail(ctx, reference)
    try:
        with _client(ctx) as client:
            text = client.report_input(report_id)
            citations = _citations(client, report_id)
            freshness = _freshness(client)
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    except httpx.RequestError as exc:
        _handle_request_error(ctx, exc)
        return
    classification = classify_simc_input(text)
    _emit(
        ctx,
        {
            "provider": "raidbots",
            "report_id": report_id,
            "input": text,
            "handoff": simc_handoff(text, classification),
            "scope": {"type": "raidbots_simc_input", "sim_type_guess": classification["sim_type_guess"]},
            "freshness": freshness,
            "citations": citations,
        },
    )


@app.command("explain-input")
def explain_input(
    ctx: typer.Context,
    text: str | None = typer.Option(None, "--text", help="Inline SimC addon/profile text."),
    file: str | None = typer.Option(None, "--file", help="Path to a file containing SimC addon/profile text."),
) -> None:
    sources = [value for value in (text, file) if value is not None]
    if len(sources) > 1:
        _fail(ctx, "invalid_query", "Provide only one of --text or --file.")
        return
    if text is not None:
        content = text
    elif file is not None:
        try:
            content = Path(file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            _fail(ctx, "invalid_query", f"Could not read input file: {exc}")
            return
    else:
        content = sys.stdin.read()
    if not content.strip():
        _fail(ctx, "invalid_query", "No SimC input provided (use --text, --file, or stdin).")
        return
    classification = classify_simc_input(content)
    _emit(
        ctx,
        {
            "provider": "raidbots",
            "scope": {"type": "raidbots_simc_input", "sim_type_guess": classification["sim_type_guess"]},
            "handoff": simc_handoff(content, classification),
        },
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()
