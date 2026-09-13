"""Exhaustive Warcraft Logs live command matrix (AUR-319).

Retail tiers roll over and reports age out of retention, so the matrix pins only the guild and
character identities (``tests/fixtures/live_matrix.py``) and discovers everything else at runtime:
the current raid zone, a boss and difficulty that a recent public report actually killed, and the
ability and actor IDs inside that kill. Sampled-analytics cases are scoped to a report-list window
centred on that anchor report, so their cohort provably contains the anchor kill and a non-empty
result is a real guarantee rather than a hope about the live report firehose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from typer.testing import CliRunner
from warcraft_core.auth import provider_auth_status
from warcraftlogs_cli.client import load_warcraftlogs_auth_config
from warcraftlogs_cli.main import app

from tests.fixtures.live_matrix import (
    ANCHOR_DIFFICULTIES,
    DISCOVERY_REPORT_LIMIT,
    GUILD_NAME,
    GUILD_REALM,
    GUILD_REGION,
    RAID_DIFFICULTY_IDS,
    SAMPLE_WINDOW_PADDING_MS,
)
from tests.fixtures.wcl_matrix_cases import (
    AuthRequirement,
    DataCheck,
    LiveMatrixContext,
    MatrixCase,
    _url,
    matrix_cases,
)

runner = CliRunner()

GUILD = [GUILD_REGION, GUILD_REALM, GUILD_NAME]


def _payload_for(args: list[str]) -> dict[str, Any]:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _discovery_payload_for(args: list[str]) -> dict[str, Any] | None:
    """Run a discovery command, treating failure as "this input is unusable", not as a test failure."""
    result = runner.invoke(app, args)
    if result.exit_code != 0:
        return None
    return json.loads(result.stdout)


def _dig(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Walk ``path`` from the envelope root, failing with the keys that were actually there."""
    value: Any = payload
    for depth, segment in enumerate(path):
        if not isinstance(value, dict) or segment not in value:
            available = sorted(value) if isinstance(value, dict) else type(value).__name__
            pytest.fail(f"missing {'.'.join(path[: depth + 1])}; available at that level: {available}")
        value = value[segment]
    return value


def _optional_dig(payload: dict[str, Any] | None, path: tuple[str, ...]) -> Any:
    value: Any = payload
    for segment in path:
        if not isinstance(value, dict):
            return None
        value = value.get(segment)
    return value


def _has_user_auth() -> bool:
    state = provider_auth_status("warcraftlogs")
    return bool(
        state.get("has_access_token") and state.get("auth_mode") in {"authorization_code", "pkce"} and not state.get("expired")
    )


def _require_client_auth() -> None:
    if not load_warcraftlogs_auth_config().configured:
        pytest.skip("Warcraft Logs credentials are not configured.")


def _require_user_auth() -> None:
    if not _has_user_auth():
        pytest.skip("Warcraft Logs user auth token is not configured.")


@dataclass(frozen=True)
class _Anchor:
    """A public report with a real boss kill; every volatile matrix input hangs off this."""

    report_code: str
    report_start_ms: int
    fight_id: int
    boss_id: int
    difficulty: int


def _current_raid_zone_id() -> int | None:
    zones = _payload_for(["zones"])["zones"]
    raids = [zone for zone in zones if {row.get("id") for row in zone.get("difficulties", [])} >= RAID_DIFFICULTY_IDS]
    open_raids = [zone for zone in raids if not zone.get("frozen")]
    candidates = open_raids or raids
    if not candidates:
        return None
    newest = max(candidates, key=lambda zone: (_optional_dig(zone, ("expansion", "id")) or 0, zone.get("id") or 0))
    zone_id = newest.get("id")
    return zone_id if isinstance(zone_id, int) else None


def _anchor_from_report(report: dict[str, Any]) -> _Anchor | None:
    code = report.get("code")
    start_ms = report.get("start_time")
    if not isinstance(code, str) or not isinstance(start_ms, int):
        return None
    payload = _discovery_payload_for(["report-fights", code])
    fights = _optional_dig(payload, ("report_fights", "fights"))
    if not isinstance(fights, list):
        return None
    for difficulty in ANCHOR_DIFFICULTIES:
        for fight in fights:
            if not isinstance(fight, dict) or fight.get("kill") is not True or fight.get("difficulty") != difficulty:
                continue
            boss_id, fight_id = fight.get("encounter_id"), fight.get("id")
            if isinstance(boss_id, int) and isinstance(fight_id, int):
                return _Anchor(report_code=code, report_start_ms=start_ms, fight_id=fight_id, boss_id=boss_id, difficulty=difficulty)
    return None


def _discover_anchor(zone_id: int) -> _Anchor | None:
    reports = _payload_for(["reports", "--zone-id", str(zone_id), "--limit", str(DISCOVERY_REPORT_LIMIT)])["reports"]
    for report in reports:
        if not isinstance(report, dict) or report.get("visibility") not in {None, "public"}:
            continue
        anchor = _anchor_from_report(report)
        if anchor is not None:
            return anchor
    return None


def _discover_aura_ability_id(report_url: str) -> int | None:
    payload = _discovery_payload_for(["report-encounter-buffs", report_url, "--preview-limit", "15"])
    rows = _optional_dig(payload, ("report_encounter_buffs", "buffs", "preview"))
    if not isinstance(rows, list):
        return None
    for row in rows:
        game_id = _optional_dig(row if isinstance(row, dict) else None, ("aura", "game_id"))
        if isinstance(game_id, int):
            return game_id
    return None


