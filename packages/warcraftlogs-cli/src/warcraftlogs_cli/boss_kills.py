"""Sampled boss-kill analytics across Warcraft Logs reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from warcraft_core.analytics import numeric_summary
from warcraft_core.timestamps import iso_now_utc
from warcraft_core.wow_specs import WOW_CLASS_NAMES, WowSpec, lookup_spec

from warcraftlogs_cli.client import ReportPlayerDetailsOptions, WarcraftLogsClient
from warcraftlogs_cli.report_payloads import fight_payload, report_brief_payload, report_payload, report_url
from warcraftlogs_cli.sampling_utils import (
    boss_matches,
    dict_at,
    fight_duration_ms,
    list_at,
    normalize_match_text,
    report_is_finished,
    sampled_spec_filter_notes,
)


@dataclass(frozen=True, slots=True)
class CrossReportScope:
    """Cohort-shaping inputs shared by every sampled cross-report command.

    Field order is the emitted ``query`` key order: ``dataclasses.asdict`` on this
    object is what the sampled payloads echo back to the caller. ``top`` is the
    returned-row cap; commands without a ``--limit`` (``--top``) flag leave it at the default and
    drop it from their query echo.
    """

    zone_id: int
    boss_id: int | None = None
    boss_name: str | None = None
    difficulty: int | None = None
    spec_name: str | None = None
    kill_time_min: float | None = None
    kill_time_max: float | None = None
    top: int = 10
    report_pages: int = 1
    reports_per_page: int = 25
    start_time: float | None = None
    end_time: float | None = None
    guild_region: str | None = None
    guild_realm: str | None = None
    guild_name: str | None = None


def retail_specs_named(text: str) -> set[WowSpec]:
    """Every retail spec ``text`` names in any provider's spelling or shorthand (Frost Mage, bm, hunter-beastmastery):
    one, or each class's for a bare spec several classes share (Frost)."""
    return {spec for class_key in WOW_CLASS_NAMES if (spec := lookup_spec(text, class_hint=class_key))}


def matching_specs(actor: dict[str, Any], spec_name: str) -> list[dict[str, Any]]:
    """The actor's spec rows that ``spec_name`` names; the actor's ``type`` is its class.

    A ``spec_name`` outside the retail table (a classic site's Combat) matches a row's spec by name,
    bare or with its class in either order (Combat Rogue).
    """
    wanted = retail_specs_named(spec_name)
    actor_class = str(actor.get("type") or "")

    def names(row_spec: str) -> bool:
        if wanted:
            return lookup_spec(row_spec, class_hint=actor_class) in wanted
        spec_text, class_text = normalize_match_text(row_spec), normalize_match_text(actor_class)
        return bool(spec_text) and normalize_match_text(spec_name) in {spec_text, spec_text + class_text, class_text + spec_text}

    return [spec for spec in list_at(actor, "specs") if isinstance(spec, dict) and names(str(spec.get("spec") or ""))]


