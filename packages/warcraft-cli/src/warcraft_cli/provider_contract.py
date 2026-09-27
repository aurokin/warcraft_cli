from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from warcraft_core.exit_codes import EXIT_USAGE
from warcraft_core.paths import config_root
from warcraft_core.provider import ProviderError

from warcraft_cli.providers import STALE_GUIDE_REASON

DEFAULT_WRAPPER_RANKING_POLICY: dict[str, Any] = {
    "provider_families": {
        "wowhead": "entity",
        "method": "article",
        "icy-veins": "article",
        "raiderio": "profile",
        "warcraftlogs": "logs",
        "warcraft-wiki": "reference",
        "lorrgs": "logs",
        "simc": "local_tool",
    },
    # Raider.IO profile regions. `world` is a leaderboard scope, not a region a profile lives in, and
    # counting it made `world boss sha of anger` read as a profile lookup.
    "known_region_terms": ["us", "eu", "kr", "tw", "cn"],
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
        # Lorrgs and Warcraft Logs describe specs and fights, never a guide.
        "guide": {"article": 26, "entity": 10, "reference": -6, "profile": -18, "local_tool": -22, "logs": -10},
        "reference": {"reference": 32, "entity": 8, "article": -10, "profile": -18, "local_tool": -12},
        "entity": {"entity": 30, "article": -10, "reference": -6, "profile": -16, "local_tool": -18},
        "guild_profile": {"profile": 30, "article": -14, "reference": -10, "entity": -12, "local_tool": -20},
        "character_profile": {"profile": 28, "entity": 6, "article": -14, "reference": -10, "local_tool": -18},
        "log_analysis": {"logs": 40, "profile": -14, "article": -16, "reference": -12, "entity": -10, "local_tool": -12},
        "structured_profile": {"profile": 34, "article": -16, "reference": -12, "entity": -10, "local_tool": -18},
        "simc": {"local_tool": 40, "article": -18, "reference": -14, "entity": -14, "profile": -20},
    },
    "intent_provider_boosts": {
        "guild_profile": {"raiderio": 10},
        "character_profile": {"raiderio": 28},
        "log_analysis": {"warcraftlogs": 28, "lorrgs": 18},
        "structured_profile": {"raiderio": 4},
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
        # Raider.IO's character/guild rows are boosted by the profile intents only. An
        # unconditional kind boost here made every character row outrank every entity row on a bare
        # name such as `thunderfury`, which is not a profile query at all.
        "warcraftlogs": {"report": 12, "report_encounter": 16},
        "warcraft-wiki": {"article": 8},
        "lorrgs": {"report_overview": 10, "spec_ranking": 14, "comp_ranking": 10},
        "simc": {"analysis": 8, "apl": 10, "decode_build": 10, "inspect": 8, "run": 8},
    },
    # How much a row's own title answering the query is worth, on the shared 0-100 axis.
    "name_match_boosts": {"exact": 25, "title_prefix": 12},
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


def _lower_frozenset(values: Any) -> frozenset[str]:
    return frozenset(str(value).lower() for value in list(values or []))


def _int_boost_map(section: Any) -> dict[str, dict[str, int]]:
    return {
        str(key): {str(inner): int(value) for inner, value in dict(boosts).items()}
        for key, boosts in dict(section or {}).items()
    }


