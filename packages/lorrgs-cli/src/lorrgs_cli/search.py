from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from lorrgs_cli.client import LorrgsClient

REPORT_CODE_PATTERN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{8,32}$")
WORD_PATTERN = re.compile(r"[a-z0-9]+")
STOP_TERMS = frozenset(
    {
        "lorrgs",
        "lorgs",
        "io",
        "www",
        "warcraftlogs",
        "warcraft",
        "logs",
        "log",
        "report",
        "reports",
        "wow",
        "world",
        "of",
        "the",
        "and",
        "mythic",
        "heroic",
        "normal",
        "lfr",
        "damage",
        "done",
        "type",
        "fight",
        "cooldown",
        "cooldowns",
        "timeline",
        "timelines",
        "timing",
        "timings",
        "parse",
        "parses",
        "top",
        "ranking",
        "rankings",
    }
)
COMP_TERMS = frozenset({"comp", "composition", "compositions", "setup", "setups", "raidcomp", "raid"})


@dataclass(frozen=True, slots=True)
class ReportReference:
    code: str
    fight_id: int | None = None
    report_type: str | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class LorrgsRouteReference:
    kind: str
    spec_slug: str | None = None
    boss_slug: str | None = None
    report: ReportReference | None = None
    source_url: str | None = None


def parse_report_reference(reference: str) -> ReportReference | None:
    text = reference.strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.lower()
        if "warcraftlogs.com" not in host and "lorrgs.io" not in host:
            return None
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        code: str | None = None
        if "reports" in parts:
            index = parts.index("reports")
            code = parts[index + 1] if index + 1 < len(parts) else None
        elif any(part in {"user_report", "user_reports"} for part in parts):
            index = next(index for index, part in enumerate(parts) if part in {"user_report", "user_reports"})
            code = parts[index + 1] if index + 1 < len(parts) else None
        if not code or not REPORT_CODE_PATTERN.fullmatch(code):
            return None
        fight_id = _fight_id_from_url(parsed.query) or _fight_id_from_url(parsed.fragment)
        report_type = _query_value_from_url(parsed.query, "type") or _query_value_from_url(parsed.fragment, "type")
        return ReportReference(code=code, fight_id=fight_id, report_type=report_type, source_url=text)
    if " " in text or not REPORT_CODE_PATTERN.fullmatch(text):
        return None
    return ReportReference(code=text)


def parse_lorrgs_route(reference: str) -> LorrgsRouteReference | None:
    text = reference.strip()
    parsed = urlparse(text)
    if not (parsed.scheme and parsed.netloc and "lorrgs.io" in parsed.netloc.lower()):
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) >= 3 and parts[0] == "spec_ranking":
        return LorrgsRouteReference(kind="spec_ranking", spec_slug=parts[1], boss_slug=parts[2], source_url=text)
    if len(parts) >= 2 and parts[0] == "comp_ranking":
        return LorrgsRouteReference(kind="comp_ranking", boss_slug=parts[1], source_url=text)
    report = parse_report_reference(text)
    if report is not None:
        return LorrgsRouteReference(kind="report_overview", report=report, source_url=text)
    return None


def search_candidates(client: LorrgsClient, query: str, *, limit: int) -> dict[str, Any]:
    normalized_query = _normalize_query(query)
    explicit = _explicit_candidates(query)
    if explicit:
        return _search_payload(query, normalized_query, explicit[:limit], total=len(explicit), limit=limit)

    specs_payload = client.specs()["payload"]
    bosses_payload = client.bosses()["payload"]
    specs = specs_payload.get("specs") if isinstance(specs_payload, dict) else []
    bosses = bosses_payload.get("bosses") if isinstance(bosses_payload, dict) else []
    spec_matches = _rank_rows(specs if isinstance(specs, list) else [], query, row_kind="spec")
    boss_matches = _rank_rows(bosses if isinstance(bosses, list) else [], query, row_kind="boss")

    candidates: list[dict[str, Any]] = []
    if spec_matches and boss_matches:
        candidates.append(_spec_ranking_candidate(spec_matches[0], boss_matches[0], source="free_text"))
    if boss_matches:
        boss_candidate = _comp_ranking_candidate(boss_matches[0], source="free_text")
        if _query_terms(query) & COMP_TERMS:
            candidates.insert(0, boss_candidate)
        else:
            candidates.append(boss_candidate)
    if spec_matches:
        candidates.append(_spec_candidate(spec_matches[0]))
    if boss_matches:
        candidates.append(_boss_candidate(boss_matches[0]))
    candidates = _dedupe_candidates(candidates)
    candidates.sort(key=lambda row: (-_score(row), str(row.get("kind") or ""), str(row.get("name") or "")))
    return _search_payload(query, normalized_query, candidates[:limit], total=len(candidates), limit=limit)


