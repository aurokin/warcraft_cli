from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product
from typing import Any
from urllib.parse import ParseResult, parse_qs, quote, urlparse

from warcraft_core.discovery import ResolveConfidence, discovery_row, resolve_data, search_data, single_word_named
from warcraft_core.identity import is_warcraftlogs_report_code, unique_spec_class

from lorrgs_cli.client import DIFFICULTIES as RANKED_DIFFICULTIES
from lorrgs_cli.client import SITE_HOST, LorrgsClient

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
        "on",
        "for",
        "in",
        "vs",
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
# Difficulty words stay out of row matching (they are in STOP_TERMS) but are carried into ranking
# follow-ups, because spec-ranking defaults to mythic and comp-ranking has no difficulty at all.
DIFFICULTIES = frozenset({"mythic", "heroic", "normal", "lfr"})
# Lorrgs path segments that immediately precede a report code ("reports" is the Warcraft Logs form).
USER_REPORT_PATH_SEGMENTS = frozenset({"user_report", "user_reports"})
# How precisely a query named one roster row. NAMED means the query spelled the row's slug or full
# name out; SHORT_NAME means it only used the short name, which Lorrgs shares between rows ("Frost"
# is two specs, "Salhadaar" two encounters); PARTIAL means some words overlapped and nothing more.
NAMED, SHORT_NAME, PARTIAL = 2, 1, 0
MATCH_LEVEL_NAMES = {NAMED: "named", SHORT_NAME: "short_name", PARTIAL: "partial"}
COMP_RANKING_NAME_PREFIX = "Composition ranking for "
# Ties are broken by surface usefulness: a ranking beats the bare entity it was built from.
KIND_ORDER = {"spec_ranking": 0, "comp_ranking": 1, "spec": 2, "boss": 3}
# Rows built from a parsed report reference: nothing checked that Lorrgs can serve the report.
REPORT_KINDS = frozenset({"report_overview", "user_report_fights"})


@dataclass(frozen=True, slots=True)
class RowMatch:
    """One Lorrgs spec or boss row that the query touched, with how well and which words it used."""

    row: dict[str, Any]
    level: int
    terms: frozenset[str]

    @property
    def strength(self) -> tuple[int, int]:
        return self.level, len(self.terms)


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
    difficulty: str | None = None
    report: ReportReference | None = None
    source_url: str | None = None


def parse_report_reference(reference: str) -> ReportReference | None:
    """Parse a Warcraft Logs/Lorrgs report URL or a bare report code into a ``ReportReference``."""
    text = reference.strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        return _report_reference_from_url(text, parsed)
    if " " in text or not is_warcraftlogs_report_code(text):
        return None
    return ReportReference(code=text)


def _report_reference_from_url(text: str, parsed: ParseResult) -> ReportReference | None:
    host = parsed.netloc.lower()
    if "warcraftlogs.com" not in host and "lorrgs.io" not in host:
        return None
    code = _report_code_from_path([part for part in parsed.path.strip("/").split("/") if part])
    if code is None:
        return None
    fight_id = _fight_id_from_url(parsed.query) or _fight_id_from_url(parsed.fragment)
    report_type = _query_value_from_url(parsed.query, "type") or _query_value_from_url(parsed.fragment, "type")
    return ReportReference(code=code, fight_id=fight_id, report_type=report_type, source_url=text)


def _report_code_from_path(parts: list[str]) -> str | None:
    index = _report_segment_index(parts)
    if index is None:
        return None
    code = parts[index + 1] if index + 1 < len(parts) else None
    if not code or not is_warcraftlogs_report_code(code, from_url=True):
        return None
    return code


def _report_segment_index(parts: list[str]) -> int | None:
    """Index of the path segment that precedes the report code; "reports" wins over the Lorrgs form."""
    if "reports" in parts:
        return parts.index("reports")
    for index, part in enumerate(parts):
        if part in USER_REPORT_PATH_SEGMENTS:
            return index
    return None


def parse_lorrgs_route(reference: str) -> LorrgsRouteReference | None:
    text = reference.strip()
    parsed = urlparse(text)
    if not (parsed.scheme and parsed.netloc and "lorrgs.io" in parsed.netloc.lower()):
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) >= 3 and parts[0] == "spec_ranking":
        return LorrgsRouteReference(
            kind="spec_ranking",
            spec_slug=parts[1],
            boss_slug=parts[2],
            difficulty=_query_value_from_url(parsed.query, "difficulty"),
            source_url=text,
        )
    if len(parts) >= 2 and parts[0] == "comp_ranking":
        return LorrgsRouteReference(kind="comp_ranking", boss_slug=parts[1], source_url=text)
    report = parse_report_reference(text)
    if report is not None:
        return LorrgsRouteReference(kind="report_overview", report=report, source_url=text)
    return None