def _normalize_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce a merged ranking policy (defaults plus user config) to its canonical value types."""
    return {
        "provider_families": {str(key): str(value) for key, value in dict(policy.get("provider_families") or {}).items()},
        "known_region_terms": _lower_frozenset(policy.get("known_region_terms")),
        "structured_profile_second_token_blocklist": _lower_frozenset(
            policy.get("structured_profile_second_token_blocklist")
        ),
        "intent_keywords": {
            str(intent): _lower_frozenset(keywords)
            for intent, keywords in dict(policy.get("intent_keywords") or {}).items()
        },
        "intent_family_boosts": _int_boost_map(policy.get("intent_family_boosts")),
        "intent_provider_boosts": _int_boost_map(policy.get("intent_provider_boosts")),
        "intent_kind_boosts": _int_boost_map(policy.get("intent_kind_boosts")),
        "provider_kind_boosts": _int_boost_map(policy.get("provider_kind_boosts")),
        "name_match_boosts": {
            str(key): int(value) for key, value in dict(policy.get("name_match_boosts") or {}).items()
        },
    }


@lru_cache(maxsize=4)
def _load_wrapper_ranking_policy_cached(path_text: str) -> dict[str, Any]:
    merged = dict(DEFAULT_WRAPPER_RANKING_POLICY)
    config_path = Path(path_text)
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "invalid_config",
                f"Could not read the wrapper ranking override at {config_path}: {exc}",
                details={"config_path": str(config_path)},
                exit_code=EXIT_USAGE,
            ) from exc
        if isinstance(payload, Mapping):
            merged = _deep_merge(merged, payload)
    return _normalize_policy(merged)


def load_wrapper_ranking_policy() -> dict[str, Any]:
    """Default ranking policy, deep-merged with ``<config_root>/wrapper_ranking.json`` when present."""
    return _load_wrapper_ranking_policy_cached(str(_wrapper_ranking_config_path()))


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
    # `<region> <realm...> <name>`: realms can be several words (`eu tarren mill Cotti`).
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


# The provider-local score a best row has to reach before it is treated as a full-strength match.
# Every provider clears it for a real hit (a Wowhead exact name alone scores 30 before prefix, term
# and popularity credit; a Raider.IO exact structured match scores 45 on top of its base), so the
# floor only bites when a provider's whole answer is weak.
MINIMUM_PROVIDER_SCORE_SCALE = 40

# The provider family that only answers a query actually asking for a player or guild profile, and
# the intents that ask for one.
PROFILE_FAMILY = "profile"
PROFILE_INTENTS = frozenset({"character_profile", "guild_profile", "structured_profile"})
# The family that owns game entities (items, spells, quests, zones). A bare query naming one of
# them is answered by that entity first; every other family describes or lists it.
ENTITY_FAMILY = "entity"

# Punctuation that separates a title's head from its qualifier: "Thunderfury, Blessed Blade of the
# Windseeker", "Un'Goro Crater: Reclamation".
_TITLE_HEAD_SEPARATORS = re.compile(r"[,:]")


def normalized_provider_score(score: int, *, provider_max_score: int) -> int:
    """Rescale one provider-local score onto the shared 0-100 axis using that provider's own best row.

    Provider search scores are not comparable, and the gap is in the scoring code, not in one
    query's data: the Warcraft Wiki stacks title, term, intent and family credit while Wowhead's best
    row is an exact name plus prefix, term and popularity credit. A live ``un'goro crater`` fanout
    measured 116 against 89. Merging the raw numbers lets the provider with the largest scale own
    every slot in the merged list.

    The divisor never drops below ``MINIMUM_PROVIDER_SCORE_SCALE``, so a provider whose best row is
    junk (a two-term text match scoring 3) is scaled down rather than promoted to 100 for winning
    its own empty field.
    """
    if provider_max_score <= 0 or score <= 0:
        return 0
    divisor = max(provider_max_score, MINIMUM_PROVIDER_SCORE_SCALE)
    return round(100 * min(score, provider_max_score) / divisor)


def _normalized_title(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def name_match_strength(query: str, name: Any) -> str | None:
    """Whether the row's own title *is* what was asked for: the whole title, its head, or neither.

    Providers score rows on their own scales, so the merged list needs one comparable signal for
    "this row is the thing". Wowhead's item ``Thunderfury, Blessed Blade of the Windseeker`` and its
    news post ``Possible Thunderfury-Themed Cloak on the PTR`` both merely contain ``thunderfury``;
    only the item's title starts with it.
    """
    normalized_query = _normalized_title(query)
    normalized_name = _normalized_title(name)
    if not normalized_query or not normalized_name:
        return None
    if normalized_name == normalized_query:
        return "exact"
    head = _normalized_title(_TITLE_HEAD_SEPARATORS.split(normalized_name, maxsplit=1)[0])
    return "title_prefix" if head == normalized_query else None


def _provider_flagged_stale(row: Mapping[str, Any]) -> bool:
    """Whether the provider itself marked this row a superseded guide (Wowhead does, per response)."""
    ranking = row.get("ranking")
    reasons = ranking.get("match_reasons") if isinstance(ranking, Mapping) else None
    return isinstance(reasons, list) and STALE_GUIDE_REASON in reasons


def wrapper_search_ranking(
    query: str,
    row: Mapping[str, Any],
    *,
    provider_max_score: int | None = None,
    provider_top_row: bool = False,
) -> dict[str, Any]:
    """Score one candidate for the merged wrapper list: normalized provider score plus policy boosts.

    ``provider_max_score`` is the best raw score the same provider returned for this query. Pass it
    whenever candidates from several providers end up in one ranked list (``warcraft search`` and
    ``warcraft resolve``) so no provider's local scale can crowd the others out.
    ``provider_top_row`` marks the provider's own first row, the only row that can anchor a page.
    ``intent_family_fit`` is the sum of the family boosts the query's intents gave the row: below zero,
    the query asked for a different kind of source than this row's provider.
    """
    policy = load_wrapper_ranking_policy()
    provider = str(row.get("provider") or "").strip()
    family = policy["provider_families"].get(provider, "unknown")
    kind = candidate_kind(row)
    raw_score = candidate_score(row)
    if provider_max_score is None:
        score = raw_score
        reasons: list[str] = [f"provider_score:{score}"]
    else:
        score = normalized_provider_score(raw_score, provider_max_score=provider_max_score)
        reasons = [f"normalized_provider_score:{score}(raw {raw_score}/{provider_max_score})"]
    intents = query_intents(query)
    family_fit = 0
    for intent in intents:
        family_boost = policy["intent_family_boosts"].get(intent, {}).get(family, 0)
        if family_boost:
            score += family_boost
            family_fit += family_boost
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
    name_match = name_match_strength(query, row.get("name"))
    name_match_boost = policy["name_match_boosts"].get(name_match or "", 0)
    if name_match_boost:
        score += name_match_boost
        reasons.append(f"name_match:{name_match}:{name_match_boost:+d}")
    off_intent = family == PROFILE_FAMILY and not (set(intents) & PROFILE_INTENTS)
    if off_intent:
        reasons.append("off_intent:profile_row_without_a_profile_query")
    anchor = provider_top_row and not intents and family == ENTITY_FAMILY and name_match is not None
    if anchor:
        reasons.append("anchor:entity_provider_top_row_named_by_a_bare_query")
    return {
        "score": score,
        "reasons": reasons,
        "intents": intents,
        "provider_family": family,
        "kind": kind,
        "name_match": name_match,
        "off_intent": off_intent,
        "intent_family_fit": family_fit,
        "anchor": anchor,
        "stale_guide": _provider_flagged_stale(row),
        "provider_score": raw_score,
        "provider_max_score": provider_max_score,
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
            "stale_guide": ranking.get("stale_guide"),
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


def decorate_search_result(
    query: str,
    row: Mapping[str, Any],
    *,
    provider_max_score: int | None = None,
    provider_top_row: bool = False,
) -> dict[str, Any]:
    """One merged-list row: the provider's own row plus its wrapper ranking and normalized ``kind``.

    Providers name a row's type differently (``kind``, ``entity_type``, ``type_name``), so the merged
    list carries the normalized ``kind`` the ranking itself used. Without it the compact ``--brief``
    row would report a field the full row does not have.
    """
    ranking = wrapper_search_ranking(
        query, row, provider_max_score=provider_max_score, provider_top_row=provider_top_row
    )
    decorated = dict(row)
    decorated["wrapper_ranking"] = ranking
    if ranking["kind"] is not None:
        decorated.setdefault("kind", ranking["kind"])
    return decorated


def provider_max_candidate_score(rows: Sequence[Mapping[str, Any]]) -> int:
    """The best raw score one provider returned for a query, the divisor for its normalized scores."""
    return max((candidate_score(row) for row in rows), default=0)


def _wrapper_ranking(row: Mapping[str, Any]) -> Mapping[str, Any]:
    wrapper = row.get("wrapper_ranking")
    return wrapper if isinstance(wrapper, Mapping) else {}


def row_is_off_intent(row: Mapping[str, Any]) -> bool:
    """A row whose family does not answer this kind of query, ranked below every on-intent row."""
    return bool(_wrapper_ranking(row).get("off_intent"))


def search_result_sort_key(row: Mapping[str, Any]) -> tuple[int, int, int, int, str, str, str]:
    """Order between providers' candidate rows: anchor, then on-intent rows, then score.

    Two tiers do the work that per-provider score tuning could not:

    * the *anchor* tier: a bare query that names the entity provider's own top row is answered by
      that entity first, whatever local scale another provider's description of it happens to use;
    * the *off-intent* tier: a profile row cannot outrank rows from families the query actually
      asked for, which is what kept ``thunderfury`` from returning five players named Thunderfury.
    """
    wrapper = _wrapper_ranking(row)
    try:
        wrapper_score = int(wrapper.get("score") or 0) if wrapper else candidate_score(row)
    except (TypeError, ValueError):
        wrapper_score = 0
    score = candidate_score(row)
    provider = str(row.get("provider") or "")
    name = str(row.get("name") or "")
    identifier = str(row.get("id") or "")
    anchor_rank = 0 if wrapper.get("anchor") else 1
    return (anchor_rank, int(row_is_off_intent(row)), -wrapper_score, -score, provider, name, identifier)


def interleave_provider_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Merge the providers' lists without reordering any one of them.

    A provider's own order is its ranking, so each list is consumed front to back and only the
    choice *between* providers uses ``search_result_sort_key``: at every step the best of the
    providers' next rows goes next.
    """
    queues: dict[str, deque[Mapping[str, Any]]] = {}
    for row in rows:
        queues.setdefault(str(row.get("provider") or ""), deque()).append(row)
    merged: list[Mapping[str, Any]] = []
    while queues:
        provider = min(queues, key=lambda name: search_result_sort_key(queues[name][0]))
        merged.append(queues[provider].popleft())
        if not queues[provider]:
            del queues[provider]
    return merged


