from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import typer
from typer.testing import CliRunner
from warcraft_core.cli import RuntimeConfig, cfg, cfg_as, configure, emit, fail, guarded_run, install_common_callback
from warcraft_core.provider import ProviderError

runner = CliRunner()


def build_app() -> typer.Typer:
    app = typer.Typer(add_completion=False)
    install_common_callback(app, provider="dummy")

    @app.command("show")
    def show(ctx: typer.Context) -> None:
        emit(ctx, {"ok": True, "a": {"b": 1}, "long": "x" * 400})

    @app.command("missing")
    def missing(ctx: typer.Context) -> None:
        fail(ctx, "not_found", "nothing here")

    return app


def test_fields_projects_payload() -> None:
    result = runner.invoke(build_app(), ["--fields", "a.b", "show"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"a": {"b": 1}}


def test_fields_strict_missing_path_exits_2_with_missing_fields_error() -> None:
    result = runner.invoke(build_app(), ["--fields", "a.zz", "--fields-strict", "show"])
    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["ok"] is False
    assert error["provider"] == "dummy"
    assert error["command"] == "show"
    assert error["error"]["code"] == "missing_fields"
    assert error["error"]["details"] == {"missing_fields": ["a.zz"]}


def test_compact_truncates_long_strings() -> None:
    result = runner.invoke(build_app(), ["--compact", "--compact-max-chars", "50", "show"])
    assert result.exit_code == 0
    assert len(json.loads(result.stdout)["long"]) == 50


def test_profile_human_pretty_prints_and_bogus_profile_is_usage_error() -> None:
    result = runner.invoke(build_app(), ["--profile", "human", "show"])
    assert result.exit_code == 0
    assert result.stdout.startswith("{\n")
    bogus = runner.invoke(build_app(), ["--profile", "bogus", "show"])
    assert bogus.exit_code == 2
    assert "--profile" in bogus.stderr


def test_fail_uses_exit_code_mapping_and_emits_envelope_on_stderr() -> None:
    result = runner.invoke(build_app(), ["missing"])
    assert result.exit_code == 4
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["error"] == {"code": "not_found", "message": "nothing here"}
    assert error["schema_version"] == "1"


def test_configure_stores_subclass_config_in_ctx() -> None:
    @dataclass(slots=True)
    class WowheadConfig(RuntimeConfig):
        expansion: str = "retail"

    app = typer.Typer(add_completion=False)

    @app.callback()
    def main(ctx: typer.Context, pretty: bool = typer.Option(False, "--pretty")) -> None:
        configure(ctx, provider="wowhead", pretty=pretty, config=WowheadConfig(expansion="classic"))

    @app.command("show")
    def show(ctx: typer.Context) -> None:
        config = cfg_as(ctx, WowheadConfig)
        assert cfg(ctx) is config
        emit(ctx, {"expansion": config.expansion, "provider": config.provider, "pretty": config.output.pretty})

    result = runner.invoke(app, ["--pretty", "show"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"expansion": "classic", "provider": "wowhead", "pretty": True}


def _run_guarded(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exc: BaseException) -> tuple[int, dict[str, Any]]:
    app = typer.Typer(add_completion=False)
    install_common_callback(app, provider="dummy")

    @app.command("boom")
    def boom(ctx: typer.Context) -> None:
        raise exc

    monkeypatch.setattr(sys, "argv", ["dummy", "boom"])
    with pytest.raises(SystemExit) as exc_info:
        guarded_run(app, provider="dummy")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    code = exc_info.value.code
    assert isinstance(code, int)
    return code, json.loads(captured.err)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"status {status}", request=request, response=response)


@pytest.mark.parametrize(
    ("exc", "expected_code", "expected_exit"),
    [
        (httpx.ConnectError("refused"), "network_error", 5),
        (httpx.ReadTimeout("slow"), "timeout", 5),
        (_status_error(401), "auth_failed", 3),
        (_status_error(404), "not_found", 4),
        (_status_error(500), "upstream_error", 5),
        (ProviderError("auth_required", "login first"), "auth_required", 3),
        (ValueError("bad"), "internal_error", 1),
    ],
)
def test_guarded_run_maps_exceptions_to_error_envelopes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exc: BaseException,
    expected_code: str,
    expected_exit: int,
) -> None:
    exit_code, payload = _run_guarded(monkeypatch, capsys, exc)
    assert exit_code == expected_exit
    assert payload["ok"] is False
    assert payload["provider"] == "dummy"
    assert payload["command"] == "boom"
    assert payload["error"]["code"] == expected_code
    if isinstance(exc, httpx.HTTPStatusError):
        assert payload["error"]["details"]["status_code"] == exc.response.status_code
    if isinstance(exc, ValueError):
        assert payload["error"]["message"] == "ValueError: bad"


def test_guarded_run_names_the_command_when_global_flags_precede_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = typer.Typer(add_completion=False)
    install_common_callback(app, provider="dummy")

    @app.command("boom")
    def boom(ctx: typer.Context) -> None:
        raise ValueError("bad")

    monkeypatch.setattr(sys, "argv", ["dummy", "--pretty", "--profile", "human", "boom"])
    with pytest.raises(SystemExit):
        guarded_run(app, provider="dummy")
    payload = json.loads(capsys.readouterr().err)
    assert payload["command"] == "boom"


def test_guarded_run_lets_usage_errors_and_typer_exit_through(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    app = build_app()
    monkeypatch.setattr(sys, "argv", ["dummy", "--profile", "bogus", "show"])
    with pytest.raises(SystemExit) as usage:
        guarded_run(app, provider="dummy")
    assert usage.value.code == 2
    monkeypatch.setattr(sys, "argv", ["dummy", "missing"])
    with pytest.raises(SystemExit) as exit_info:
        guarded_run(app, provider="dummy")
    assert exit_info.value.code == 4
    assert json.loads(capsys.readouterr().err.splitlines()[-1])["error"]["code"] == "not_found"