def player_details_roles(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Tank, healer and dps rows of a playerDetails response, in that order."""
    details = dict_at(report, "playerDetails")
    data = dict_at(details, "data")
    role_data = dict_at(data, "playerDetails") or data
    roles: dict[str, list[dict[str, Any]]] = {}
    for role in ("tanks", "healers", "dps"):
        roles[role] = [row for row in list_at(role_data, role) if isinstance(row, dict)]
    return roles


def matching_spec_players(report: dict[str, Any], *, spec_name: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for role, rows in player_details_roles(report).items():
        for row in rows:
            specs = matching_specs(row, spec_name)
            if specs:
                matches.append(
                    {"name": row.get("name"), "id": row.get("id"), "role": role, "type": row.get("type"), "matching_specs": specs}
                )
    return matches


def boss_kill_row(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    matching_players: list[dict[str, Any]] | None = None,
    duplicate_reports: list[dict[str, Any]] | None = None,
    possible_duplicate_of: dict[str, Any] | None = None,
) -> dict[str, Any]:
    duration_ms = fight_duration_ms(fight)
    return {
        "report": report_brief_payload(report),
        "report_finished": report_is_finished(report),
        "guild": report_payload(report).get("guild"),
        "fight": fight_payload(fight),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 2) if duration_ms is not None else None,
        "matching_players": matching_players or [],
        # Other reports of this same pull, collapsed into this row by deduplicate_pulls.
        "duplicate_reports": duplicate_reports or [],
        # A kill timed like an earlier sampled kill, one of the two without a guild, whose roster could not be confirmed equal.
        "possible_duplicate_of": possible_duplicate_of,
    }


# Two raiders in one group each uploading the pull yields two reports of a single kill. Their
# combat logs start seconds apart, so the same pull lands at slightly different wall-clock
# boundaries; anything further apart than this is a different pull.
DUPLICATE_PULL_TOLERANCE_MS = 5000
# One set of players cannot be in two kills of an encounter whose starts and ends are each this
# close, so when the rosters match a wider bound absorbs uploaders' clock skew (seen at 5.2 s live).
ROSTER_MATCH_TOLERANCE_MS = 30000


@dataclass(frozen=True, slots=True)
class _PullIdentity:
    """Everything except timing and uploader that has to agree before two sampled fights can be one pull."""

    encounter_id: int | None
    difficulty: int | None
    size: int | None


# The players in one sampled fight, for telling two logs of one pull from two pulls.
RosterLookup = Callable[[dict[str, Any], dict[str, Any]], frozenset[str]]


def _pull_identity(fight: dict[str, Any]) -> _PullIdentity:
    return _PullIdentity(
        encounter_id=fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None,
        difficulty=fight.get("difficulty") if isinstance(fight.get("difficulty"), int) else None,
        size=fight.get("size") if isinstance(fight.get("size"), int) else None,
    )


def _absolute_fight_window_ms(report: dict[str, Any], fight: dict[str, Any]) -> tuple[float, float] | None:
    """Wall-clock ``(start, end)`` of a fight: report start plus the report-relative fight offsets."""
    report_start = report.get("startTime")
    fight_start = fight.get("startTime")
    fight_end = fight.get("endTime")
    if not isinstance(report_start, (int, float)) or not isinstance(fight_start, (int, float)):
        return None
    if not isinstance(fight_end, (int, float)):
        return None
    return float(report_start) + float(fight_start), float(report_start) + float(fight_end)


def _same_window(first: tuple[float, float], second: tuple[float, float], tolerance_ms: int = DUPLICATE_PULL_TOLERANCE_MS) -> bool:
    """Start and end both within ``tolerance_ms``."""
    return all(abs(mine - other) <= tolerance_ms for mine, other in zip(first, second, strict=True))


@dataclass(slots=True)
class SampledPull:
    """One real pull, plus the citations of the other reports that logged the same pull."""

    report: dict[str, Any]
    fight: dict[str, Any]
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    possible_duplicate_of: dict[str, Any] | None = None


@dataclass(slots=True)
class _PullCluster:
    """A kept pull, the guild that logged it, and the window of every fight folded into it."""

    pull: SampledPull
    guild_id: int | None
    windows: list[tuple[float, float]]

    def within(self, window: tuple[float, float], tolerance_ms: int = DUPLICATE_PULL_TOLERANCE_MS) -> bool:
        return any(_same_window(member, window, tolerance_ms) for member in self.windows)


def _pull_citation(report: dict[str, Any], fight: dict[str, Any]) -> dict[str, Any]:
    return {"report_code": report.get("code"), "fight_id": fight.get("id")}


def deduplicate_pulls(
    candidates: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    *,
    roster: RosterLookup | None = None,
) -> list[SampledPull]:
    """Collapse one real pull logged in several reports into a single sampled kill.

    Warcraft Logs exposes no cross-report pull ID, so the match is deliberately narrow and is
    labelled in the payload rather than inferred silently (docs/foundation/SAFE_ANALYTICS_RULES.md).
    Fights must share encounter, difficulty and raid size, and then either come from the same guild
    with wall-clock start *and* end within ``DUPLICATE_PULL_TOLERANCE_MS``, or list the same
    non-empty set of players (``roster``) within ``ROSTER_MATCH_TOLERANCE_MS``. Timing is checked
    against every fight already folded into a pull, so an unrelated pull starting in between cannot
    split one double-logged pull in two. Fights are clustered in start order, so the answer does not
    depend on the order reports were listed in, and the earliest-starting report represents the
    pull. A fight without a computable window is always kept on its own.

    Timing alone cannot tell two unrelated personal logs apart, so a fight timed like an earlier pull
    whose roster does not match, where either side has no guild, is kept and marked
    ``possible_duplicate_of`` that pull.
    """
    kept: list[SampledPull] = []
    timed: list[tuple[tuple[float, float], dict[str, Any], dict[str, Any]]] = []
    for report, fight in candidates:
        window = _absolute_fight_window_ms(report, fight)
        if window is None:
            kept.append(SampledPull(report=report, fight=fight))
        else:
            timed.append((window, report, fight))
    timed.sort(key=lambda row: (row[0], str(row[1].get("code") or ""), str(row[2].get("id"))))
    clusters: dict[_PullIdentity, list[_PullCluster]] = {}
    for window, report, fight in timed:
        pulls = clusters.setdefault(_pull_identity(fight), [])
        guild_id = dict_at(report, "guild").get("id")
        guild_id = guild_id if isinstance(guild_id, int) else None
        near = [known for known in pulls if known.within(window, ROSTER_MATCH_TOLERANCE_MS)]
        match = next(
            (known for known in near if guild_id is not None and known.guild_id == guild_id and known.within(window)), None
        ) or next((known for known in near if _same_roster(roster, known.pull, report, fight)), None)
        if match is not None:
            match.pull.duplicates.append(_pull_citation(report, fight))
            match.windows.append(window)
            continue
        pull = SampledPull(report=report, fight=fight, possible_duplicate_of=_possible_duplicate_of(near, window, guild_id))
        kept.append(pull)
        pulls.append(_PullCluster(pull=pull, guild_id=guild_id, windows=[window]))
    return kept


def _possible_duplicate_of(
    near: list[_PullCluster], window: tuple[float, float], guild_id: int | None
) -> dict[str, Any] | None:
    """Cite the earlier pull timed like this one when either side has no guild to tell them apart."""
    close = next((known for known in near if known.within(window) and None in (guild_id, known.guild_id)), None)
    return _pull_citation(close.pull.report, close.pull.fight) if close is not None else None


def _same_roster(roster: RosterLookup | None, pull: SampledPull, report: dict[str, Any], fight: dict[str, Any]) -> bool:
    if roster is None:
        return False
    players = roster(pull.report, pull.fight)
    return bool(players) and players == roster(report, fight)


def sampled_cohort_notes(sample: dict[str, Any]) -> list[str]:
    """Say so in the payload when sampled kills were collapsed or none matched, per SAFE_ANALYTICS_RULES.md."""
    notes: list[str] = []
    removed = sample.get("duplicates_removed")
    if isinstance(removed, int) and removed > 0:
        notes.append(
            f"{removed} sampled fight(s) were the same pull logged in more than one report (same encounter, "
            f"difficulty and raid size, and either the same guild with start and end within "
            f"{DUPLICATE_PULL_TOLERANCE_MS // 1000}s or the same players within {ROSTER_MATCH_TOLERANCE_MS // 1000}s) "
            "and were collapsed into one kill; the collapsed report codes are on each kill's duplicate_reports"
        )
    possible = sample.get("possible_duplicates")
    if isinstance(possible, int) and possible > 0:
        notes.append(
            f"{possible} sampled kill(s) are timed like an earlier sampled kill, one of the two from a report without a "
            "guild, but their rosters could not be confirmed equal, so they were kept; each carries possible_duplicate_of. Drop them "
            "before counting if they are the same pull"
        )
    difficulties = sample.get("difficulty_counts") or []
    if len(difficulties) > 1:
        notes.append(
            f"The cohort mixes {len(difficulties)} difficulties (see difficulty_counts); kill times and frequencies "
            "across difficulties are not comparable. Pass --difficulty to sample one"
        )
    keystone_levels = sample.get("keystone_level_counts") or []
    if len(keystone_levels) > 1:
        notes.append(
            f"The cohort mixes {len(keystone_levels)} keystone levels (see keystone_level_counts); kills are ordered by "
            "raw duration, so a faster run at a lower key ranks ahead of a higher one. Compare durations within one "
            "fight.keystone_level; fight.keystone_time_ms is the in-game key timer"
        )
    if sample.get("matched_boss_kill_count") == 0:
        notes.append(
            f"No kill matched in the {sample.get('source_report_count')} sampled reports "
            f"({sample.get('scanned_fight_count')} fights scanned): the cohort is empty, which says nothing about the "
            "boss. Raise --report-pages or narrow --start-time/--end-time to reach more kills, or use "
            "encounter-rankings, which ranks every logged kill, for a late or rarely killed boss"
        )
    return notes


def sampled_cross_report_freshness(
    cache_ttl_seconds: int | None = None,
    *,
    transport_counts: dict[str, int],
) -> dict[str, Any]:
    """Freshness for one sampled payload.

    ``sampled_at`` is when the command ran, not when the cohort was fetched: report listings and
    report details can be served from cache up to ``cache_ttl_seconds`` old. The transport counts
    make that visible, so a warm-cache rerun is distinguishable from a live scan.
    """
    upstream_request_count = transport_counts.get("upstream_request_count", 0)
    cache_hit_count = transport_counts.get("cache_hit_count", 0)
    return {
        "sampled_at": iso_now_utc(),
        "cache_ttl_seconds": cache_ttl_seconds,
        "cache_hit_count": cache_hit_count,
        "upstream_request_count": upstream_request_count,
        "served_entirely_from_cache": upstream_request_count == 0 and cache_hit_count > 0,
    }


def sampled_cache_provenance(cache_ttl_seconds: int | None, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Cache provenance for a sampled cohort of kill rows.

    Kills are sampled from finished and still-logging reports alike (a kill fight is final once it
    ends), so the cohort is ``live`` when any kill came from a report still being logged. The client
    caches a live report under the short report TTL; ``cache_ttl_seconds`` is the finished TTL.
    """
    live = any(row.get("report_finished") is False for row in rows)
    return {
        "finished": not live,
        "live": live,
        "cache_ttl_seconds": cache_ttl_seconds,
        "source": "sampled_reports",
    }


def sampled_sample_scope(
    *,
    ranking_basis: str,
    query: dict[str, Any],
    returned: int,
    excluded: int,
    truncated: bool,
) -> dict[str, Any]:
    """Consolidated scope block: ranking basis + cohort filters + returned/excluded counts.

    ``filters`` is a copy of the full ``query`` already emitted on the payload — every
    cohort-shaping input (boss/zone/difficulty/spec/kill-time, guild scope, time window,
    report-page budget) is preserved so two runs with different scope never look identical.
    """
    return {
        "ranking_basis": ranking_basis,
        "filters": dict(query) if isinstance(query, dict) else {},
        "returned": returned,
        "excluded": excluded,
        "truncated": truncated,
    }


def _row_citation_pairs(row: dict[str, Any]) -> list[tuple[str, int | None]]:
    """A row's own ``(report code, fight id)``, then every report that logged the same pull.

    The collapsed reports stay citable: a caller holding one of them must still be able to find
    the kill it was folded into.
    """
    candidates = [(dict_at(row, "report").get("code"), dict_at(row, "fight").get("id"))]
    candidates += [
        (entry.get("report_code"), entry.get("fight_id"))
        for entry in list_at(row, "duplicate_reports")
        if isinstance(entry, dict)
    ]
    return [
        (code, fight_id if isinstance(fight_id, int) else None)
        for code, fight_id in candidates
        if isinstance(code, str)
    ]


def sampled_cross_report_citations(
    rows: list[dict[str, Any]],
    *,
    limit: int = 20,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    sample_reports: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    for report_code, fight_id in (pair for row in rows for pair in _row_citation_pairs(row)):
        if (report_code, fight_id) in seen:
            continue
        seen.add((report_code, fight_id))
        sample_reports.append(
            {
                "report_code": report_code,
                "fight_id": fight_id,
                "report_url": report_url(report_code, fight_id=fight_id, root_url=root_url),
            }
        )
        if len(sample_reports) >= limit:
            break
    return {
        "sample_reports": sample_reports,
    }


def duration_bucket_rows(values: list[float], *, bucket_seconds: int) -> list[dict[str, Any]]:
    if not values:
        return []
    counts: dict[int, int] = {}
    for value in values:
        bucket_start = int(value // bucket_seconds) * bucket_seconds
        counts[bucket_start] = counts.get(bucket_start, 0) + 1
    rows = []
    total = len(values)
    for bucket_start, count in sorted(counts.items()):
        bucket_end = bucket_start + bucket_seconds
        rows.append(
            {
                "start_seconds": bucket_start,
                "end_seconds": bucket_end,
                "count": count,
                "percent": round((count / total) * 100, 2),
            }
        )
    return rows


def _fetch_zone_report_rows(
    client: WarcraftLogsClient,
    *,
    zone_id: int,
    guild_region: str | None,
    guild_realm: str | None,
    guild_name: str | None,
    report_pages: int,
    reports_per_page: int,
    start_time: float | None,
    end_time: float | None,
) -> list[dict[str, Any]]:
    # The listing shifts while live reports move up between page fetches, so one report can come
    # back on two pages; keep its first occurrence so it is neither counted twice nor cited as its own duplicate.
    report_rows: dict[Any, dict[str, Any]] = {}
    for page in range(1, report_pages + 1):
        pagination = client.reports(
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
            limit=reports_per_page,
            page=page,
            start_time=start_time,
            end_time=end_time,
            zone_id=zone_id,
            game_zone_id=None,
        )
        for row in list_at(pagination, "data"):
            if isinstance(row, dict):
                report_rows.setdefault(row.get("code"), row)
        if not pagination.get("has_more_pages"):
            break
    return list(report_rows.values())


def _kill_duration_in_bounds(
    fight: dict[str, Any],
    *,
    kill_time_min: float | None,
    kill_time_max: float | None,
) -> float | None:
    duration_ms = fight_duration_ms(fight)
    if duration_ms is None:
        return None
    duration_seconds = duration_ms / 1000
    if kill_time_min is not None and duration_seconds < kill_time_min:
        return None
    if kill_time_max is not None and duration_seconds > kill_time_max:
        return None
    return duration_seconds


def _fight_player_details(
    client: WarcraftLogsClient, *, report: dict[str, Any], fight: dict[str, Any], difficulty: int | None
) -> dict[str, Any]:
    return client.report_player_details(
        code=str(report.get("code") or ""),
        allow_unlisted=False,
        options=ReportPlayerDetailsOptions(
            difficulty=difficulty,
            encounter_id=fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None,
            fight_ids=[int(fight["id"])] if isinstance(fight.get("id"), int) else None,
            include_combatant_info=True,
            kill_type="Kills",
        ),
        ttl_override=client._finished_report_ttl,
    )


def _fight_roster(
    client: WarcraftLogsClient, *, report: dict[str, Any], fight: dict[str, Any], difficulty: int | None
) -> frozenset[str]:
    """Every player in the fight as ``name-server``."""
    details = _fight_player_details(client, report=report, fight=fight, difficulty=difficulty)
    return frozenset(
        f"{row.get('name')}-{row.get('server')}" for rows in player_details_roles(details).values() for row in rows
    )


def _matching_players_for_fight(
    client: WarcraftLogsClient,
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    difficulty: int | None,
    spec_name: str | None,
) -> list[dict[str, Any]] | None:
    if not spec_name:
        return []
    details_report = _fight_player_details(client, report=report, fight=fight, difficulty=difficulty)
    matching_players = matching_spec_players(details_report, spec_name=spec_name)
    if not matching_players:
        return None
    return matching_players


def _matching_kill_fights(
    client: WarcraftLogsClient,
    reports: list[dict[str, Any]],
    *,
    boss_id: int | None,
    boss_name: str | None,
    difficulty: int | None,
    kill_time_min: float | None,
    kill_time_max: float | None,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], int]:
    """``(report, fight)`` pairs for every kill matching the cohort filters, and the fights scanned.

    Reports still being logged are scanned too: a kill fight is final once it has ended, and the
    listing puts the most recently updated reports first, so skipping them empties the cohort.
    """
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    scanned_fight_count = 0
    for report in reports:
        fights_payload = client.report_fights(
            code=str(report.get("code") or ""),
            difficulty=difficulty,
            allow_unlisted=False,
            ttl_override=client._finished_report_ttl,
        )
        for fight in list_at(fights_payload, "fights"):
            if not isinstance(fight, dict):
                continue
            scanned_fight_count += 1
            if not fight.get("kill"):
                continue
            if not boss_matches(fight, boss_id=boss_id, boss_name=boss_name):
                continue
            if _kill_duration_in_bounds(fight, kill_time_min=kill_time_min, kill_time_max=kill_time_max) is None:
                continue
            candidates.append((report, fight))
    return candidates, scanned_fight_count


@dataclass(frozen=True, slots=True)
class ScannedKills:
    """Rows for one sampled cohort plus the counts that make the sampling legible."""

    rows: list[dict[str, Any]]
    scanned_fight_count: int
    matched_boss_kill_count: int
    duplicates_removed: int
    possible_duplicates: int


def _scan_reports_for_boss_kills(
    client: WarcraftLogsClient,
    reports: list[dict[str, Any]],
    *,
    boss_id: int | None,
    boss_name: str | None,
    difficulty: int | None,
    spec_name: str | None,
    kill_time_min: float | None,
    kill_time_max: float | None,
) -> ScannedKills:
    candidates, scanned_fight_count = _matching_kill_fights(
        client,
        reports,
        boss_id=boss_id,
        boss_name=boss_name,
        difficulty=difficulty,
        kill_time_min=kill_time_min,
        kill_time_max=kill_time_max,
    )
    # Collapse before the per-fight player-details fetch, so a double-logged pull neither
    # double-counts nor costs a second upstream request.
    pulls = deduplicate_pulls(
        candidates,
        roster=lambda report, fight: _fight_roster(client, report=report, fight=fight, difficulty=difficulty),
    )
    boss_kills: list[dict[str, Any]] = []
    for pull in pulls:
        matching_players = _matching_players_for_fight(
            client,
            report=pull.report,
            fight=pull.fight,
            difficulty=difficulty,
            spec_name=spec_name,
        )
        if spec_name and matching_players is None:
            continue
        boss_kills.append(
            boss_kill_row(
                report=pull.report,
                fight=pull.fight,
                matching_players=matching_players or [],
                duplicate_reports=pull.duplicates,
                possible_duplicate_of=pull.possible_duplicate_of,
            )
        )

    boss_kills.sort(
        key=lambda row: (
            row.get("duration_ms") if isinstance(row.get("duration_ms"), (int, float)) else float("inf"),
            str((row.get("report") or {}).get("code") or ""),
            int((row.get("fight") or {}).get("id") or 0),
        )
    )
    return ScannedKills(
        rows=boss_kills,
        scanned_fight_count=scanned_fight_count,
        matched_boss_kill_count=len(pulls),
        duplicates_removed=len(candidates) - len(pulls),
        possible_duplicates=sum(1 for row in boss_kills if row["possible_duplicate_of"] is not None),
    )


def collect_boss_kill_rows(client: WarcraftLogsClient, scope: CrossReportScope) -> dict[str, Any]:
    report_rows = _fetch_zone_report_rows(
        client,
        zone_id=scope.zone_id,
        guild_region=scope.guild_region,
        guild_realm=scope.guild_realm,
        guild_name=scope.guild_name,
        report_pages=scope.report_pages,
        reports_per_page=scope.reports_per_page,
        start_time=scope.start_time,
        end_time=scope.end_time,
    )
    live_report_count = sum(1 for row in report_rows if not report_is_finished(row))
    scanned = _scan_reports_for_boss_kills(
        client,
        report_rows,
        boss_id=scope.boss_id,
        boss_name=scope.boss_name,
        difficulty=scope.difficulty,
        spec_name=scope.spec_name,
        kill_time_min=scope.kill_time_min,
        kill_time_max=scope.kill_time_max,
    )
    sample: dict[str, Any] = {
        "source_report_count": len(report_rows),
        "finished_report_count": len(report_rows) - live_report_count,
        "live_report_count": live_report_count,
        "scanned_fight_count": scanned.scanned_fight_count,
        # Distinct pulls: a kill logged by several raiders counts once, and duplicates_removed
        # says how many raw fights were collapsed to get there.
        "matched_boss_kill_count": scanned.matched_boss_kill_count,
        "duplicates_removed": scanned.duplicates_removed,
        "possible_duplicates": scanned.possible_duplicates,
        "difficulty_counts": _value_counts(scanned.rows, "difficulty"),
        "keystone_level_counts": _value_counts(scanned.rows, "keystone_level"),
    }
    if scope.spec_name:
        # A bare spec name can match several classes (Frost Mage and Frost Death Knight).
        sample["matched_spec_classes"] = sorted(
            {str(player["type"]) for row in scanned.rows for player in row["matching_players"] if player.get("type")}
        )
    return {"rows": scanned.rows, "sample": sample}


def _value_counts(rows: list[dict[str, Any]], fight_field: str) -> list[dict[str, Any]]:
    """Kills per value of one emitted fight field, most common first; fights without the field are skipped."""
    counts = Counter(value for row in rows if (value := dict_at(row, "fight").get(fight_field)) is not None)
    return [{fight_field: value, "kill_count": count} for value, count in counts.most_common()]


def boss_kills_payload(
    *,
    kind: str,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
    transport_counts: dict[str, int],
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    returned = rows[:top]
    excluded = max(0, len(rows) - len(returned))
    truncated = len(rows) > top
    return {
        "kind": kind,
        "ranking_basis": "sampled_fastest_kills",
        "matching_rule": "sampled_zone_reports_filtered_by_optional_boss_difficulty_spec_and_kill_time",
        "query": query,
        "notes": [
            *sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None, sample),
            *sampled_cohort_notes(sample),
        ],
        "freshness": sampled_cross_report_freshness(cache_ttl_seconds, transport_counts=transport_counts),
        "cache_provenance": sampled_cache_provenance(cache_ttl_seconds, rows),
        "sample_scope": sampled_sample_scope(
            ranking_basis="sampled_fastest_kills",
            query=query,
            returned=len(returned),
            excluded=excluded,
            truncated=truncated,
        ),
        "citations": sampled_cross_report_citations(rows, root_url=root_url),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
            "returned_kill_count": len(returned),
            "excluded_kill_count": excluded,
            "truncated": truncated,
        },
        "count": len(returned),
        "kills": returned,
    }


def spec_filtered_kill_samples_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
    transport_counts: dict[str, int],
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    # `rows` arrive sorted by ascending kill duration (shared collect_boss_kill_rows path),
    # so the returned head is the fastest qualifying kills. `sample_size` is the FULL matching
    # cohort, not the returned slice, and the truncation order is surfaced so consumers do not
    # mistake a fastest-kill head for a representative random sample.
    returned = rows[:top]
    spec_name = query.get("spec_name") if isinstance(query, dict) else None
    truncated = len(rows) > top
    matching_participant_count = sum(
        len(row.get("matching_players") or [])
        for row in rows
        if isinstance(row, dict)
    )
    notes = [
        *sampled_spec_filter_notes(spec_name, sample),
        (
            "rows are sampled kills that contained at least one participant of the requested spec; "
            "this is a participant cohort, not a spec ranking leaderboard"
        ),
        *sampled_cohort_notes(sample),
    ]
    if truncated:
        notes.append(
            "returned kills are the fastest qualifying kills (ascending duration); slower kills in "
            "the cohort are excluded by --limit, so the returned subset is not a representative random sample"
        )
    return {
        "kind": "spec_filtered_kill_samples",
        "cohort": "spec_filtered_participant_kill_cohort",
        "ranking_basis": "spec_filtered_participant_kill_samples",
        "matching_rule": "sampled_zone_reports_filtered_to_kills_containing_the_requested_participant_spec",
        "query": query,
        "notes": notes,
        "freshness": sampled_cross_report_freshness(cache_ttl_seconds, transport_counts=transport_counts),
        "cache_provenance": sampled_cache_provenance(cache_ttl_seconds, rows),
        "sample_scope": sampled_sample_scope(
            ranking_basis="spec_filtered_participant_kill_samples",
            query=query,
            returned=len(returned),
            excluded=max(0, len(rows) - len(returned)),
            truncated=truncated,
        ),
        "citations": sampled_cross_report_citations(rows, root_url=root_url),
        "sample": {
            **sample,
            "spec_name": spec_name,
            "sample_size": len(rows),
            "matching_participant_count": matching_participant_count,
            "filtered_kill_count": len(rows),
            "returned_kill_count": len(returned),
            "excluded_kill_count": max(0, len(rows) - len(returned)),
            "truncated": truncated,
            "truncation_order": "fastest_kill_duration_ascending",
        },
        "count": len(returned),
        "kills": returned,
    }


def kill_time_distribution_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    bucket_seconds: int,
    transport_counts: dict[str, int],
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    durations = [
        float(duration)
        for duration in (row.get("duration_seconds") for row in rows)
        if isinstance(duration, (int, float))
    ]
    return {
        "kind": "kill_time_distribution",
        "ranking_basis": "sampled_kill_time_distribution",
        "matching_rule": "sampled_zone_reports_filtered_by_optional_boss_difficulty_spec_and_kill_time",
        "query": query,
        "notes": [
            *sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None, sample),
            *sampled_cohort_notes(sample),
        ],
        "freshness": sampled_cross_report_freshness(cache_ttl_seconds, transport_counts=transport_counts),
        "cache_provenance": sampled_cache_provenance(cache_ttl_seconds, rows),
        "sample_scope": sampled_sample_scope(
            ranking_basis="sampled_kill_time_distribution",
            query=query,
            returned=len(rows),
            excluded=0,
            truncated=False,
        ),
        "citations": sampled_cross_report_citations(rows, root_url=root_url),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
        },
        "distribution": {
            "unit": "seconds",
            "bucket_seconds": bucket_seconds,
            "statistics": numeric_summary(durations),
            "rows": duration_bucket_rows(durations, bucket_seconds=bucket_seconds),
        },
        "fastest_kills_preview": rows[: min(5, len(rows))],
    }