def search_candidates(client: LorrgsClient, query: str, *, limit: int) -> dict[str, Any]:
    """Rank every Lorrgs surface the query can reach and keep the best ``limit`` of them."""
    return search_data(
        search_query=_normalize_query(query),
        ranked=_ranked_candidates(client, query),
        limit=limit,
        query=query,
        supported_inputs=_supported_inputs(),
        suggested_commands=_suggested_commands(),
    )


def resolve_payload(client: LorrgsClient, query: str, *, limit: int) -> dict[str, Any]:
    """Promote the top candidate to a next command, judging ambiguity over *every* candidate.

    The tie check runs on the full ranked list rather than the ``limit`` slice the caller sees: with
    ``--limit 1`` a tie between Frost Mage and Frost Death Knight would otherwise look like a single
    unrivalled answer, which is exactly the silent wrong resolution the limit must not create.
    """
    candidates = _ranked_candidates(client, query)
    return resolve_data(
        search_query=_normalize_query(query),
        ranked=candidates,
        limit=limit,
        confidence=_confidence(candidates),
        fallback_search_command=shlex.join(["lorrgs", "search", query]),
        single_word_identity=_names_single_word,
        query=query,
        supported_inputs=_supported_inputs(),
        suggested_commands=_suggested_commands(),
    )


def _ranked_candidates(client: LorrgsClient, query: str) -> list[dict[str, Any]]:
    explicit = _explicit_candidates(query)
    if explicit:
        return explicit

    specs_payload = client.specs()["payload"]
    bosses_payload = client.bosses()["payload"]
    specs = specs_payload.get("specs") if isinstance(specs_payload, dict) else []
    bosses = bosses_payload.get("bosses") if isinstance(bosses_payload, dict) else []
    query_terms = _query_terms(query)
    spec_matches = _match_rows(specs if isinstance(specs, list) else [], query_terms)
    boss_matches = _match_rows(bosses if isinstance(bosses, list) else [], query_terms)
    # Every query word that is not filler (STOP_TERMS). A candidate that leaves one out answered a
    # narrower question than the caller asked: "frost chimaerus" -> a boss drops "frost", and "frost
    # mage guide" -> the Frost Mage spec drops "guide", a question Lorrgs has no answer for.
    known_terms = frozenset(query_terms)
    difficulty = next((word for word in _words(query) if word in DIFFICULTIES), None)
    # Keep every row tied for the strongest match rather than the first one: "frost <boss>" fits Frost
    # Mage and Frost Death Knight equally, so both have to reach the caller as separate candidates.
    top_specs = _best_matches(spec_matches)
    top_bosses = _best_matches(boss_matches)

    # Lorrgs ranks Mythic and Heroic only, so a normal/lfr question has no spec ranking to hand over.
    ranked = difficulty is None or difficulty in RANKED_DIFFICULTIES
    candidates: list[dict[str, Any]] = [
        _spec_ranking_candidate(spec, boss, known_terms, difficulty)
        for spec, boss in product(top_specs, top_bosses)
        if ranked
    ]
    candidates.extend(_comp_ranking_candidate(boss, known_terms, difficulty) for boss in top_bosses)
    candidates.extend(_spec_candidate(spec, known_terms) for spec in top_specs)
    candidates.extend(_boss_candidate(boss, known_terms) for boss in top_bosses)
    candidates = _dedupe_candidates(candidates)
    candidates.sort(key=lambda row: (-_score(row), KIND_ORDER.get(str(row.get("kind")), 9), str(row.get("name") or "")))
    return candidates


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
            return [_explicit_spec_ranking_candidate(route.spec_slug, route.boss_slug, route.difficulty, route.source_url)]
        if route.kind == "comp_ranking" and route.boss_slug:
            return [_explicit_comp_ranking_candidate(route.boss_slug, route.source_url)]
        if route.report is not None:
            return _report_candidates(route.report, url=route.source_url)
    report = parse_report_reference(query)
    if report is not None:
        return _report_candidates(report)
    return []


