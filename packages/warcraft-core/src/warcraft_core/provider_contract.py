from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from warcraft_core.paths import config_root

DEFAULT_WRAPPER_RANKING_POLICY: dict[str, Any] = {
    "provider_families": {
        "wowhead": "entity",
        "method": "article",
        "icy-veins": "article",
        "raiderio": "profile",
        "warcraftlogs": "logs",
        "warcraft-wiki": "reference",
        "wowprogress": "profile",
        "lorrgs": "logs",
        "simc": "local_tool",
    },
    "known_region_terms": ["us", "eu", "kr", "tw", "cn", "world"],
    "structured_profile_second_token_blocklist": ["of", "the", "warcraft", "wiki", "api", "guide", "guides", "article", "articles"],
    "intent_keywords": {
        "guide": ["guide", "guides", "build", "builds", "rotation", "talent", "talents", "bis"],
        "reference": ["wiki", "article", "articles", "api", "addon", "addons", "lua", "reference", "lore", "story"],
        "entity": [
            "achievement",
            "comment",
            "comments",
            "currency",
            "faction",
            "item",
            "items",
            "mount",
            "mounts",
            "npc",
            "npcs",
            "object",
            "objects",
            "pet",
            "pets",
            "quest",
            "quests",
            "recipe",
            "recipes",
            "spell",
            "spells",
            "tooltip",
            "zone",
            "zones",
        ],
        "guild_profile": ["guild", "guilds", "roster", "progression", "leaderboard", "leaderboards"],
        "character_profile": ["character", "characters", "rio", "score", "scores", "keys", "runs", "m+", "mythic+", "mythicplus"],
        "log_analysis": [
            "warcraftlogs",
            "log",
            "logs",
            "report",
            "reports",
            "fight",
            "fights",
            "encounter",
            "encounters",
            "pull",
            "pulls",
            "timeline",
            "timelines",
        ],
        "simc": [
            "simc",
            "simulationcraft",
            "apl",
            "action",
            "actions",
            "profile",
            "profiles",
            "decode-build",
            "branch",
            "branches",
            "trace",
        ],
    },
    "intent_family_boosts": {
        "guide": {"article": 26, "entity": 10, "reference": -6, "profile": -18, "local_tool": -22},
        "reference": {"reference": 32, "entity": 8, "article": -10, "profile": -18, "local_tool": -12},
        "entity": {"entity": 30, "article": -10, "reference": -6, "profile": -16, "local_tool": -18},
        "guild_profile": {"profile": 30, "article": -14, "reference": -10, "entity": -12, "local_tool": -20},
        "character_profile": {"profile": 28, "entity": 6, "article": -14, "reference": -10, "local_tool": -18},
        "log_analysis": {"logs": 40, "profile": -14, "article": -16, "reference": -12, "entity": -10, "local_tool": -12},
        "structured_profile": {"profile": 34, "article": -16, "reference": -12, "entity": -10, "local_tool": -18},
        "simc": {"local_tool": 40, "article": -18, "reference": -14, "entity": -14, "profile": -20},
    },
    "intent_provider_boosts": {
        "guild_profile": {"wowprogress": 10, "raiderio": -4},
        "character_profile": {"raiderio": 28, "wowprogress": -14},
        "log_analysis": {"warcraftlogs": 28, "lorrgs": 18},
        "structured_profile": {"wowprogress": 4, "raiderio": 2},
    },
    "intent_kind_boosts": {
        "guide": {"guide": 18},
        "reference": {"article": 18},
        "entity": {
            "achievement": 12,
            "currency": 12,
            "faction": 10,
            "item": 16,
            "npc": 16,
            "object": 10,
            "quest": 18,
            "recipe": 12,
            "spell": 18,
            "zone": 10,
        },
        "guild_profile": {"guild": 24, "leaderboard": 18},
        "character_profile": {"character": 24, "mythic_plus_runs": 12},
        "log_analysis": {"report": 24, "report_encounter": 28, "report_overview": 20, "spec_ranking": 26, "comp_ranking": 20},
        "structured_profile": {"guild": 20, "character": 20},
        "simc": {"analysis": 16, "apl": 20, "decode_build": 18, "inspect": 12, "run": 10},
    },
    "provider_kind_boosts": {
        "wowhead": {"guide": 4, "item": 4, "npc": 4, "quest": 6, "spell": 6},
        "method": {"guide": 6},
        "icy-veins": {"guide": 6},
        "raiderio": {"character": 16, "guild": 6, "mythic_plus_runs": 8},
        "warcraftlogs": {"report": 12, "report_encounter": 16},
        "warcraft-wiki": {"article": 8},
        "wowprogress": {"character": 0, "guild": 12, "leaderboard": 8},
        "lorrgs": {"report_overview": 10, "spec_ranking": 14, "comp_ranking": 10},
        "simc": {"analysis": 8, "apl": 10, "decode_build": 10, "inspect": 8, "run": 8},
    },
}