def _discover_player_actor_id(report_code: str, fight_id: int) -> int | None:
    payload = _discovery_payload_for(["report-player-details", report_code, "--fight-id", str(fight_id)])
    roles = _optional_dig(payload, ("report_player_details", "roles"))
    if not isinstance(roles, dict):
        return None
    for players in roles.values():
        for player in players if isinstance(players, list) else []:
            actor_id = player.get("id") if isinstance(player, dict) else None
            if isinstance(actor_id, int):
                return actor_id
    return None


def _discover_cast_ability_id(report_code: str, fight_id: int, actor_id: int | None) -> int | None:
    """An ability the anchor kill's own player actor cast, so the sampled cohort has to see it."""
    if actor_id is None:
        return None
    payload = _discovery_payload_for(
        ["report-events", report_code, "--fight-id", str(fight_id), "--data-type", "casts", "--limit", "100"]
    )
    events = _optional_dig(payload, ("report_events",))
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, dict) or event.get("sourceID") != actor_id:
            continue
        ability_id = event.get("abilityGameID")
        if isinstance(ability_id, int):
            return ability_id
    return None


def _discover_private_report() -> tuple[str, str] | None:
    """The guild's own most recent private report, which only a user token can read."""
    payload = _discovery_payload_for(["guild-reports", *GUILD, "--limit", str(DISCOVERY_REPORT_LIMIT)])
    reports = _optional_dig(payload, ("guild_reports",))
    if not isinstance(reports, list):
        return None
    for report in reports:
        code = report.get("code") if isinstance(report, dict) else None
        if not isinstance(code, str) or report.get("visibility") != "private":
            continue
        fights = _optional_dig(_discovery_payload_for(["report-fights", code]), ("report_fights", "fights"))
        for fight in fights if isinstance(fights, list) else []:
            fight_id = fight.get("id") if isinstance(fight, dict) else None
            if isinstance(fight_id, int):
                return code, _url(code, fight_id)
    return None


@pytest.fixture(scope="session")
def live_matrix_context() -> LiveMatrixContext | None:
    if not load_warcraftlogs_auth_config().configured:
        return None
    zone_id = _current_raid_zone_id()
    if zone_id is None:
        return None
    anchor = _discover_anchor(zone_id)
    if anchor is None:
        return None

    report_url = _url(anchor.report_code, anchor.fight_id)
    actor_id = _discover_player_actor_id(anchor.report_code, anchor.fight_id)
    private_report = _discover_private_report() if _has_user_auth() else None

    return LiveMatrixContext(
        zone_id=zone_id,
        boss_id=anchor.boss_id,
        difficulty=anchor.difficulty,
        sample_start_ms=anchor.report_start_ms - SAMPLE_WINDOW_PADDING_MS,
        sample_end_ms=anchor.report_start_ms + SAMPLE_WINDOW_PADDING_MS,
        public_report_code=anchor.report_code,
        public_report_url=report_url,
        public_fight_id=anchor.fight_id,
        aura_ability_id=_discover_aura_ability_id(report_url),
        player_actor_id=actor_id,
        cast_ability_id=_discover_cast_ability_id(anchor.report_code, anchor.fight_id, actor_id),
        private_report_code=private_report[0] if private_report else None,
        private_report_url=private_report[1] if private_report else None,
    )


def _auth_skip(case: MatrixCase) -> None:
    _require_client_auth()
    if case.auth in {AuthRequirement.USER, AuthRequirement.PRIVATE}:
        _require_user_auth()


def _assert_sampling_contract(payload: dict[str, Any], ctx: LiveMatrixContext) -> None:
    """Sampled analytics must declare the cohort they drew from, whatever the cohort contained."""
    filters = _dig(payload, ("sample_scope", "filters"))
    assert filters["zone_id"] == ctx.zone_id
    assert filters["boss_id"] == ctx.boss_id
    assert filters["difficulty"] == ctx.difficulty
    returned = _dig(payload, ("sample_scope", "returned"))
    assert isinstance(returned, int) and returned >= 0, f"sample_scope.returned is {returned!r}"
    assert isinstance(_dig(payload, ("citations",)), dict)
    assert isinstance(_dig(payload, ("freshness", "sampled_at")), str)
    assert _dig(payload, ("cache_provenance", "finished")) is True


def _assert_data(payload: dict[str, Any], case: MatrixCase) -> None:
    value = _dig(payload, case.data_path)
    where = ".".join(case.data_path)
    if case.check is DataCheck.NONEMPTY:
        assert isinstance(value, list | dict), f"expected a collection at {where}, got {type(value).__name__}"
        assert value, f"expected a non-empty collection at {where}"
    elif case.check is DataCheck.POSITIVE:
        assert isinstance(value, int | float) and not isinstance(value, bool), f"expected a number at {where}, got {value!r}"
        assert value > 0, f"expected a positive number at {where}, got {value!r}"
    elif case.check is DataCheck.TRUE:
        assert value is True, f"expected {where} to be True, got {value!r}"
    elif case.check is DataCheck.PRESENT:
        assert value is not None, f"expected data at {where}, got null"
    else:
        assert isinstance(value, list), f"expected a (possibly empty) sampled list at {where}, got {type(value).__name__}"


@pytest.mark.live
@pytest.mark.parametrize("case", matrix_cases(), ids=lambda case: case.case_id)
def test_live_warcraftlogs_command_matrix(case: MatrixCase, live_matrix_context: LiveMatrixContext | None) -> None:
    if live_matrix_context is None:
        pytest.skip("Warcraft Logs live matrix inputs are unavailable (credentials, or no recent public kill to anchor on).")

    _auth_skip(case)
    try:
        args = case.build_args(live_matrix_context)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    payload = _payload_for(args)

    assert payload["ok"] is True
    assert payload["provider"] == "warcraftlogs"
    assert payload["command"] == case.command
    assert case.canonical_key in payload
    if case.sampled:
        _assert_sampling_contract(payload, live_matrix_context)
    _assert_data(payload, case)
