"""Shared Typer scaffolding for every binary: runtime config, output flags, emit/fail, process guard.

See docs/foundation/ERROR_CONTRACT.md for the envelope, exit codes, and flag contract this enforces.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, NoReturn

import httpx
import typer

from warcraft_core.envelope import Envelope, error_envelope
from warcraft_core.exit_codes import EXIT_AUTH, EXIT_GENERIC, EXIT_NETWORK, EXIT_NOT_FOUND, EXIT_USAGE, exit_code_for
from warcraft_core.output import (
    DEFAULT_COMPACT_MAX_CHARS,
    DiagnosticsCollector,
    OutputOptions,
    OutputProjectionError,
    emit_shaped,
    resolve_output_options,
    to_json,
)
from warcraft_core.provider import ProviderError


@dataclass(slots=True)
class RuntimeConfig:
    """Per-invocation state stored in ``ctx.obj``. Providers with extra global flags subclass this."""

    provider: str = ""
    output: OutputOptions = field(default_factory=OutputOptions)
    diagnostics: DiagnosticsCollector = field(default_factory=DiagnosticsCollector)


def cfg(ctx: typer.Context) -> RuntimeConfig:
    obj = ctx.obj
    if isinstance(obj, RuntimeConfig):
        return obj
    return RuntimeConfig()


def cfg_as[T: RuntimeConfig](ctx: typer.Context, cls: type[T]) -> T:
    obj = ctx.obj
    if isinstance(obj, cls):
        return obj
    return cls()


# Shared option aliases so every callback declares identical flags and help text.
PrettyOption = Annotated[bool, typer.Option("--pretty", help="Pretty-print JSON for human reading. Default output is compact JSON.")]
CompactOption = Annotated[bool, typer.Option("--compact", help="Truncate long string fields to reduce payload size.")]
FieldsOption = Annotated[
    list[str] | None,
    typer.Option("--fields", help="Return only selected fields (dot paths). Repeat or pass comma-separated values."),
]
FieldsStrictOption = Annotated[
    bool, typer.Option("--fields-strict", help="Fail when a requested --fields dot-path is missing from the payload.")
]
ProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile", help="Output profile preset: agent (default compact JSON), human (pretty JSON), debug (pretty JSON + diagnostics)."
    ),
]
CompactMaxCharsOption = Annotated[
    int,
    typer.Option("--compact-max-chars", min=40, max=10_000, help="Maximum string length before --compact truncation adds an ellipsis."),
]


def configure(
    ctx: typer.Context,
    *,
    provider: str,
    pretty: bool = False,
    compact: bool = False,
    fields: list[str] | None = None,
    fields_strict: bool = False,
    profile: str | None = None,
    compact_max_chars: int = DEFAULT_COMPACT_MAX_CHARS,
    config: RuntimeConfig | None = None,
) -> RuntimeConfig:
    """Resolve the shared output flags into ``config`` (or a fresh RuntimeConfig) and store it in ``ctx.obj``."""
    try:
        output = resolve_output_options(
            profile=profile,
            pretty=pretty,
            compact=compact,
            compact_max_chars=compact_max_chars,
            fields=fields or [],
            fields_strict=fields_strict,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--profile") from exc
    resolved = config if config is not None else RuntimeConfig()
    resolved.provider = provider
    resolved.output = output
    ctx.obj = resolved
    global _ACTIVE_COMMAND
    _ACTIVE_COMMAND = ctx.invoked_subcommand
    return resolved


# Subcommand resolved by Click for the current process; ``guarded_run`` labels escaping errors with it.
_ACTIVE_COMMAND: str | None = None


def _first_positional(args: list[str]) -> str:
    """Best-effort command name when the callback never ran (for example a failure inside it)."""
    return next((arg for arg in args if not arg.startswith("-")), "")


def install_common_callback(app: typer.Typer, *, provider: str) -> None:
    """Register the shared global flags on ``app`` for providers that have no extra global options."""

    @app.callback()
    def _common(
        ctx: typer.Context,
        pretty: PrettyOption = False,
        compact: CompactOption = False,
        fields: FieldsOption = None,
        fields_strict: FieldsStrictOption = False,
        profile: ProfileOption = None,
        compact_max_chars: CompactMaxCharsOption = DEFAULT_COMPACT_MAX_CHARS,
    ) -> None:
        configure(
            ctx,
            provider=provider,
            pretty=pretty,
            compact=compact,
            fields=fields,
            fields_strict=fields_strict,
            profile=profile,
            compact_max_chars=compact_max_chars,
        )


def emit(ctx: typer.Context, payload: Mapping[str, Any], *, err: bool = False) -> None:
    config = cfg(ctx)
    try:
        emit_shaped(dict(payload), config.output, diagnostics=config.diagnostics, err=err)
    except OutputProjectionError as exc:
        fail(ctx, "missing_fields", str(exc), exit_code=EXIT_USAGE, details={"missing_fields": list(exc.missing_fields)})


def fail(
    ctx: typer.Context,
    code: str,
    message: str,
    *,
    exit_code: int | None = None,
    details: dict[str, Any] | None = None,
) -> NoReturn:
    """Write an error envelope to stderr and exit with the code mapped from ``code`` unless overridden."""
    config = cfg(ctx)
    payload = error_envelope(provider=config.provider, command=ctx.info_name or "", code=code, message=message, details=details)
    typer.echo(to_json(payload, pretty=config.output.pretty), err=True)
    raise typer.Exit(exit_code if exit_code is not None else exit_code_for(code))


def error_envelope_for(provider: str, command: str, exc: BaseException) -> tuple[Envelope, int]:
    """Map an escaping exception to ``(error envelope, exit code)`` per docs/foundation/ERROR_CONTRACT.md.

    ``guarded_run`` uses this at the process boundary; the ``warcraft`` wrapper uses it for in-process
    provider calls so both paths classify ``ProviderError`` and httpx failures identically.
    """

    def build(code: str, message: str, exit_code: int, details: dict[str, Any] | None = None) -> tuple[Envelope, int]:
        return error_envelope(provider=provider, command=command, code=code, message=message, details=details), exit_code

    if isinstance(exc, ProviderError):
        return build(exc.code, exc.message, exc.exit_code, exc.details)
    if isinstance(exc, httpx.TimeoutException):
        return build("timeout", str(exc) or "request timed out", EXIT_NETWORK)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        details = {"status_code": status, "url": str(exc.request.url)}
        if status in (401, 403):
            return build("auth_failed", str(exc), EXIT_AUTH, details)
        if status == 404:
            return build("not_found", str(exc), EXIT_NOT_FOUND, details)
        return build("upstream_error", str(exc), EXIT_NETWORK, details)
    if isinstance(exc, httpx.RequestError):
        return build("network_error", f"{type(exc).__name__}: {exc}", EXIT_NETWORK)
    return build("internal_error", f"{type(exc).__name__}: {exc}", EXIT_GENERIC)


def guarded_run(app: typer.Typer, *, provider: str) -> None:
    """Entry point for every binary: run ``app`` and turn any escaping exception into an error envelope.

    Usage errors (exit 2) and ``typer.Exit`` propagate as SystemExit untouched. Everything else is
    written to stderr as compact JSON with the contract exit code; a traceback is never printed.
    """
    global _ACTIVE_COMMAND
    _ACTIVE_COMMAND = None
    app.pretty_exceptions_enable = False
    try:
        app()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        command = _ACTIVE_COMMAND or _first_positional(sys.argv[1:])
        payload, exit_code = error_envelope_for(provider, command, exc)
        typer.echo(to_json(payload, pretty=False), err=True)
        raise SystemExit(exit_code) from exc
