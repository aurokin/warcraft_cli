"""Pure WowProgress provider surface.

Functions here never print and never raise ``typer.Exit``: they return an ``Envelope`` or raise
``ProviderError``. ``wowprogress_cli.main`` wraps them for the CLI and the ``warcraft`` wrapper can
call ``PROVIDER`` in-process.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope, with_legacy_keys
from warcraft_core.exit_codes import EXIT_NETWORK
from warcraft_core.provider import ProviderError, ProviderSurface

from wowprogress_cli.analytics import (
    GuildProfileFilters,
    _distribution_payload,
    _filter_guild_profiles,
    _guild_profile_distribution_payload,
    _guild_profile_sample_summary,
    _guild_profile_threshold_payload,
    _history_trajectory_rows,
    _load_pve_guild_profile_sample,
    _load_pve_leaderboard_sample,
    _sample_summary,
    _threshold_payload,
)
from wowprogress_cli.client import (
    DEFAULT_IMPERSONATE,
    WowProgressClient,
    WowProgressClientError,
    load_wowprogress_cache_settings_from_env,
)
from wowprogress_cli.identity import _guild_history_tier_row, _guild_ranks_row, _normalized_identity
from wowprogress_cli.page_parser import WOWPROGRESS_BASE_URL
from wowprogress_cli.search import resolve_payload, search_candidates

PROVIDER_NAME = "wowprogress"

CAPABILITIES: dict[str, str] = {
    "search": "ready",
    "resolve": "ready",
    "guild": "ready",
    "guild_history": "ready",
    "guild_ranks": "ready",
    "guild_snapshot": "ready",
    "history_trajectory": "ready",
    "character": "ready",
    "leaderboard": "ready",
    "sample_pve_leaderboard": "ready",
    "distribution_pve_leaderboard": "ready",
    "threshold_pve_leaderboard": "ready",
    "sample_pve_guild_profiles": "ready",
    "distribution_pve_guild_profiles": "ready",
    "threshold_pve_guild_profiles": "ready",
}

LEADERBOARD_METRICS = ("progress", "difficulty", "realm", "bosses_killed", "rank")
LEADERBOARD_THRESHOLD_METRICS = ("rank", "bosses_killed")
GUILD_PROFILE_METRICS = ("progress", "faction", "item_level_average", "world_rank", "encounter")
GUILD_PROFILE_THRESHOLD_METRICS = ("world_rank", "item_level_average")

# A Cloudflare challenge is an upstream refusal to serve, so it exits like a network failure
# instead of falling through to the generic exit 1.
_EXIT_CODE_OVERRIDES: dict[str, int] = {"blocked": EXIT_NETWORK}


def provider_error(exc: WowProgressClientError) -> ProviderError:
    """Translate a client failure into the shared error vocabulary."""
    return ProviderError(exc.code, exc.message, exit_code=_EXIT_CODE_OVERRIDES.get(exc.code))


def open_client() -> WowProgressClient:
    """Build a WowProgress client, reporting a bad cache configuration as a ProviderError."""
    try:
        return WowProgressClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc


def _provenance(source_url: str | None = None) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "site": WOWPROGRESS_BASE_URL,
        "source": "wowprogress_html",
        "transport": "browser_fingerprint_http",
    }
    if source_url:
        provenance["source_url"] = source_url
    return provenance


def _envelope(
    command: str,
    kind: str,
    payload: Mapping[str, Any],
    *,
    query: Any = None,
    provenance: dict[str, Any] | None = None,
) -> Envelope:
    """Wrap a provider payload in the shared envelope, keeping deprecated top-level copies of its keys."""
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind,
        data=dict(payload),
        query=query,
        provenance=provenance if provenance is not None else _provenance(),
    )
    legacy = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    # with_legacy_keys returns a plain dict; the envelope keys it carries are untouched.
    return cast(Envelope, with_legacy_keys(envelope, legacy))


def _page_citation(payload: Mapping[str, Any]) -> str | None:
    citations = payload.get("citations")
    page = citations.get("page") if isinstance(citations, dict) else None
    return page if isinstance(page, str) else None


def _require_metric(metric: str, allowed: tuple[str, ...]) -> str:
    if metric not in allowed:
        raise ProviderError("invalid_query", f"--metric must be one of: {', '.join(allowed)}")
    return metric


def _freshness(client: WowProgressClient) -> dict[str, Any]:
    # cache_ttl_seconds is null when caching is disabled (WOWPROGRESS_CACHE_BACKEND=none) so the
    # block never claims a TTL that is not actually applied.
    return {
        "sampled_at": datetime.now(UTC).isoformat(),
        "cache_ttl_seconds": client.guild_page_ttl_seconds if client.cache_enabled else None,
    }


def _history_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    history = payload.get("history")
    return [row for row in history if isinstance(row, dict)] if isinstance(history, list) else []


GUILD_PROFILE_FILTER_KEYS = (
    "faction",
    "difficulty",
    "world_rank_min",
    "world_rank_max",
    "item_level_min",
    "item_level_max",
    "encounter",
)


def _filtered_query(query: dict[str, Any], filtering: dict[str, Any]) -> dict[str, Any]:
    return {
        **query,
        "filters": {key: filtering[key] for key in GUILD_PROFILE_FILTER_KEYS},
    }


def doctor(**options: Any) -> Envelope:
    """Report WowProgress transport mode, cache configuration, and per-surface capability state."""
    del options
    try:
        settings, guild_ttl, character_ttl, leaderboard_ttl = load_wowprogress_cache_settings_from_env()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
    payload: dict[str, Any] = {
        "status": "ready",
        "installed": True,
        "language": "python",
        "auth": {"required": False, "deferred": True},
        "transport": {"mode": "browser_fingerprint_http", "impersonate": DEFAULT_IMPERSONATE},
        "capabilities": {"doctor": "ready", **CAPABILITIES},
        "cache": {
            "enabled": settings.enabled,
            "backend": settings.backend,
            "cache_dir": str(settings.cache_dir),
            "redis_url": settings.redis_url,
            "prefix": settings.prefix,
            "ttls": {
                "guild_page": guild_ttl,
                "character_page": character_ttl,
                "leaderboard_page": leaderboard_ttl,
            },
        },
    }
    return _envelope("doctor", "doctor", payload)


def search(query: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Probe WowProgress guild and character routes for a structured query."""
    del options
    with open_client() as client:
        try:
            payload = search_candidates(client, query, limit=limit)
        except WowProgressClientError as exc:
            raise provider_error(exc) from exc
    return _envelope("search", "search_results", payload, query=query)