def resolve_payload(search_payload: dict[str, Any]) -> dict[str, Any]:
    results = [row for row in search_payload.get("results") or [] if isinstance(row, dict)]
    best = results[0] if results else None
    second = results[1] if len(results) > 1 else None
    resolved = _resolved(best, second)
    confidence = _confidence(best, resolved=resolved)
    return {
        "provider": "lorrgs",
        "query": search_payload.get("query"),
        "search_query": search_payload.get("search_query"),
        "resolved": resolved,
        "confidence": confidence,
        "match": best if resolved else None,
        "next_command": _follow_up_command(best) if resolved else None,
        "results": results,
        "supported_inputs": _supported_inputs(),
        "suggested_commands": _suggested_commands(),
    }


def _fight_id_from_url(value: str) -> int | None:
    raw_value = _query_value_from_url(value, "fight")
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _query_value_from_url(value: str, key: str) -> str | None:
    parsed = parse_qs(value)
    values = parsed.get(key) or []
    if not values:
        return None
    raw_value = values[0]
    return raw_value if isinstance(raw_value, str) and raw_value else None


def _normalize_query(query: str) -> str:
    return " ".join(sorted(_query_terms(query)))


def _words(value: Any) -> list[str]:
    return WORD_PATTERN.findall(str(value or "").lower())


def _query_terms(query: str) -> set[str]:
    return {term for term in _words(query) if term not in STOP_TERMS}


def _explicit_candidates(query: str) -> list[dict[str, Any]]:
    route = parse_lorrgs_route(query)
    if route is not None:
        if route.kind == "spec_ranking" and route.spec_slug and route.boss_slug:
            return [_explicit_spec_ranking_candidate(route.spec_slug, route.boss_slug, route.source_url)]
        if route.kind == "comp_ranking" and route.boss_slug:
            return [_explicit_comp_ranking_candidate(route.boss_slug, route.source_url)]
        if route.report is not None:
            return _report_candidates(route.report)
    report = parse_report_reference(query)
    if report is not None:
        return _report_candidates(report)
    return []


def _report_candidates(ref: ReportReference) -> list[dict[str, Any]]:
    quoted = shlex.quote(ref.code)
    overview = {
        "provider": "lorrgs",
        "kind": "report_overview",
        "id": f"report:{ref.code}",
        "name": f"Lorrgs report overview {ref.code}",
        "report_id": ref.code,
        "fight_id": ref.fight_id,
        "report_type": ref.report_type,
        "source_url": ref.source_url,
        "ranking": {"score": 98, "match_reasons": ["explicit_report_reference", "overview_available"]},
        "follow_up": {
            "provider": "lorrgs",
            "kind": "report_overview",
            "surface": "report-overview",
            "command": f"lorrgs report-overview {quoted}",
        },
    }
    if ref.fight_id is None:
        return [overview]
    fight = {
        "provider": "lorrgs",
        "kind": "user_report_fights",
        "id": f"report:{ref.code}:fight:{ref.fight_id}",
        "name": f"Lorrgs cached fight {ref.fight_id} for report {ref.code}",
        "report_id": ref.code,
        "fight_id": ref.fight_id,
        "report_type": ref.report_type,
        "source_url": ref.source_url,
        "ranking": {"score": 90, "match_reasons": ["explicit_report_reference", "fight_scope_present", "cached_fight_optional"]},
        "follow_up": {
            "provider": "lorrgs",
            "kind": "user_report_fights",
            "surface": "user-report-fights",
            "command": _report_fights_command(ref, quoted),
        },
        "caveat": "Requires the selected fight to already be loaded/cached by Lorrgs.",
    }
    return [overview, fight]


def _report_fights_command(ref: ReportReference, quoted_code: str) -> str:
    command = f"lorrgs user-report-fights {quoted_code} --fight {ref.fight_id}"
    if ref.report_type:
        command += f" --type {shlex.quote(ref.report_type)}"
    return command


