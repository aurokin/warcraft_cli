from __future__ import annotations

import json
import re
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

    @app.command("need")
    def need(ctx: typer.Context, target: str) -> None:
        emit(ctx, {"target": target})

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


def test_profile_human_pretty_prints() -> None:
    result = runner.invoke(build_app(), ["--profile", "human", "show"])
    assert result.exit_code == 0
    assert result.stdout.startswith("{\n")


def test_fields_reports_a_missing_path_instead_of_returning_an_empty_object() -> None:
    """``--fields nope`` used to print ``{}`` with exit 0, which reads as "no results"."""
    result = runner.invoke(build_app(), ["--fields", "nope", "show"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"fields_missing": ["nope"]}


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


def _run_argv(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    """Drive a binary exactly as its entry point does, with colour forced on.

    Rich styling used to split option names with ANSI escapes; the envelope this asserts on is
    written by warcraft_core itself, so it must be identical whatever the terminal wants.
    """
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exit_info:
        guarded_run(build_app(), provider="dummy")
    captured = capsys.readouterr()
    code = exit_info.value.code
    assert isinstance(code, int)
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    ("argv", "expected_command", "expected_message"),
    [
        (["dummy", "--profile", "bogus", "show"], "show", "Invalid value for --profile: --profile must be one of: agent, human"),
        (["dummy", "--bogus-flag", "show"], "show", "No such option: --bogus-flag"),
        (["dummy", "nosuchcommand"], "nosuchcommand", "No such command 'nosuchcommand'."),
        (["dummy", "need"], "need", "Missing argument 'target'."),
        (["dummy"], "", "Missing command."),
    ],
    ids=["bad-option-value", "unknown-flag", "unknown-command", "missing-argument", "no-command"],
)
def test_guarded_run_renders_usage_errors_as_the_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected_command: str,
    expected_message: str,
) -> None:
    """``command`` must name the subcommand even when the callback never ran: an option value is not one."""
    exit_code, out, err = _run_argv(monkeypatch, capsys, argv)
    assert exit_code == 2
    assert out == ""
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["provider"] == "dummy"
    assert payload["command"] == expected_command
    assert payload["error"] == {"code": "invalid_argument", "message": expected_message}


def test_guarded_run_keeps_help_as_human_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` is for humans: it must stay rendered help on stdout, not an error envelope."""
    exit_code, out, err = _run_argv(monkeypatch, capsys, ["dummy", "--help"])
    assert exit_code == 0
    assert err == ""
    assert "Usage" in re.sub(r"\x1b\[[0-9;]*m", "", out)


def test_guarded_run_propagates_the_exit_code_from_fail(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, _, err = _run_argv(monkeypatch, capsys, ["dummy", "missing"])
    assert exit_code == 4
    assert json.loads(err)["error"]["code"] == "not_found"


def test_guarded_run_exits_zero_on_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, out, _ = _run_argv(monkeypatch, capsys, ["dummy", "show"])
    assert exit_code == 0
    assert json.loads(out)["a"] == {"b": 1}
