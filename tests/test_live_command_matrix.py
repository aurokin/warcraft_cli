"""Exhaustive Warcraft Logs live command matrix (AUR-319)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner
from warcraft_core.auth import provider_auth_status
from warcraftlogs_cli.client import load_warcraftlogs_auth_config
from warcraftlogs_cli.main import app

from tests.fixtures.live_matrix import (
    PRIVATE_DIFFICULTY,
    PRIVATE_REPORT_CODE,
    PUBLIC_DIFFICULTY,
    PUBLIC_REPORT_CODE,
)
from tests.fixtures.wcl_matrix_cases import (
    AuthRequirement,
    LiveMatrixContext,
    MatrixCase,
    _url,
    matrix_cases,
)

runner = CliRunner()


def _payload_for(args: list[str]) -> dict[str, Any]:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _require_client_auth() -> None:
    if not load_warcraftlogs_auth_config().configured:
        pytest.skip("Warcraft Logs credentials are not configured.")


def _require_user_auth() -> None:
    state = provider_auth_status("warcraftlogs")
    if not (
        state.get("has_access_token")
        and state.get("auth_mode") in {"authorization_code", "pkce"}
        and not state.get("expired")
    ):
        pytest.skip("Warcraft Logs user auth token is not configured.")


def _pick_fight(code: str, *, difficulty: int) -> int:
    payload = _payload_for(["report-fights", code, "--difficulty", str(difficulty)])
    block = payload.get("report_fights") or payload.get("fights")
    if isinstance(block, dict):
        fights = block.get("fights", block)
    else:
        fights = block
    assert isinstance(fights, list), payload
    for fight in fights:
        fight_id = fight.get("id")
        if isinstance(fight_id, int):
            return fight_id
    pytest.skip(f"No fights found for report {code} at difficulty {difficulty}.")


@pytest.fixture(scope="module")
def live_matrix_context() -> LiveMatrixContext:
    _require_client_auth()
    public_fight_id = _pick_fight(PUBLIC_REPORT_CODE, difficulty=PUBLIC_DIFFICULTY)
    public_report_url = _url(PUBLIC_REPORT_CODE, public_fight_id)

    private_report_url: str | None = None
    private_fight_id: int | None = None
    state = provider_auth_status("warcraftlogs")
    if (
        state.get("has_access_token")
        and state.get("auth_mode") in {"authorization_code", "pkce"}
        and not state.get("expired")
    ):
        try:
            private_fight_id = _pick_fight(PRIVATE_REPORT_CODE, difficulty=PRIVATE_DIFFICULTY)
            private_report_url = _url(PRIVATE_REPORT_CODE, private_fight_id)
        except AssertionError:
            private_report_url = None
            private_fight_id = None

    return LiveMatrixContext(
        public_report_url=public_report_url,
        public_fight_id=public_fight_id,
        private_report_url=private_report_url,
        private_fight_id=private_fight_id,
    )


def _auth_skip(case: MatrixCase) -> None:
    if case.auth == AuthRequirement.CLIENT:
        _require_client_auth()
    elif case.auth == AuthRequirement.USER:
        _require_client_auth()
        _require_user_auth()
    elif case.auth == AuthRequirement.PRIVATE:
        _require_client_auth()
        _require_user_auth()


def _assert_nonempty(payload: dict[str, Any], *, canonical_key: str, leaf: str) -> None:
    body = payload[canonical_key]
    if isinstance(body, dict):
        value: Any = body.get(leaf, body)
    else:
        value = body
    if isinstance(value, list):
        assert value, f"expected non-empty list at {canonical_key}.{leaf}"
    elif isinstance(value, dict):
        assert value, f"expected non-empty dict at {canonical_key}.{leaf}"
    elif value is None:
        pytest.fail(f"expected data at {canonical_key}.{leaf}, got null")


@pytest.mark.live
@pytest.mark.parametrize("case", matrix_cases(), ids=lambda case: case.case_id)
def test_live_warcraftlogs_command_matrix(case: MatrixCase, live_matrix_context: LiveMatrixContext) -> None:
    _auth_skip(case)
    if case.auth == AuthRequirement.PRIVATE and live_matrix_context.private_report_url is None:
        pytest.skip("Private report fixtures require user auth and view-private-reports scope.")

    args = case.build_args(live_matrix_context)
    payload = _payload_for(args)

    assert payload.get("ok") is not False
    assert payload["provider"] == "warcraftlogs"

    if case.command == "auth":
        assert case.nonempty_leaf in payload
        return

    assert payload.get("command") == case.command
    assert case.canonical_key in payload
    if case.nonempty_leaf:
        _assert_nonempty(payload, canonical_key=case.canonical_key, leaf=case.nonempty_leaf)