def _explicit_spec_ranking_candidate(spec_slug: str, boss_slug: str, source_url: str | None) -> dict[str, Any]:
    return {
        "provider": "lorrgs",
        "kind": "spec_ranking",
        "id": f"spec-ranking:{spec_slug}:{boss_slug}",
        "name": f"Lorrgs {spec_slug} on {boss_slug}",
        "spec_slug": spec_slug,
        "boss_slug": boss_slug,
        "source_url": source_url,
        "ranking": {"score": 99, "match_reasons": ["explicit_lorrgs_spec_ranking_url"]},
        "follow_up": _ranking_follow_up(spec_slug, boss_slug),
    }


def _explicit_comp_ranking_candidate(boss_slug: str, source_url: str | None) -> dict[str, Any]:
    return {
        "provider": "lorrgs",
        "kind": "comp_ranking",
        "id": f"comp-ranking:{boss_slug}",
        "name": f"Lorrgs composition ranking for {boss_slug}",
        "boss_slug": boss_slug,
        "source_url": source_url,
        "ranking": {"score": 96, "match_reasons": ["explicit_lorrgs_comp_ranking_url"]},
        "follow_up": _comp_follow_up(boss_slug),
    }


def _rank_rows(rows: list[Any], query: str, *, row_kind: str) -> list[dict[str, Any]]:
    query_terms = _query_terms(query)
    ranked: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        score, reasons = _row_score(row, query_terms, row_kind=row_kind)
        if score <= 0:
            continue
        ranked.append({"row": row, "score": score, "reasons": reasons})
    ranked.sort(key=lambda item: (-int(item["score"]), _row_name(item["row"])))
    return ranked


def _row_score(row: dict[str, Any], query_terms: set[str], *, row_kind: str) -> tuple[int, list[str]]:
    text = " ".join(str(row.get(key) or "") for key in ("full_name_slug", "full_name", "name", "name_slug"))
    class_info = row.get("class")
    if isinstance(class_info, dict):
        text += " " + " ".join(str(class_info.get(key) or "") for key in ("name", "name_slug"))
    row_terms = set(_words(text))
    matched = query_terms & row_terms
    if not matched:
        return 0, []
    score = 12 * len(matched)
    reasons = [f"term:{term}" for term in sorted(matched)]
    slug = str(row.get("full_name_slug") or row.get("name_slug") or "").strip().lower()
    full_name = str(row.get("full_name") or row.get("name") or "").strip().lower()
    slug_terms = set(_words(slug))
    full_terms = set(_words(full_name))
    if slug and "-".join(sorted(matched)) == slug:
        score += 30
        reasons.append("exact_slug")
    if full_terms and full_terms <= query_terms:
        score += 28
        reasons.append("full_name_terms")
    if row_kind == "boss" and row.get("name") and str(row["name"]).lower() in query_terms:
        score += 28
        reasons.append("boss_short_name")
    if row_kind == "spec" and slug_terms and slug_terms <= query_terms:
        score += 24
        reasons.append("spec_slug_terms")
    return score, reasons


def _row_name(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("full_name") or row.get("name") or row.get("full_name_slug") or "")


def _row_slug(row: dict[str, Any]) -> str:
    return str(row.get("full_name_slug") or row.get("name_slug") or "").strip()


def _spec_ranking_candidate(spec_match: dict[str, Any], boss_match: dict[str, Any], *, source: str) -> dict[str, Any]:
    spec = spec_match["row"]
    boss = boss_match["row"]
    spec_slug = _row_slug(spec)
    boss_slug = _row_slug(boss)
    score = min(99, 24 + int(spec_match["score"]) + int(boss_match["score"]))
    return {
        "provider": "lorrgs",
        "kind": "spec_ranking",
        "id": f"spec-ranking:{spec_slug}:{boss_slug}",
        "name": f"{_row_name(spec)} on {_row_name(boss)}",
        "spec_slug": spec_slug,
        "boss_slug": boss_slug,
        "ranking": {
            "score": score,
            "match_reasons": [
                source,
                *[f"spec_{reason}" for reason in spec_match["reasons"]],
                *[f"boss_{reason}" for reason in boss_match["reasons"]],
            ],
        },
        "follow_up": _ranking_follow_up(spec_slug, boss_slug),
    }