def _capped_rows(
    rows: Sequence[Mapping[str, Any]], *, room: int, per_provider_cap: int
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Fill ``room`` slots in merged order with at most ``per_provider_cap`` rows per provider."""
    taken: list[Mapping[str, Any]] = []
    deferred: list[Mapping[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        provider = str(row.get("provider") or "")
        if len(taken) < room and counts.get(provider, 0) < per_provider_cap:
            taken.append(row)
            counts[provider] = counts.get(provider, 0) + 1
        else:
            deferred.append(row)
    return taken, deferred


def _reserves_profile_slot(
    ordered: Sequence[Mapping[str, Any]], off_intent: Sequence[Mapping[str, Any]], *, limit: int
) -> bool:
    """Whether a bare name that is exactly a character's or guild's name keeps one profile row.

    A bare name is ambiguous. When an entity provider's top row answers it (the page is anchored)
    the name is that entity's; otherwise the first off-intent row, if its name is exactly the query,
    may be what the user meant, and the wiki's full-text search always has rows to crowd it out.
    One slot, never a majority.
    """
    if limit < 3 or not off_intent or any(_wrapper_ranking(row).get("anchor") for row in ordered):
        return False
    return _wrapper_ranking(off_intent[0]).get("name_match") == "exact"


def merged_search_page(
    rows: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The merged page of candidates, with no single provider allowed to fill it.

    ``rows`` arrive in each provider's own order, and the page never reorders two rows from one
    provider (``interleave_provider_rows``). Interleaving alone is not enough: a provider whose rows
    tie at its own best score (Raider.IO returns twenty identically scored characters for a bare
    name) normalizes every one of them to 100 and owns every slot. So:

    * on-intent rows fill the page first, each provider taking at most half of it (rounded up);
      rows over that share are deferred and fill whatever the other providers leave;
    * off-intent rows only take slots on-intent rows left empty, at most a strict minority of the
      page, except for the one slot ``_reserves_profile_slot`` keeps for an exact-name profile row;
    * the chosen rows keep their interleaved order.

    Deferred, reserved and withheld rows are counted in the policy block.
    """
    ordered = interleave_provider_rows(rows)
    position = {id(row): index for index, row in enumerate(ordered)}
    per_provider_cap = max(1, (limit + 1) // 2)
    off_intent_cap = max(1, limit // 2)
    off_intent = [row for row in ordered if row_is_off_intent(row)]
    reserved = 1 if _reserves_profile_slot(ordered, off_intent, limit=limit) else 0
    room = limit - reserved
    capped, deferred = _capped_rows(
        [row for row in ordered if not row_is_off_intent(row)], room=room, per_provider_cap=per_provider_cap
    )
    promoted = deferred[: room - len(capped)]
    on_page = [*capped, *promoted]
    off_page = off_intent[: min(off_intent_cap, limit - len(on_page))]
    page = [dict(row) for row in sorted([*on_page, *off_page], key=lambda row: position[id(row)])]
    provider_row_counts: dict[str, int] = {}
    for row in page:
        provider = str(row.get("provider") or "")
        provider_row_counts[provider] = provider_row_counts.get(provider, 0) + 1
    return page, {
        "rule": "interleave_provider_order_then_per_provider_cap",
        "per_provider_cap": per_provider_cap,
        "off_intent_provider_cap": off_intent_cap,
        "reserved_exact_profile_slot_count": reserved,
        "candidate_row_count": len(ordered),
        "deferred_row_count": len(deferred),
        "promoted_after_cap_count": len(promoted),
        "withheld_off_intent_row_count": len(off_intent) - len(off_page),
        "provider_row_counts": provider_row_counts,
    }


def decorate_resolve_payload(query: str, provider: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """One provider's resolve answer, its match ranked exactly as ``warcraft search`` ranks that
    provider's top row: normalized against the provider's own candidates, and able to anchor."""
    decorated = dict(payload)
    match = payload.get("match")
    if isinstance(match, Mapping):
        rows = [match, *(row for row in payload.get("candidates") or [] if isinstance(row, Mapping))]
        decorated_match = decorate_search_result(
            query,
            {"provider": provider, **dict(match)},
            provider_max_score=provider_max_candidate_score(rows),
            provider_top_row=True,
        )
        decorated["match"] = decorated_match
        decorated["wrapper_ranking"] = decorated_match["wrapper_ranking"]
    return decorated


def resolve_payload_sort_key(payload: Mapping[str, Any]) -> tuple[int, int, int, int, int, int, str, str, str]:
    """Order between providers' decorated resolve answers: ``search_result_sort_key`` on the match.

    ``warcraft resolve`` answers with the row ``warcraft search`` would put first, so a provider's own
    ``resolved``/``confidence`` never lifts a row over a better-ranked one; they only break an exact
    tie on the wrapper score, ahead of the incomparable raw provider score.
    """
    match = payload.get("match")
    anchor, off_intent, wrapper_score, score, provider, name, identifier = search_result_sort_key(
        match if isinstance(match, Mapping) else {}
    )
    resolved = 1 if payload.get("resolved") else 0
    confidence = confidence_rank(payload.get("confidence"))
    return (anchor, off_intent, wrapper_score, -resolved, -confidence, score, provider, name, identifier)


def resolve_answer_accepted(payload: Mapping[str, Any]) -> bool:
    """Whether the top-ranked resolve answer is the wrapper's answer.

    Its own provider must have resolved it, and the query's intents must not rank that provider's
    family down: a guide query is not answered by Lorrgs spec metadata, nor a guild query by a wiki
    article, whatever confidence the provider reported.
    """
    ranking = payload.get("wrapper_ranking")
    fit = ranking.get("intent_family_fit") if isinstance(ranking, Mapping) else 0
    return bool(payload.get("resolved")) and int(fit or 0) >= 0