TYPE_NAME_KIND_MAP = {
    "article": "article",
    "guide": "guide",
    "character": "character",
    "guild": "guild",
    "leaderboard": "leaderboard",
}


def _wrapper_ranking_config_path() -> Path:
    return config_root() / "wrapper_ranking.json"


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _normalize_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider_families": {str(key): str(value) for key, value in dict(policy.get("provider_families") or {}).items()},
        "known_region_terms": frozenset(str(value).lower() for value in list(policy.get("known_region_terms") or [])),
        "structured_profile_second_token_blocklist": frozenset(
            str(value).lower() for value in list(policy.get("structured_profile_second_token_blocklist") or [])
        ),
        "intent_keywords": {
            str(intent): frozenset(str(keyword).lower() for keyword in list(keywords))
            for intent, keywords in dict(policy.get("intent_keywords") or {}).items()
        },
        "intent_family_boosts": {
            str(intent): {str(key): int(value) for key, value in dict(boosts).items()}
            for intent, boosts in dict(policy.get("intent_family_boosts") or {}).items()
        },
        "intent_provider_boosts": {
            str(intent): {str(key): int(value) for key, value in dict(boosts).items()}
            for intent, boosts in dict(policy.get("intent_provider_boosts") or {}).items()
        },
        "intent_kind_boosts": {
            str(intent): {str(key): int(value) for key, value in dict(boosts).items()}
            for intent, boosts in dict(policy.get("intent_kind_boosts") or {}).items()
        },
        "provider_kind_boosts": {
            str(provider): {str(key): int(value) for key, value in dict(boosts).items()}
            for provider, boosts in dict(policy.get("provider_kind_boosts") or {}).items()
        },
    }


@lru_cache(maxsize=4)
def _load_wrapper_ranking_policy_cached(path_text: str | None) -> dict[str, Any]:
    merged = dict(DEFAULT_WRAPPER_RANKING_POLICY)
    config_path = Path(path_text) if path_text else _wrapper_ranking_config_path()
    if config_path.exists():
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            merged = _deep_merge(merged, payload)
    return _normalize_policy(merged)


def load_wrapper_ranking_policy(*, override_path: Path | None = None) -> dict[str, Any]:
    return _load_wrapper_ranking_policy_cached(str(override_path) if override_path is not None else None)


def confidence_rank(value: Any) -> int:
    normalized = str(value or "").strip().lower()
    if normalized == "high":
        return 3
    if normalized == "medium":
        return 2
    if normalized == "low":
        return 1
    return 0


