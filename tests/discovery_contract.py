"""Violation checkers for the shared search/resolve data shape in ``warcraft_core.discovery``.

Each checker returns a list of human-readable problems; an empty list means the data conforms.
"""

from __future__ import annotations

from typing import Any

CONFIDENCES = frozenset({"high", "medium", "low", "none"})
STUB_FLAGS = ("coming_soon", "not_supported")


def row_violations(row: Any, *, provider: str) -> list[str]:
    if not isinstance(row, dict):
        return [f"row is {type(row).__name__}, not an object"]
    problems: list[str] = []
    if row.get("provider") != provider:
        problems.append(f"provider is {row.get('provider')!r}, not {provider!r}")
    for key in ("kind", "name"):
        if not isinstance(row.get(key), str) or not row[key]:
            problems.append(f"{key} must be a non-empty string")
    if isinstance(row.get("id"), bool) or not isinstance(row.get("id"), str | int):
        problems.append("id must be a string or an integer")
    if "url" not in row or not isinstance(row["url"], str | None):
        problems.append("url must be present, a string or null")
    ranking = row.get("ranking")
    if not isinstance(ranking, dict) or not isinstance(ranking.get("score"), int) or isinstance(ranking.get("score"), bool):
        problems.append("ranking.score must be an integer")
    elif not isinstance(ranking.get("match_reasons"), list) or not all(isinstance(reason, str) for reason in ranking["match_reasons"]):
        problems.append("ranking.match_reasons must be a list of strings")
    follow_up = row.get("follow_up")
    if not isinstance(follow_up, dict) or "command" not in follow_up or not isinstance(follow_up["command"], str | None):
        problems.append("follow_up.command must be present, a string or null")
    elif not isinstance(follow_up.get("surface"), str) or not follow_up["surface"]:
        problems.append("follow_up.surface must be a non-empty string")
    return [f"{row.get('id')!r}: {problem}" for problem in problems]


def _page_violations(data: dict[str, Any], list_key: str, *, provider: str) -> list[str]:
    """``search_query``, the list, and the count/total_matches/truncated trio both surfaces share."""
    problems: list[str] = []
    if "search_query" not in data or not isinstance(data["search_query"], str | None):
        problems.append("search_query must be present, a string or null")
    rows = data.get(list_key)
    if not isinstance(rows, list):
        return [*problems, f"{list_key} must be a list"]
    if data.get("count") != len(rows):
        problems.append(f"count {data.get('count')!r} is not len({list_key}) {len(rows)}")
    total, truncated = data.get("total_matches", "missing"), data.get("truncated")
    if total is None:
        if not any(data.get(flag) is True for flag in STUB_FLAGS):
            problems.append("total_matches is null outside a coming_soon/not_supported stub")
        if truncated is not False:
            problems.append("truncated must be false when total_matches is null")
    elif not isinstance(total, int) or isinstance(total, bool) or total < len(rows):
        problems.append(f"total_matches {total!r} must be an integer of at least {len(rows)}")
    elif truncated is not (total > len(rows)):
        problems.append(f"truncated {truncated!r} disagrees with total_matches {total} and {len(rows)} {list_key}")
    for row in rows:
        problems.extend(row_violations(row, provider=provider))
    return problems


def search_data_violations(data: Any, *, provider: str) -> list[str]:
    if not isinstance(data, dict):
        return ["data is not an object"]
    return _page_violations(data, "results", provider=provider)


def resolve_data_violations(data: Any, *, provider: str) -> list[str]:
    if not isinstance(data, dict):
        return ["data is not an object"]
    problems = _page_violations(data, "candidates", provider=provider)
    confidence, match, next_command = data.get("confidence"), data.get("match"), data.get("next_command")
    if confidence not in CONFIDENCES:
        problems.append(f"confidence {confidence!r} is not one of {sorted(CONFIDENCES)}")
    if not isinstance(data.get("resolved"), bool):
        problems.append("resolved must be a bool")
    elif not data["resolved"] == (confidence == "high") == (next_command is not None):
        problems.append(f"resolved {data['resolved']}, confidence {confidence!r} and next_command {next_command!r} disagree")
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    if candidates and match != candidates[0]:
        problems.append("match is not the top candidate")
    if not candidates and data.get("total_matches") in (0, None) and match is not None:
        problems.append("match is set with no candidates")
    if match is not None:
        problems.extend(f"match: {problem}" for problem in row_violations(match, provider=provider))
    if next_command is not None and (not isinstance(match, dict) or next_command != (match.get("follow_up") or {}).get("command")):
        problems.append("next_command is not match.follow_up.command")
    if "fallback_search_command" not in data or not isinstance(data["fallback_search_command"], str | None):
        problems.append("fallback_search_command must be present, a string or null")
    elif data.get("resolved") is True and data["fallback_search_command"] is not None:
        problems.append("a resolved answer has no fallback_search_command")
    return problems