def _report_candidates(ref: ReportReference, *, url: str | None = None) -> list[dict[str, Any]]:
    """Report rows; ``url`` is the Lorrgs page the caller passed, since a bare code or a Warcraft Logs URL names none."""
    quoted = shlex.quote(ref.code)
    overview = discovery_row(
        provider="lorrgs",
        kind="report_overview",
        id=f"report:{ref.code}",
        name=f"Lorrgs report overview {ref.code}",
        url=url,
        # The reference parsed cleanly, but nothing here checked that Lorrgs can serve this report:
        # load_overview loads any public report on demand, yet answers 401 for one Warcraft Logs
        # keeps private and 404 for a code that does not exist, so the handoff stays at medium.
        score=88,
        match_reasons=["explicit_report_reference"],
        command=f"lorrgs report-overview {quoted}",
        surface="report-overview",
        report_id=ref.code,
        fight_id=ref.fight_id,
        report_type=ref.report_type,
        source_url=ref.source_url,
        caveat="Availability is unverified: Lorrgs loads any public report, but refuses reports Warcraft Logs keeps private.",
    )
    if ref.fight_id is None:
        return [overview]
    fight = discovery_row(
        provider="lorrgs",
        kind="user_report_fights",
        id=f"report:{ref.code}:fight:{ref.fight_id}",
        name=f"Lorrgs cached fight {ref.fight_id} for report {ref.code}",
        url=url,
        score=68,
        match_reasons=["explicit_report_reference", "fight_scope_present"],
        command=_report_fights_command(ref, quoted),
        surface="user-report-fights",
        report_id=ref.code,
        fight_id=ref.fight_id,
        report_type=ref.report_type,
        source_url=ref.source_url,
        caveat="Requires the selected fight to already be loaded/cached by Lorrgs.",
    )
    return [overview, fight]


def _report_fights_command(ref: ReportReference, quoted_code: str) -> str:
    command = f"lorrgs user-report-fights {quoted_code} --fight {ref.fight_id}"
    if ref.report_type:
        command += f" --type {shlex.quote(ref.report_type)}"
    return command


def _explicit_spec_ranking_candidate(
    spec_slug: str, boss_slug: str, difficulty: str | None, source_url: str | None
) -> dict[str, Any]:
    return _spec_ranking_row(
        spec_slug,
        boss_slug,
        difficulty,
        name=f"Lorrgs {spec_slug} on {boss_slug}",
        ranking={"score": 99, "match_reasons": ["explicit_lorrgs_spec_ranking_url"]},
        source_url=source_url,
    )


def _explicit_comp_ranking_candidate(boss_slug: str, source_url: str | None) -> dict[str, Any]:
    return _comp_ranking_row(
        boss_slug,
        name=f"Lorrgs composition ranking for {boss_slug}",
        ranking={"score": 96, "match_reasons": ["explicit_lorrgs_comp_ranking_url"]},
        source_url=source_url,
    )


def _match_rows(rows: list[Any], query_terms: set[str]) -> list[RowMatch]:
    matches: list[RowMatch] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        terms = query_terms & set(_words(_row_match_text(row)))
        if terms:
            matches.append(RowMatch(row=row, level=_identity_level(row, query_terms), terms=frozenset(terms)))
    return matches


def _best_matches(matches: list[RowMatch]) -> list[RowMatch]:
    """Every row tied for the strongest match: a tie is ambiguity, not a coin flip."""
    if not matches:
        return []
    best = max(match.strength for match in matches)
    return sorted((match for match in matches if match.strength == best), key=lambda match: _row_name(match.row))


def _identity_level(row: dict[str, Any], query_terms: set[str]) -> int:
    """How precisely the query named this row, ignoring filler words on both sides.

    Stop words are stripped from the row's own names too: "the-eye-of-the-jailer" is named outright by
    the query "the eye of the jailer", and treating "the"/"of" as missing used to hand that query to
    "The Jailer, Zovaal" instead.
    """
    for key in ("full_name_slug", "full_name"):
        terms = _query_terms(str(row.get(key) or ""))
        if terms and terms <= query_terms:
            return NAMED
    short_terms = _query_terms(str(row.get("name") or ""))
    if short_terms and short_terms <= query_terms:
        return SHORT_NAME
    return PARTIAL


def _row_match_text(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("full_name_slug", "full_name", "name", "name_slug"))
    class_info = row.get("class")
    if isinstance(class_info, dict):
        text += " " + " ".join(str(class_info.get(key) or "") for key in ("name", "name_slug"))
    return text