def candidate_score(candidate: Mapping[str, Any] | None) -> int:
    if not isinstance(candidate, Mapping):
        return 0
    ranking = candidate.get("ranking")
    if not isinstance(ranking, Mapping):
        return 0
    try:
        return int(ranking.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def _query_tokens(query: str) -> tuple[str, set[str]]:
    normalized = query.strip().lower()
    tokens = set(re.findall(r"[a-z0-9+]+", normalized))
    if "m+" in normalized:
        tokens.add("m+")
    if "mythic+" in normalized:
        tokens.add("mythic+")
    return normalized, tokens


def query_intents(query: str) -> list[str]:
    policy = load_wrapper_ranking_policy()
    normalized, tokens = _query_tokens(query)
    intents: set[str] = set()
    for intent, keywords in policy["intent_keywords"].items():
        if tokens & keywords:
            intents.add(intent)
    ordered_tokens = [token for token in normalized.split() if token]
    if (
        len(ordered_tokens) >= 3
        and ordered_tokens[0] in policy["known_region_terms"]
        and ordered_tokens[1] not in policy["structured_profile_second_token_blocklist"]
    ):
        intents.add("structured_profile")
    if {"guild", "character"} & tokens:
        intents.add("structured_profile")
    return sorted(intents)


def candidate_kind(candidate: Mapping[str, Any] | None) -> str | None:
    if not isinstance(candidate, Mapping):
        return None
    for key in ("kind", "entity_type"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower().replace(" ", "_")
    follow_up = candidate.get("follow_up")
    if isinstance(follow_up, Mapping):
        for key in ("surface", "recommended_surface"):
            value = follow_up.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower().replace(" ", "_")
    type_name = candidate.get("type_name")
    if isinstance(type_name, str):
        normalized = type_name.strip().lower()
        return TYPE_NAME_KIND_MAP.get(normalized, normalized.replace(" ", "_")) if normalized else None
    return None


def wrapper_search_ranking(query: str, row: Mapping[str, Any]) -> dict[str, Any]:
    policy = load_wrapper_ranking_policy()
    provider = str(row.get("provider") or "").strip()
    family = policy["provider_families"].get(provider, "unknown")
    kind = candidate_kind(row)
    score = candidate_score(row)
    reasons: list[str] = [f"provider_score:{score}"]
    intents = query_intents(query)
    for intent in intents:
        family_boost = policy["intent_family_boosts"].get(intent, {}).get(family, 0)
        if family_boost:
            score += family_boost
            reasons.append(f"intent:{intent}:family:{family}:{family_boost:+d}")
        provider_boost = policy["intent_provider_boosts"].get(intent, {}).get(provider, 0)
        if provider_boost:
            score += provider_boost
            reasons.append(f"intent:{intent}:provider:{provider}:{provider_boost:+d}")
        if kind:
            kind_boost = policy["intent_kind_boosts"].get(intent, {}).get(kind, 0)
            if kind_boost:
                score += kind_boost
                reasons.append(f"intent:{intent}:kind:{kind}:{kind_boost:+d}")
    if kind:
        provider_kind_boost = policy["provider_kind_boosts"].get(provider, {}).get(kind, 0)
        if provider_kind_boost:
            score += provider_kind_boost
            reasons.append(f"provider_kind:{provider}:{kind}:{provider_kind_boost:+d}")
    return {
        "score": score,
        "reasons": reasons,
        "intents": intents,
        "provider_family": family,
        "kind": kind,
        "provider_score": candidate_score(row),
    }


def compact_wrapper_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "provider": candidate.get("provider"),
        "kind": candidate_kind(candidate),
        "name": candidate.get("name"),
        "id": candidate.get("id"),
    }
    for key in ("entity_type", "type_name", "profile_url", "url", "next_command", "confidence"):
        value = candidate.get(key)
        if value is not None:
            compact[key] = value
    follow_up = candidate.get("follow_up")
    if isinstance(follow_up, Mapping) and follow_up.get("command"):
        compact["follow_up_command"] = follow_up.get("command")
    provider_expansion = candidate.get("provider_expansion")
    if isinstance(provider_expansion, Mapping):
        compact["provider_expansion"] = {
            "mode": provider_expansion.get("mode"),
            "requested_expansion": provider_expansion.get("requested_expansion"),
            "allowed": provider_expansion.get("allowed"),
            "supported_expansions": provider_expansion.get("supported_expansions"),
            "review_status": provider_expansion.get("review_status"),
            "policy_note": provider_expansion.get("policy_note"),
        }
        exclusion_reason = provider_expansion.get("exclusion_reason")
        if exclusion_reason is not None:
            compact["provider_expansion"]["exclusion_reason"] = exclusion_reason
    ranking = candidate.get("wrapper_ranking")
    if isinstance(ranking, Mapping):
        compact["wrapper_ranking"] = {
            "score": ranking.get("score"),
            "reasons": ranking.get("reasons"),
            "intents": ranking.get("intents"),
            "provider_family": ranking.get("provider_family"),
        }
    return compact


def compact_resolve_match(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    match = payload.get("match")
    if not isinstance(match, Mapping):
        return None
    compact = compact_wrapper_candidate(match)
    if payload.get("next_command") is not None:
        compact["next_command"] = payload.get("next_command")
    if payload.get("confidence") is not None:
        compact["confidence"] = payload.get("confidence")
    return compact


def synthetic_search_candidates(query: str) -> list[dict[str, Any]]:
    normalized, tokens = _query_tokens(query)
    ordered_tokens = [token for token in normalized.split() if token]
    if "leaderboard" not in tokens:
        return []
    known_region_terms = load_wrapper_ranking_policy()["known_region_terms"]
    region = next((token for token in ordered_tokens if token in known_region_terms and token != "world"), None)
    if region is None:
        return []
    realm: str | None = None
    region_index = ordered_tokens.index(region)
    trailing = ordered_tokens[region_index + 1:]
    if trailing and trailing[0] not in {"leaderboard", "pve"}:
        realm = trailing[0]
    command = f"wowprogress leaderboard pve {region}"
    if realm:
        command += f" --realm {realm}"
    display_name = f"{region.upper()} PvE Leaderboard" if realm is None else f"{region.upper()} {realm.title()} PvE Leaderboard"
    candidate = {
        "provider": "wowprogress",
        "kind": "leaderboard",
        "id": f"wowprogress:leaderboard:pve:{region}:{realm or ''}",
        "name": display_name,
        "region": region,
        "realm": realm,
        "ranking": {"score": 60, "match_reasons": ["wrapper_direct_route", "leaderboard_query"]},
        "follow_up": {
            "provider": "wowprogress",
            "kind": "leaderboard",
            "surface": "leaderboard",
            "command": command,
        },
    }
    return [candidate]


def synthetic_resolve_payloads(query: str) -> list[tuple[str, dict[str, Any]]]:
    del query
    return []


def decorate_search_result(query: str, row: Mapping[str, Any]) -> dict[str, Any]:
    decorated = dict(row)
    decorated["wrapper_ranking"] = wrapper_search_ranking(query, row)
    return decorated


def search_result_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str, str, str]:
    wrapper = row.get("wrapper_ranking")
    if isinstance(wrapper, Mapping):
        try:
            wrapper_score = int(wrapper.get("score") or 0)
        except (TypeError, ValueError):
            wrapper_score = 0
    else:
        wrapper_score = candidate_score(row)
    score = candidate_score(row)
    provider = str(row.get("provider") or "")
    name = str(row.get("name") or "")
    identifier = str(row.get("id") or "")
    return (-wrapper_score, -score, provider, name, identifier)


def decorate_resolve_payload(query: str, provider: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    decorated = dict(payload)
    match = payload.get("match")
    if isinstance(match, Mapping):
        decorated_match = decorate_search_result(query, {"provider": provider, **dict(match)})
        decorated["match"] = decorated_match
        decorated["wrapper_ranking"] = decorated_match["wrapper_ranking"]
    return decorated


def resolve_payload_sort_key(provider: str, payload: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    resolved = 1 if payload.get("resolved") else 0
    confidence = confidence_rank(payload.get("confidence"))
    wrapper = payload.get("wrapper_ranking")
    if isinstance(wrapper, Mapping):
        try:
            wrapper_score = int(wrapper.get("score") or 0)
        except (TypeError, ValueError):
            wrapper_score = 0
    else:
        wrapper_score = 0
    match = payload.get("match")
    score = candidate_score(match if isinstance(match, Mapping) else None)
    return (-resolved, -confidence, -wrapper_score, -score, provider)