def resolve(target: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Resolve a structured WowProgress query to a single next command when it is unambiguous."""
    del options
    with open_client() as client:
        try:
            payload = resolve_payload(search_candidates(client, target, limit=limit))
        except WowProgressClientError as exc:
            raise provider_error(exc) from exc
    return _envelope("resolve", "resolution", payload, query=target)


def guild(region: str, realm: str, name: str) -> Envelope:
    """Fetch one WowProgress guild page."""
    query = _normalized_identity(region, realm, name)
    with open_client() as client:
        try:
            payload = client.fetch_guild_page_variants(**query)
        except WowProgressClientError as exc:
            raise provider_error(exc) from exc
    return _envelope("guild", "guild", payload, query=query, provenance=_provenance(_page_citation(payload)))


def character(region: str, realm: str, name: str) -> Envelope:
    """Fetch one WowProgress character page."""
    query = _normalized_identity(region, realm, name)
    with open_client() as client:
        try:
            payload = client.fetch_character_page_variants(**query)
        except WowProgressClientError as exc:
            raise provider_error(exc) from exc
    return _envelope("character", "character", payload, query=query, provenance=_provenance(_page_citation(payload)))


def _guild_history_payload(query: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the raw guild-history payload plus freshness, translating client failures."""
    with open_client() as client:
        try:
            payload = client.fetch_guild_history(**query)
        except WowProgressClientError as exc:
            raise provider_error(exc) from exc
        return payload, _freshness(client)


def guild_history(region: str, realm: str, name: str) -> Envelope:
    """Fetch every archived WowProgress tier page for one guild."""
    query = _normalized_identity(region, realm, name)
    payload, _freshness_block = _guild_history_payload(query)
    rows = _history_rows(payload)
    merged = {
        **payload,
        "count": len(rows),
        "tiers": [_guild_history_tier_row(row) for row in rows],
    }
    return _envelope("guild-history", "guild_history", merged, query=query, provenance=_provenance(_page_citation(payload)))


def guild_ranks(region: str, realm: str, name: str) -> Envelope:
    """Report per-tier world, region, and realm ranks for one guild."""
    query = _normalized_identity(region, realm, name)
    payload, _freshness_block = _guild_history_payload(query)
    rows = _history_rows(payload)
    merged = {
        "guild": payload.get("guild"),
        "count": len(rows),
        "tiers": [_guild_ranks_row(row) for row in rows],
        "citations": payload.get("citations"),
    }
    return _envelope("guild-ranks", "guild_ranks", merged, query=query, provenance=_provenance(_page_citation(payload)))


def guild_snapshot(region: str, realm: str, name: str) -> Envelope:
    """Compose current progress, ranks, item level, encounters, and a per-tier rank series."""
    query = _normalized_identity(region, realm, name)
    payload, freshness = _guild_history_payload(query)
    rows = _history_rows(payload)
    merged = {
        "guild": payload.get("guild"),
        "progress": payload.get("current_progress"),
        # Current-state values from the main guild page (survive an empty history series).
        "item_level": payload.get("current_item_level"),
        "encounters": payload.get("current_encounters"),
        "rank_series": [_guild_ranks_row(row) for row in rows],
        "citations": payload.get("citations"),
        "freshness": freshness,
    }
    return _envelope("guild-snapshot", "guild_snapshot", merged, query=query, provenance=_provenance(_page_citation(payload)))


TRAJECTORY_NOTES = [
    "Each tier row is the guild's final-for-tier snapshot (source-native WowProgress tier "
    "pages), not a live or in-progress value.",
    "delta_vs_previous compares consecutive tiers, which are different raids/difficulties; "
    "treat it as descriptive movement, not a normalized skill metric. A rank is 'improved' "
    "when its world/region/realm number is lower (better).",
]


def history_trajectory(region: str, realm: str, name: str) -> Envelope:
    """Report per-tier rank and item-level trajectory with tier-over-tier deltas."""
    query = _normalized_identity(region, realm, name)
    payload, freshness = _guild_history_payload(query)
    tiers = _history_trajectory_rows(_history_rows(payload))
    merged = {
        "guild": payload.get("guild"),
        "count": len(tiers),
        "tiers": tiers,
        "notes": list(TRAJECTORY_NOTES),
        "citations": payload.get("citations"),
        "freshness": freshness,
    }
    return _envelope("history-trajectory", "history_trajectory", merged, query=query, provenance=_provenance(_page_citation(payload)))


def leaderboard(kind: str, region: str, *, realm: str | None = None, limit: int = 25) -> Envelope:
    """Fetch one WowProgress PvE leaderboard page."""
    if kind.lower() != "pve":
        raise ProviderError("invalid_query", "WowProgress phase 1 supports only the 'pve' leaderboard.")
    with open_client() as client:
        try:
            payload = client.fetch_pve_leaderboard(region=region, realm=realm, limit=limit)
        except WowProgressClientError as exc:
            raise provider_error(exc) from exc
    query = {"kind": "pve", "region": region.lower(), "realm": realm.lower() if realm else None, "limit": limit}
    return _envelope("leaderboard", "pve_leaderboard", payload, query=query, provenance=_provenance(_page_citation(payload)))


def _leaderboard_sample(region: str, realm: str | None, limit: int) -> tuple[
    list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    with open_client() as client:
        try:
            return _load_pve_leaderboard_sample(client, region=region, realm=realm, limit=limit)
        except WowProgressClientError as exc:
            raise provider_error(exc) from exc


def _guild_profile_sample(region: str, realm: str | None, limit: int, filters: GuildProfileFilters) -> tuple[
    list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    with open_client() as client:
        try:
            entries, meta, board, query = _load_pve_guild_profile_sample(client, region=region, realm=realm, limit=limit)
        except WowProgressClientError as exc:
            raise provider_error(exc) from exc
    entries, filtering = _filter_guild_profiles(entries, filters)
    return entries, meta, board, _filtered_query(query, filtering), filtering


def sample_pve_leaderboard(*, region: str, realm: str | None = None, limit: int = 25) -> Envelope:
    """Sample the top rows of a WowProgress PvE leaderboard with explicit sampling boundaries."""
    entries, meta, board, query = _leaderboard_sample(region, realm, limit)
    payload = {
        "leaderboard": board,
        "sample": _sample_summary(entries, meta=meta),
        "entries": entries,
        "freshness": {"sampled_at": meta["sampled_at"], "cache_ttl_seconds": meta["cache_ttl_seconds"]},
        "citations": {"leaderboard_page": meta["page_url"]},
    }
    return _envelope(
        "pve-leaderboard", "pve_leaderboard_sample", payload, query=query, provenance=_provenance(meta["page_url"])
    )


def distribution_pve_leaderboard(*, metric: str = "progress", region: str, realm: str | None = None, limit: int = 50) -> Envelope:
    """Summarize one metric across a sampled WowProgress PvE leaderboard slice."""
    _require_metric(metric, LEADERBOARD_METRICS)
    entries, meta, _board, query = _leaderboard_sample(region, realm, limit)
    payload = _distribution_payload(metric, entries, meta=meta, query=query)
    return _envelope(
        "pve-leaderboard", "pve_leaderboard_distribution", payload, query=query, provenance=_provenance(meta["page_url"])
    )


def threshold_pve_leaderboard(
    *, metric: str = "rank", value: float, region: str, realm: str | None = None, limit: int = 50, nearest: int = 10
) -> Envelope:
    """Estimate where a target metric value sits inside a sampled PvE leaderboard slice."""
    _require_metric(metric, LEADERBOARD_THRESHOLD_METRICS)
    entries, meta, _board, query = _leaderboard_sample(region, realm, limit)
    payload = _threshold_payload(metric, value, entries, meta=meta, query=query, nearest_limit=nearest)
    return _envelope(
        "pve-leaderboard", "pve_leaderboard_threshold", payload, query=query, provenance=_provenance(meta["page_url"])
    )


def sample_pve_guild_profiles(
    *, region: str, realm: str | None = None, limit: int = 10, filters: GuildProfileFilters | None = None
) -> Envelope:
    """Enrich the top leaderboard rows with their WowProgress guild pages."""
    entries, meta, board, query, filtering = _guild_profile_sample(region, realm, limit, filters or GuildProfileFilters())
    payload = {
        "leaderboard": board,
        "sample": _guild_profile_sample_summary(entries, meta=meta, filtering=filtering),
        "guild_profiles": entries,
        "freshness": {"sampled_at": meta["sampled_at"], "cache_ttl_seconds": meta["cache_ttl_seconds"]},
        "citations": {"leaderboard_page": meta["page_url"]},
    }
    return _envelope(
        "pve-guild-profiles", "pve_guild_profiles_sample", payload, query=query, provenance=_provenance(meta["page_url"])
    )


def distribution_pve_guild_profiles(
    *, metric: str = "progress", region: str, realm: str | None = None, limit: int = 10,
    filters: GuildProfileFilters | None = None,
) -> Envelope:
    """Summarize one metric across sampled WowProgress guild profiles."""
    _require_metric(metric, GUILD_PROFILE_METRICS)
    entries, meta, _board, query, filtering = _guild_profile_sample(region, realm, limit, filters or GuildProfileFilters())
    payload = _guild_profile_distribution_payload(metric, entries, meta=meta, query=query, filtering=filtering)
    return _envelope(
        "pve-guild-profiles", "pve_guild_profiles_distribution", payload, query=query, provenance=_provenance(meta["page_url"])
    )


def threshold_pve_guild_profiles(
    *, metric: str = "world_rank", value: float, region: str, realm: str | None = None, limit: int = 10,
    nearest: int = 5, filters: GuildProfileFilters | None = None,
) -> Envelope:
    """Estimate where a target metric value sits inside a sampled guild-profile slice."""
    _require_metric(metric, GUILD_PROFILE_THRESHOLD_METRICS)
    entries, meta, _board, query, filtering = _guild_profile_sample(region, realm, limit, filters or GuildProfileFilters())
    payload = _guild_profile_threshold_payload(
        metric, value, entries, meta=meta, query=query, nearest_limit=nearest, filtering=filtering
    )
    return _envelope(
        "pve-guild-profiles", "pve_guild_profiles_threshold", payload, query=query, provenance=_provenance(meta["page_url"])
    )


class WowProgressProvider:
    """In-process WowProgress surface for the ``warcraft`` wrapper."""

    name = PROVIDER_NAME

    search = staticmethod(search)
    resolve = staticmethod(resolve)
    doctor = staticmethod(doctor)


PROVIDER: ProviderSurface = WowProgressProvider()