def _comp_ranking_candidate(boss_match: dict[str, Any], *, source: str) -> dict[str, Any]:
    boss = boss_match["row"]
    boss_slug = _row_slug(boss)
    score = min(94, 34 + int(boss_match["score"]))
    return {
        "provider": "lorrgs",
        "kind": "comp_ranking",
        "id": f"comp-ranking:{boss_slug}",
        "name": f"Composition ranking for {_row_name(boss)}",
        "boss_slug": boss_slug,
        "ranking": {"score": score, "match_reasons": [source, *[f"boss_{reason}" for reason in boss_match["reasons"]]]},
        "follow_up": _comp_follow_up(boss_slug),
    }


def _spec_candidate(spec_match: dict[str, Any]) -> dict[str, Any]:
    spec = spec_match["row"]
    spec_slug = _row_slug(spec)
    return {
        "provider": "lorrgs",
        "kind": "spec",
        "id": f"spec:{spec_slug}",
        "name": _row_name(spec),
        "spec_slug": spec_slug,
        "ranking": {"score": int(spec_match["score"]), "match_reasons": spec_match["reasons"]},
        "follow_up": {
            "provider": "lorrgs",
            "kind": "spec",
            "surface": "spec",
            "command": f"lorrgs spec {shlex.quote(spec_slug)}",
        },
    }


def _boss_candidate(boss_match: dict[str, Any]) -> dict[str, Any]:
    boss = boss_match["row"]
    boss_slug = _row_slug(boss)
    return {
        "provider": "lorrgs",
        "kind": "boss",
        "id": f"boss:{boss_slug}",
        "name": _row_name(boss),
        "boss_slug": boss_slug,
        "ranking": {"score": int(boss_match["score"]), "match_reasons": boss_match["reasons"]},
        "follow_up": {
            "provider": "lorrgs",
            "kind": "boss",
            "surface": "boss",
            "command": f"lorrgs boss {shlex.quote(boss_slug)}",
        },
    }


def _ranking_follow_up(spec_slug: str, boss_slug: str) -> dict[str, str]:
    return {
        "provider": "lorrgs",
        "kind": "spec_ranking",
        "surface": "spec-ranking",
        "command": f"lorrgs spec-ranking {shlex.quote(spec_slug)} {shlex.quote(boss_slug)}",
    }


def _comp_follow_up(boss_slug: str) -> dict[str, str]:
    return {
        "provider": "lorrgs",
        "kind": "comp_ranking",
        "surface": "comp-ranking",
        "command": f"lorrgs comp-ranking {shlex.quote(boss_slug)}",
    }


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.get("id") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _search_payload(query: str, normalized_query: str, results: list[dict[str, Any]], *, total: int, limit: int) -> dict[str, Any]:
    return {
        "provider": "lorrgs",
        "query": query,
        "search_query": normalized_query,
        "count": total,
        "results": results,
        "truncated": total > limit,
        "supported_inputs": _supported_inputs(),
        "suggested_commands": _suggested_commands(),
    }


def _supported_inputs() -> list[str]:
    return [
        "Lorrgs spec ranking URL: https://lorrgs.io/spec_ranking/<spec-slug>/<boss-slug>",
        "Lorrgs comp ranking URL: https://lorrgs.io/comp_ranking/<boss-slug>",
        "Warcraft Logs report URL: https://www.warcraftlogs.com/reports/<code>?fight=<id>",
        "Free text containing a spec and boss, e.g. frost mage chimaerus",
    ]


def _suggested_commands() -> list[str]:
    return [
        "lorrgs specs",
        "lorrgs bosses",
        "lorrgs spec-ranking mage-frost chimaerus-the-undreamt-god",
        "lorrgs report-overview bG3xDYPqKjLm8XaR",
    ]


def _score(candidate: dict[str, Any] | None) -> int:
    ranking = candidate.get("ranking") if isinstance(candidate, dict) else None
    if not isinstance(ranking, dict):
        return 0
    try:
        return int(ranking.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def _follow_up_command(candidate: dict[str, Any] | None) -> str | None:
    follow_up = candidate.get("follow_up") if isinstance(candidate, dict) else None
    if not isinstance(follow_up, dict):
        return None
    command = follow_up.get("command")
    return command if isinstance(command, str) and command else None


def _resolved(best: dict[str, Any] | None, second: dict[str, Any] | None) -> bool:
    best_score = _score(best)
    if best_score >= 96:
        return True
    if best_score < 75:
        return False
    second_score = _score(second)
    return best_score - second_score >= 18


def _confidence(best: dict[str, Any] | None, *, resolved: bool) -> str:
    if not resolved:
        return "none"
    score = _score(best)
    if score >= 90:
        return "high"
    if score >= 75:
        return "medium"
    return "low"
