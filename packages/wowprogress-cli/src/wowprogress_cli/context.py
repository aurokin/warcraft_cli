from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import typer
from warcraft_core.output import emit

from wowprogress_cli.client import WowProgressClientError


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


def _client(ctx: typer.Context):
    from wowprogress_cli.main import WowProgressClient

    try:
        return WowProgressClient()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        raise AssertionError("unreachable") from None


def _handle_client_error(ctx: typer.Context, exc: WowProgressClientError) -> None:
    _fail(ctx, exc.code, exc.message)