def _free_text_ranking(matches: tuple[RowMatch, ...], known_terms: frozenset[str], reasons: list[str]) -> dict[str, Any]:
    """Rank a candidate by how much of the recognised query it accounts for, and how exactly.

    Coverage dominates the score so that a candidate always outranks the narrower ones built from the
    same rows, and `unmatched_terms` names what it had to ignore, which is what stops `resolve` from
    answering a question the caller did not ask.
    """
    covered = frozenset[str]().union(*(match.terms for match in matches))
    level = min(match.level for match in matches)
    coverage = len(covered) / len(known_terms) if known_terms else 0.0
    return {
        "score": 24 + round(70 * coverage) + level,
        "match_level": MATCH_LEVEL_NAMES[level],
        "matched_terms": sorted(covered),
        "unmatched_terms": sorted(known_terms - covered),
        "match_reasons": reasons,
    }


def _row_name(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("full_name") or row.get("name") or row.get("full_name_slug") or "")


def _row_slug(row: dict[str, Any]) -> str:
    return str(row.get("full_name_slug") or row.get("name_slug") or "").strip()


def _match_reason(role: str, match: RowMatch) -> str:
    return f"{role}_{MATCH_LEVEL_NAMES[match.level]}"


def _spec_ranking_candidate(
    spec_match: RowMatch, boss_match: RowMatch, known_terms: frozenset[str], difficulty: str | None
) -> dict[str, Any]:
    return _spec_ranking_row(
        _row_slug(spec_match.row),
        _row_slug(boss_match.row),
        difficulty,
        name=f"{_row_name(spec_match.row)} on {_row_name(boss_match.row)}",
        ranking=_free_text_ranking(
            (spec_match, boss_match),
            known_terms,
            [_match_reason("spec", spec_match), _match_reason("boss", boss_match)],
        ),
    )


def _comp_ranking_candidate(boss_match: RowMatch, known_terms: frozenset[str], difficulty: str | None) -> dict[str, Any]:
    boss_slug = _row_slug(boss_match.row)
    ranking = _free_text_ranking((boss_match,), known_terms, [_match_reason("boss", boss_match)])
    if difficulty not in (None, "mythic"):
        # comp-ranking takes no difficulty, so it cannot answer a heroic/normal/lfr question.
        ranking["unmatched_terms"] = sorted([*ranking["unmatched_terms"], difficulty])
    return _comp_ranking_row(boss_slug, name=f"{COMP_RANKING_NAME_PREFIX}{_row_name(boss_match.row)}", ranking=ranking)


def _spec_candidate(spec_match: RowMatch, known_terms: frozenset[str]) -> dict[str, Any]:
    spec_slug = _row_slug(spec_match.row)
    return _row(
        "spec",
        f"spec:{spec_slug}",
        name=_row_name(spec_match.row),
        url=None,
        ranking=_free_text_ranking((spec_match,), known_terms, [_match_reason("spec", spec_match)]),
        command=f"lorrgs spec {shlex.quote(spec_slug)}",
        surface="spec",
        spec_slug=spec_slug,
    )


def _boss_candidate(boss_match: RowMatch, known_terms: frozenset[str]) -> dict[str, Any]:
    boss_slug = _row_slug(boss_match.row)
    return _row(
        "boss",
        f"boss:{boss_slug}",
        name=_row_name(boss_match.row),
        url=None,
        ranking=_free_text_ranking((boss_match,), known_terms, [_match_reason("boss", boss_match)]),
        command=f"lorrgs boss {shlex.quote(boss_slug)}",
        surface="boss",
        boss_slug=boss_slug,
    )


def _spec_ranking_row(
    spec_slug: str, boss_slug: str, difficulty: str | None, *, name: str, ranking: dict[str, Any], source_url: str | None = None
) -> dict[str, Any]:
    """A ``lorrgs spec-ranking`` candidate, whose ``url`` is the Lorrgs page that ranking renders."""
    command = f"lorrgs spec-ranking {shlex.quote(spec_slug)} {shlex.quote(boss_slug)}"
    url = f"{SITE_HOST}/spec_ranking/{quote(spec_slug)}/{quote(boss_slug)}"
    if difficulty:
        command += f" --difficulty {shlex.quote(difficulty)}"
        url += f"?difficulty={quote(difficulty)}"
    extra = {"source_url": source_url} if source_url else {}
    return _row(
        "spec_ranking",
        f"spec-ranking:{spec_slug}:{boss_slug}",
        name=name,
        url=url,
        ranking=ranking,
        command=command,
        surface="spec-ranking",
        spec_slug=spec_slug,
        boss_slug=boss_slug,
        difficulty=difficulty,
        **extra,
    )


