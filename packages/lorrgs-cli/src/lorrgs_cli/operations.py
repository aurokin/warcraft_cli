"""Typed Lorrgs operations consumed by cooldown evidence workflows."""

from __future__ import annotations

from warcraft_core.envelope import Envelope
from warcraft_core.exit_codes import EXIT_USAGE
from warcraft_core.provider import ProviderError

from lorrgs_cli.provider import call_api, call_spec_api, note_empty_ranking, validated_difficulty
from lorrgs_cli.search import parse_report_reference


def bosses() -> Envelope:
    return call_api("bosses", "bosses", {}, lambda client: client.bosses())


def spec_spells(spec_slug: str) -> Envelope:
    return call_spec_api("spec-spells", "spec_spells", spec_slug, {"spec_slug": spec_slug}, lambda client, slug: client.spec_spells(slug))


def boss_spells(boss_slug: str) -> Envelope:
    return call_api("boss-spells", "boss_spells", {"boss_slug": boss_slug}, lambda client: client.boss_spells(boss_slug))


def spec_ranking(spec_slug: str, boss_slug: str, *, difficulty: str = "mythic", metric: str | None = None) -> Envelope:
    query = {"spec_slug": spec_slug, "boss_slug": boss_slug, "difficulty": difficulty, "metric": metric}
    return call_spec_api(
        "spec-ranking",
        "spec_ranking",
        spec_slug,
        query,
        lambda client, slug: note_empty_ranking(
            client.spec_ranking(spec_slug=slug, boss_slug=boss_slug, difficulty=validated_difficulty(difficulty), metric=metric),
            f"{slug} reports for {boss_slug} on {difficulty}",
        ),
    )


def user_report_fights(
    report_ref: str,
    *,
    fight: str | None = None,
    fight_ids: list[int] | None = None,
    player: str | None = None,
    data_type: str | None = None,
) -> Envelope:
    ref = parse_report_reference(report_ref)
    if ref is None:
        raise ProviderError(
            "invalid_report_ref",
            "Expected a Warcraft Logs report URL, Lorrgs user_report URL, or 16-character report code.",
            exit_code=EXIT_USAGE,
        )
    if fight and fight_ids:
        raise ProviderError("invalid_query", "Pass --fight or --fight-id, not both.")
    selected = fight or ".".join(map(str, fight_ids or [])) or (str(ref.fight_id) if ref.fight_id is not None else None)
    if not selected:
        raise ProviderError(
            "missing_fight", "Pass --fight (or --fight-id) or provide a report URL containing fight=<id>.", exit_code=EXIT_USAGE
        )
    selected_type = data_type or ref.report_type
    query = {"report_ref": report_ref, "report_id": ref.code, "fight": selected, "player": player, "type": selected_type}
    return call_api(
        "user-report-fights",
        "user_report_fights",
        query,
        lambda client: client.user_report_fights(report_id=ref.code, fight=selected, player=player, data_type=selected_type),
    )