def _comp_ranking_row(boss_slug: str, *, name: str, ranking: dict[str, Any], source_url: str | None = None) -> dict[str, Any]:
    """A ``lorrgs comp-ranking`` candidate, whose ``url`` is the Lorrgs page that ranking renders."""
    extra = {"source_url": source_url} if source_url else {}
    return _row(
        "comp_ranking",
        f"comp-ranking:{boss_slug}",
        name=name,
        url=f"{SITE_HOST}/comp_ranking/{quote(boss_slug)}",
        ranking=ranking,
        command=f"lorrgs comp-ranking {shlex.quote(boss_slug)}",
        surface="comp-ranking",
        boss_slug=boss_slug,
        **extra,
    )


def _row(
    kind: str, row_id: str, *, name: str, url: str | None, ranking: dict[str, Any], command: str, surface: str, **extra: Any
) -> dict[str, Any]:
    """A free-text or ranking-URL candidate: ``ranking`` carries Lorrgs' own match detail beside the core."""
    score, reasons = ranking["score"], ranking["match_reasons"]
    ranking_extra = {key: value for key, value in ranking.items() if key not in {"score", "match_reasons"}}
    return discovery_row(
        provider="lorrgs",
        kind=kind,
        id=row_id,
        name=name,
        url=url,
        score=score,
        match_reasons=reasons,
        command=command,
        surface=surface,
        ranking_extra=ranking_extra,
        **extra,
    )


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


def _unambiguous(best: dict[str, Any], results: list[dict[str, Any]]) -> bool:
    """The top candidate resolves only when it accounts for the whole query and has no equal rival.

    The rival check is per kind and per strength: the runner-up is normally the comp ranking or the
    bare boss built from the *same* rows, which is a narrower view of one answer, not a competing one.
    Two candidates of one kind that matched equally well but name different entities are the real
    ambiguity ("frost <boss>" is Frost Mage and Frost Death Knight), and those must not be guessed.
    """
    ranking = best.get("ranking")
    if isinstance(ranking, dict) and ranking.get("unmatched_terms"):
        return False
    strength = _match_strength(best)
    entities = _entities(best)
    return not any(
        _match_strength(row) == strength and _entities(row) != entities
        for row in results
        if row is not best and row.get("kind") == best.get("kind")
    )


def _match_strength(candidate: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    ranking = candidate.get("ranking")
    if not isinstance(ranking, dict):
        return "", ()
    return str(ranking.get("match_level") or ""), tuple(ranking.get("matched_terms") or ())


def _entities(candidate: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(candidate.get(key) or "") for key in ("spec_slug", "boss_slug", "report_id"))


def _names_single_word(word: str, row: Mapping[str, Any]) -> bool:
    """Lorrgs' own answer to a one-word query: a spec word only one class has, or the boss's own name.

    "shadow" names the Shadow Priest spec; a shared spec word ("frost") never resolves. A boss is
    named by its whole name or its head before ',' or before an epithet Lorrgs writes without the
    in-game comma ("chimaerus" -> "Chimaerus the Undreamt God"), so "anduin" does not name
    "Anduin Wrynn" nor "jailer" "The Jailer, Zovaal".
    """
    if row["kind"] == "spec":
        return unique_spec_class(word) is not None
    boss = str(row["name"]).removeprefix(COMP_RANKING_NAME_PREFIX)
    return row["kind"] in {"boss", "comp_ranking"} and single_word_named(word, boss.split(" the ", 1)[0])


def _confidence(candidates: list[dict[str, Any]]) -> ResolveConfidence:
    """Like every other provider, resolve only hands over a command at high confidence.

    A partial word match or an unverified report reference stays the match at medium, and an
    ambiguous top row (a tie, or one that left query words out) stays the match at low.
    """
    if not candidates:
        return "none"
    best = candidates[0]
    if not _unambiguous(best, candidates):
        return "low"
    if best.get("kind") in REPORT_KINDS or best["ranking"].get("match_level") == MATCH_LEVEL_NAMES[PARTIAL]:
        return "medium"
    return "high"
