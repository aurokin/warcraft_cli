from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, TypeGuard
from urllib.parse import urlparse

from warcraft_core.expansions import wowhead_path_prefixes
from warcraft_core.wow_normalization import normalized_text
from warcraft_core.wow_specs import WOW_CLASS_NAMES, WOW_SPECS

# Warcraft Logs report codes are 16 alphanumerics with mixed case and often no digit (JVFTxcKCqrvpaAzD).
# A code must mix upper and lower case or letters and digits, so a slug such as frostdeathknight or a
# guild name is never read as a code.
_WARCRAFTLOGS_REPORT_CODE = re.compile(
    r"^(?:(?=.*[a-z])(?=.*[A-Z])[A-Za-z0-9]{16}|(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{8,32})$"
)
_CAMEL_CASE_NAME = re.compile(r"(?:[A-Z][a-z]+)+")
IdentityStatus = Literal["unknown", "normalized", "canonical", "inferred", "ambiguous"]
IdentityConfidence = Literal["none", "low", "medium", "high"]
TalentTransportStatus = Literal["unknown", "raw_only", "validated", "exact"]
WOWHEAD_TALENT_CALC_SEGMENT = "talent-calc"
WOWHEAD_EXPANSION_PREFIXES = wowhead_path_prefixes()
# Wowhead calculator path prefixes: every expansion site's, plus WoW Forever's (no expansion profile yet).
WOWHEAD_TALENT_CALC_PREFIXES = WOWHEAD_EXPANSION_PREFIXES | {"forever"}
# Calculators whose paths use the retail spec table; classic ones have their own (MoP Classic's rogue ``combat``).
RETAIL_TALENT_CALCULATORS = frozenset({"retail", "ptr", "beta"})
# Calculators that pick one talent per tier and carry a glyph segment after the build code.
TIERED_TALENT_CALCULATORS = frozenset({"mop-classic"})
# The calculators SimC and build references read: retail and the expansion sites (not WoW Forever).
_EXPANSION_SITE_CALCULATORS = WOWHEAD_EXPANSION_PREFIXES | {"retail"}
# Spec slugs as normalize_spec_name spells Wowhead's talent-calc path segments (beast-mastery ->
# beast_mastery). A talent-calc path only names a spec when its third segment is one of these.
WOW_SPECS_BY_CLASS: dict[str, frozenset[str]] = {
    class_key: frozenset(spec.key for spec in WOW_SPECS if spec.class_key == class_key) for class_key in WOW_CLASS_NAMES
}
WOW_CLASS_SLUGS = frozenset(WOW_SPECS_BY_CLASS)


def unique_spec_class(word: str) -> str | None:
    """The one class whose spec ``word`` names ("shadow" -> "priest"); ``None`` for a shared spec ("frost") or no spec."""
    classes = [actor_class for actor_class, specs in WOW_SPECS_BY_CLASS.items() if word in specs]
    return classes[0] if len(classes) == 1 else None


def _is_wowhead_hostname(hostname: str | None) -> bool:
    if not isinstance(hostname, str):
        return False
    normalized = hostname.lower()
    return normalized == "wowhead.com" or normalized.endswith(".wowhead.com")


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _clean_notes(notes: list[str] | tuple[str, ...] | None) -> list[str]:
    if not notes:
        return []
    cleaned: list[str] = []
    for note in notes:
        text = _clean_text(note)
        if text:
            cleaned.append(text)
    return cleaned


def is_warcraftlogs_report_code(code: str, *, from_url: bool = False) -> bool:
    """Whether ``code`` reads as a Warcraft Logs report code.

    A bare word made of capitalised words (HavocDemonHunter) is a name, not a code. Only bare words
    are checked: a random code has this shape about once in 1750, and a /reports/<code> URL path
    (``from_url``) is a code.
    """
    if not _WARCRAFTLOGS_REPORT_CODE.fullmatch(code):
        return False
    return from_url or not _CAMEL_CASE_NAME.fullmatch(code)


def is_transport_int(value: Any) -> TypeGuard[int]:
    """A talent entry, node id or rank: an int that is not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_actor_class(value: str | None) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", text.lower())
    return normalized or None


def normalize_spec_name(value: str | None) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    # Warcraft Logs spells Beast Mastery as BeastMastery, so a lower-to-upper case change splits words.
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", text)
    normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return normalized or None


def normalize_encounter_name(value: str | None) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    normalized = normalized_text(text)
    return normalized.replace(" ", "-") if normalized else None


def normalize_ability_name(value: str | None) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    normalized = normalized_text(text)
    return normalized.replace(" ", "_") if normalized else None


_TALENT_CALC_EMPTY_SEGMENTS = "talent-calc reference must not include empty path segments."
_TALENT_CALC_LAYOUT = (
    "Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>] or "
    "/talent-calc/<class>/<build-code> with a WoW class."
)
# A spec path segment (``balance``, ``beast-mastery``); build codes carry digits or capitals.
_TALENT_CALC_SPEC_SEGMENT = re.compile(r"[a-z]+(?:-[a-z]+)*")


@dataclass(frozen=True, slots=True)
class WowheadTalentCalcRef:
    """A Wowhead talent calculator reference split into its calculator, class, spec and build code.

    ``expansion`` is the calculator: an expansion key, or ``forever`` for WoW Forever. Classic-era
    paths name no spec (``/classic/talent-calc/warrior/<code>``), so ``spec`` is None there.
    ``extra_segment`` is what follows the build code: a classic calculator's talent selection order
    or a Mists of Pandaria Classic glyph code. ``explicit_path`` is False for a shorthand ref
    (``druid/balance/<code>``) that never named ``/talent-calc``.
    """

    reference_url: str
    expansion: str
    class_slug: str
    actor_class: str
    spec_slug: str | None
    spec: str | None
    build_code: str | None
    extra_segment: str | None
    path_segments: tuple[str, ...]
    explicit_path: bool


@dataclass(frozen=True, slots=True)
class WowheadTalentCalcRefError:
    """Why a ref is no Wowhead talent calculator reference.

    ``targets_talent_calc`` is True when the ref still aims at a talent calculator (a Wowhead URL
    whose path holds ``talent-calc``, or a shorthand that starts with a class), so a router hands it
    to Wowhead to report ``message`` rather than reading it as something else.
    """

    message: str
    targets_talent_calc: bool


def _talent_calc_segments(
    expansion: str, segments: list[str], *, spec_aliases: frozenset[str] = frozenset()
) -> tuple[str | None, str | None, str | None] | str:
    """Spec slug, build code and extra segment from the path segments after the class, or why they do not fit.

    A spec-shaped first segment is a spec (``balance``); otherwise it is the build code, which a
    classic calculator may follow with its selection order or MoP Classic with its glyph code.
    """
    slot, *tail = segments
    if _TALENT_CALC_SPEC_SEGMENT.fullmatch(slot) or normalize_spec_name(slot) in spec_aliases:
        if len(tail) > (2 if expansion in TIERED_TALENT_CALCULATORS else 1):
            return _TALENT_CALC_LAYOUT
        return slot, (tail[0] if tail else None), (tail[1] if len(tail) > 1 else None)
    if tail and expansion in RETAIL_TALENT_CALCULATORS:
        return f"Talent calculator spec segment {slot!r} is not a spec name."
    if len(tail) > 1:
        return _TALENT_CALC_LAYOUT
    return None, slot, (tail[0] if tail else None)


def _bare_ref_targets_talent_calc(parts: list[str]) -> bool:
    """A shorthand ref aims at a calculator when it starts (after any calculator prefix) with a class or talent-calc."""
    if parts and parts[0] in WOWHEAD_TALENT_CALC_PREFIXES:
        parts = parts[1:]
    if not parts:
        return False
    if parts[0] == WOWHEAD_TALENT_CALC_SEGMENT or normalize_actor_class(parts[0]) in WOW_CLASS_SLUGS:
        return True
    return len(parts) >= 2 and parts[1] == WOWHEAD_TALENT_CALC_SEGMENT


def _talent_calc_location(
    ref: str, default_expansion: str
) -> tuple[str, str, list[str], bool, bool] | WowheadTalentCalcRefError:
    """The ref's base URL (scheme and host), calculator, path parts from ``talent-calc`` on, explicitness and aim."""
    lowered = ref.lower()
    candidate = f"https://{ref}" if lowered.startswith(("wowhead.com/", "www.wowhead.com/")) else ref
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    parsed = urlparse(candidate)
    is_url = bool(parsed.scheme and parsed.netloc)
    raw_path = parsed.path if is_url else re.split(r"[?#]", ref, maxsplit=1)[0]
    raw_parts = raw_path.strip("/").split("/") if raw_path.strip("/") else []
    nonempty = [part for part in raw_parts if part]
    if is_url and not _is_wowhead_hostname(parsed.hostname):
        return WowheadTalentCalcRefError("talent-calc URL must point to wowhead.com.", False)
    targets = WOWHEAD_TALENT_CALC_SEGMENT in nonempty if is_url else _bare_ref_targets_talent_calc(nonempty)
    if "" in raw_parts:
        return WowheadTalentCalcRefError(_TALENT_CALC_EMPTY_SEGMENTS, targets)
    head = raw_parts[0] if raw_parts else ""
    expansion = head if head in WOWHEAD_TALENT_CALC_PREFIXES else ("retail" if is_url else default_expansion)
    parts = raw_parts[1:] if head in WOWHEAD_TALENT_CALC_PREFIXES else raw_parts
    explicit = bool(parts) and parts[0] == WOWHEAD_TALENT_CALC_SEGMENT
    if is_url:
        if not explicit:
            return WowheadTalentCalcRefError("Talent calculator URL must point to /talent-calc.", targets)
        return f"{parsed.scheme}://{parsed.netloc}", expansion, parts, True, targets
    if not explicit:
        parts = [WOWHEAD_TALENT_CALC_SEGMENT, *parts]
    return "https://www.wowhead.com", expansion, parts, explicit, targets


def parse_wowhead_talent_calc(
    ref: str, *, default_expansion: str = "retail", allow_spec_aliases: bool = False
) -> WowheadTalentCalcRef | WowheadTalentCalcRefError:
    """Parse a Wowhead talent calculator URL, path or ``<class>/<spec>/<code>`` shorthand.

    The one parser behind Wowhead's ``talent-calc``, the wrapper's talent routing and the build
    references in guides. A shorthand or a path without a calculator prefix reads as
    ``default_expansion``'s calculator; a URL without one is retail.
    ``allow_spec_aliases`` preserves build references' historical normalized spec spellings;
    Wowhead tool refs use the calculator's lowercase spec-slug grammar.
    """
    text = ref.strip()
    if not text:
        return WowheadTalentCalcRefError("talent-calc reference cannot be empty.", False)
    location = _talent_calc_location(text, default_expansion)
    if isinstance(location, WowheadTalentCalcRefError):
        return location
    base, expansion, parts, explicit, targets = location
    segments = parts[1:]
    max_segments = 4 if expansion in TIERED_TALENT_CALCULATORS else 3
    actor_class = normalize_actor_class(segments[0]) if segments else None
    if not 2 <= len(segments) <= max_segments or actor_class not in WOW_CLASS_SLUGS:
        return WowheadTalentCalcRefError(_TALENT_CALC_LAYOUT, targets)
    split = _talent_calc_segments(
        expansion, segments[1:], spec_aliases=WOW_SPECS_BY_CLASS[actor_class] if allow_spec_aliases else frozenset()
    )
    if isinstance(split, str):
        return WowheadTalentCalcRefError(split, targets)
    spec_slug, build_code, extra_segment = split
    spec = normalize_spec_name(spec_slug)
    if spec_slug is not None and expansion in RETAIL_TALENT_CALCULATORS and spec not in WOW_SPECS_BY_CLASS[actor_class]:
        return WowheadTalentCalcRefError(f"Talent calculator spec {spec_slug!r} is not a {segments[0]} spec.", targets)
    # Every calculator's path prefix is its expansion key; retail (and a key with no Wowhead site) has none.
    prefix = f"/{expansion}" if expansion in WOWHEAD_TALENT_CALC_PREFIXES else ""
    return WowheadTalentCalcRef(
        reference_url=f"{base}{prefix}/{'/'.join(parts)}",
        expansion=expansion,
        class_slug=segments[0],
        actor_class=actor_class,
        spec_slug=spec_slug,
        spec=spec,
        build_code=build_code,
        extra_segment=extra_segment,
        path_segments=tuple(segments),
        explicit_path=explicit,
    )


def parse_wowhead_talent_calc_ref(ref: str) -> dict[str, str | None] | None:
    """A talent-calc URL or path that names ``/talent-calc``, a class and one of its retail specs, else None.

    The narrow form build references and SimC read; :func:`parse_wowhead_talent_calc` reads every form.
    """
    parsed = parse_wowhead_talent_calc(ref, allow_spec_aliases=True)
    if (
        isinstance(parsed, WowheadTalentCalcRefError)
        or not parsed.explicit_path
        or parsed.extra_segment is not None
        or parsed.expansion not in _EXPANSION_SITE_CALCULATORS
        or parsed.spec not in WOW_SPECS_BY_CLASS[parsed.actor_class]
    ):
        return None
    return {
        "actor_class": parsed.actor_class,
        "spec": parsed.spec,
        "build_code": parsed.build_code,
        "reference_url": parsed.reference_url,
        "source_kind": "wowhead_talent_calc_url",
    }


def class_spec_identity_payload(
    *,
    actor_class: str | None,
    spec: str | None,
    provider: str | None = None,
    source: str | None = None,
    confidence: IdentityConfidence = "none",
    canonical: bool = False,
    inferred: bool = False,
    candidates: list[tuple[str | None, str | None]] | None = None,
    notes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    normalized_actor_class = normalize_actor_class(actor_class)
    normalized_spec = normalize_spec_name(spec)
    candidate_rows = [
        {
            "actor_class": normalize_actor_class(candidate_actor_class),
            "spec": normalize_spec_name(candidate_spec),
        }
        for candidate_actor_class, candidate_spec in (candidates or [])
    ]
    cleaned_notes = _clean_notes(notes)
    if canonical and normalized_actor_class and normalized_spec:
        status: IdentityStatus = "canonical"
    elif inferred and normalized_actor_class and normalized_spec:
        status = "inferred"
    elif len(candidate_rows) > 1 and not (normalized_actor_class and normalized_spec):
        status = "ambiguous"
    elif normalized_actor_class or normalized_spec:
        status = "normalized"
    else:
        status = "unknown"
    payload: dict[str, object] = {
        "kind": "class_spec_identity",
        "status": status,
        "confidence": confidence,
        "identity": {
            "actor_class": normalized_actor_class,
            "spec": normalized_spec,
        },
        "candidate_count": len(candidate_rows),
        "candidates": candidate_rows,
        "notes": cleaned_notes,
    }
    if provider is not None or source is not None:
        payload["source"] = {"provider": provider, "source": source}
    return payload


def encounter_identity_payload(
    *,
    encounter_id: int | None,
    journal_id: int | None = None,
    name: str | None = None,
    zone_id: int | None = None,
    provider: str | None = None,
    source: str | None = None,
    notes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    normalized_name = normalize_encounter_name(name)
    status: IdentityStatus = (
        "canonical"
        if encounter_id is not None or journal_id is not None
        else ("normalized" if normalized_name else "unknown")
    )
    payload: dict[str, object] = {
        "kind": "encounter_identity",
        "status": status,
        "identity": {
            "encounter_id": encounter_id,
            "journal_id": journal_id,
            "zone_id": zone_id,
            "normalized_name": normalized_name,
        },
        "notes": _clean_notes(notes),
    }
    if provider is not None or source is not None:
        payload["source"] = {"provider": provider, "source": source}
    return payload


def ability_identity_payload(
    *,
    spell_id: int | None = None,
    game_id: int | None = None,
    name: str | None = None,
    provider: str | None = None,
    source: str | None = None,
    notes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    normalized_name = normalize_ability_name(name)
    status: IdentityStatus = (
        "canonical"
        if spell_id is not None or game_id is not None
        else ("normalized" if normalized_name else "unknown")
    )
    payload: dict[str, object] = {
        "kind": "ability_identity",
        "status": status,
        "identity": {
            "spell_id": spell_id,
            "game_id": game_id,
            "normalized_name": normalized_name,
        },
        "notes": _clean_notes(notes),
    }
    if provider is not None or source is not None:
        payload["source"] = {"provider": provider, "source": source}
    return payload


def report_actor_identity_payload(
    *,
    report_code: str | None,
    fight_id: int | None,
    actor_id: int | None,
    name: str | None = None,
    actor_class: str | None = None,
    spec: str | None = None,
    provider: str | None = None,
    source: str | None = None,
    notes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    local_key = f"{report_code}:{fight_id}:{actor_id}" if report_code and fight_id is not None and actor_id is not None else None
    status: IdentityStatus = "canonical" if local_key is not None else ("normalized" if any((name, actor_class, spec)) else "unknown")
    payload: dict[str, object] = {
        "kind": "report_actor_identity",
        "status": status,
        "scope": {
            "type": "report_fight",
            "report_code": report_code,
            "fight_id": fight_id,
        },
        "identity": {
            "actor_id": actor_id,
            "local_key": local_key,
            "name": _clean_text(name),
            "actor_class": normalize_actor_class(actor_class),
            "spec": normalize_spec_name(spec),
        },
        "notes": _clean_notes(notes),
    }
    if provider is not None or source is not None:
        payload["source"] = {"provider": provider, "source": source}
    return payload


def build_identity_payload(
    *,
    actor_class: str | None,
    spec: str | None,
    confidence: IdentityConfidence,
    source: str,
    candidates: list[tuple[str | None, str | None]] | None = None,
    source_notes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    normalized_actor_class = normalize_actor_class(actor_class)
    normalized_spec = normalize_spec_name(spec)
    candidate_rows = [
        {
            "actor_class": normalize_actor_class(candidate_actor_class),
            "spec": normalize_spec_name(candidate_spec),
        }
        for candidate_actor_class, candidate_spec in (candidates or [])
    ]
    if normalized_actor_class and normalized_spec:
        status: IdentityStatus = "inferred"
    elif len(candidate_rows) > 1:
        status = "ambiguous"
    else:
        status = "unknown"
    return {
        "kind": "build_identity",
        "status": status,
        "confidence": confidence,
        "canonical": False,
        "source": source,
        "class_spec_identity": class_spec_identity_payload(
            actor_class=normalized_actor_class,
            spec=normalized_spec,
            source=source,
            confidence=confidence,
            inferred=bool(normalized_actor_class and normalized_spec),
            candidates=[(row["actor_class"], row["spec"]) for row in candidate_rows],
            notes=source_notes,
        ),
        "candidate_count": len(candidate_rows),
        "candidates": candidate_rows,
        "source_notes": _clean_notes(source_notes),
    }


def build_reference_payload(
    *,
    ref: str,
    provider: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    label: str | None = None,
    notes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object] | None:
    parsed = parse_wowhead_talent_calc_ref(ref)
    if parsed is None:
        return None
    source_notes = ["class/spec came from the explicit Wowhead talent-calc URL path"]
    if parsed["build_code"] is not None:
        source_notes.append("wowhead build code")
    source_notes.extend(_clean_notes(notes))
    payload: dict[str, object] = {
        "kind": "build_reference",
        "reference_type": "wowhead_talent_calc_url",
        "url": parsed["reference_url"],
        "label": _clean_text(label),
        "build_code": parsed["build_code"],
        "source_url": source_url,
        "build_identity": build_identity_payload(
            actor_class=parsed["actor_class"],
            spec=parsed["spec"],
            confidence="high",
            source="wowhead_talent_calc_url",
            source_notes=source_notes,
        ),
    }
    if provider is not None or source is not None:
        payload["source"] = {"provider": provider, "source": source}
    return payload


def _clean_transport_form_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        nested = {
            nested_key: cleaned_value
            for nested_key, nested_value in value.items()
            if (cleaned_value := _clean_transport_form_value(nested_value)) is not None
        }
        return nested or None
    return value if value is not None else None


def _clean_transport_forms(transport_forms: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: cleaned_value
        for key, value in (transport_forms or {}).items()
        if (cleaned_value := _clean_transport_form_value(value)) is not None
    }


def _clean_payload_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _packet_class_spec_identity(build_identity: dict[str, Any]) -> tuple[str | None, str | None]:
    class_spec_identity = build_identity.get("class_spec_identity")
    if not isinstance(class_spec_identity, dict):
        return None, None
    identity = class_spec_identity.get("identity")
    if not isinstance(identity, dict):
        return None, None
    actor_class = normalize_actor_class(identity.get("actor_class")) if isinstance(identity.get("actor_class"), str) else None
    spec = normalize_spec_name(identity.get("spec")) if isinstance(identity.get("spec"), str) else None
    return actor_class, spec


def _validated_class_spec_identity(validation: dict[str, Any]) -> tuple[str | None, str | None]:
    actor_class = normalize_actor_class(validation.get("actor_class")) if isinstance(validation.get("actor_class"), str) else None
    spec = normalize_spec_name(validation.get("spec")) if isinstance(validation.get("spec"), str) else None
    return actor_class, spec


def _is_usable_talent_tree_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    return all(is_transport_int(row.get(key)) for key in ("entry", "node_id", "rank"))


def _has_usable_raw_talent_evidence(raw_evidence: dict[str, Any]) -> bool:
    rows = raw_evidence.get("talent_tree_entries")
    return isinstance(rows, list) and bool(rows) and all(_is_usable_talent_tree_row(row) for row in rows)


def _talent_transport_payload_parts(
    *,
    transport_forms: dict[str, Any] | None,
    raw_evidence: dict[str, Any] | None,
    validation: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], TalentTransportStatus]:
    cleaned_transport_forms = _clean_transport_forms(transport_forms)
    raw_payload = _clean_payload_dict(raw_evidence)
    validation_payload = _clean_payload_dict(validation)
    status = _talent_transport_status(
        transport_forms=cleaned_transport_forms,
        raw_evidence=raw_payload,
        validation=validation_payload,
    )
    return cleaned_transport_forms, raw_payload, validation_payload, status


def build_reference_transport_packet_payload(
    *,
    ref: str,
    provider: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    source_urls: list[str] | tuple[str, ...] | None = None,
    label: str | None = None,
    notes: list[str] | tuple[str, ...] | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, object] | None:
    parsed = parse_wowhead_talent_calc_ref(ref)
    if parsed is None or parsed["build_code"] is None:
        return None
    cleaned_source_urls = _clean_notes(list(source_urls or []))
    raw_evidence: dict[str, Any] = {
        "reference_type": "wowhead_talent_calc_url",
        "reference_url": parsed["reference_url"],
    }
    cleaned_label = _clean_text(label)
    cleaned_source_url = _clean_text(source_url)
    if cleaned_label is not None:
        raw_evidence["label"] = cleaned_label
    if cleaned_source_url is not None:
        raw_evidence["source_url"] = cleaned_source_url
    if cleaned_source_urls:
        raw_evidence["source_urls"] = cleaned_source_urls
    source_notes = ["exact transport form came from the explicit Wowhead talent-calc URL"]
    if parsed["build_code"] is not None:
        source_notes.append("wowhead build code")
    source_notes.extend(_clean_notes(notes))
    return talent_transport_packet_payload(
        actor_class=parsed["actor_class"],
        spec=parsed["spec"],
        confidence="high",
        source=source or "wowhead_talent_calc_url",
        provider=provider,
        transport_forms={"wowhead_talent_calc_url": parsed["reference_url"]},
        raw_evidence=raw_evidence,
        validation={},
        scope=scope,
        source_notes=source_notes,
    )


def talent_transport_packet_payload(
    *,
    actor_class: str | None,
    spec: str | None,
    confidence: IdentityConfidence,
    source: str,
    transport_forms: dict[str, Any] | None = None,
    raw_evidence: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    provider: str | None = None,
    source_notes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    cleaned_transport_forms, raw_payload, validation_payload, status = _talent_transport_payload_parts(
        transport_forms=transport_forms,
        raw_evidence=raw_evidence,
        validation=validation,
    )
    payload: dict[str, object] = {
        "kind": "talent_transport_packet",
        "transport_status": status,
        "build_identity": build_identity_payload(
            actor_class=actor_class,
            spec=spec,
            confidence=confidence,
            source=source,
            source_notes=source_notes,
        ),
        "transport_forms": cleaned_transport_forms,
        "raw_evidence": raw_payload,
        "validation": validation_payload,
        "scope": scope if isinstance(scope, dict) else {},
    }
    if provider is not None or source is not None:
        payload["source"] = {"provider": provider, "source": source}
    return validate_talent_transport_packet(payload)


def refresh_talent_transport_packet(
    packet: dict[str, Any],
    *,
    transport_forms: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    build_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    refreshed = dict(packet)
    cleaned_transport_forms, raw_payload, validation_payload, status = _talent_transport_payload_parts(
        transport_forms=transport_forms,
        raw_evidence=refreshed.get("raw_evidence") if isinstance(refreshed.get("raw_evidence"), dict) else {},
        validation=validation,
    )
    refreshed["transport_forms"] = cleaned_transport_forms
    refreshed["validation"] = validation_payload
    refreshed["transport_status"] = status
    refreshed["raw_evidence"] = raw_payload
    if build_identity is not None:
        refreshed["build_identity"] = build_identity
    return validate_talent_transport_packet(refreshed)


def _talent_transport_envelope(packet: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if packet.get("kind") != "talent_transport_packet":
        raise ValueError(f"Unsupported talent transport packet kind: {packet.get('kind')!r}")

    transport_status = packet.get("transport_status")
    if transport_status not in {"unknown", "raw_only", "validated", "exact"}:
        raise ValueError(f"Unsupported talent transport status: {transport_status!r}")

    build_identity = packet.get("build_identity")
    if not isinstance(build_identity, dict):
        raise ValueError("Talent transport packet build_identity must be an object.")
    transport_forms = packet.get("transport_forms")
    if not isinstance(transport_forms, dict):
        raise ValueError("Talent transport packet transport_forms must be an object.")
    raw_evidence = packet.get("raw_evidence")
    if not isinstance(raw_evidence, dict):
        raise ValueError("Talent transport packet raw_evidence must be an object.")
    validation = packet.get("validation")
    if not isinstance(validation, dict):
        raise ValueError("Talent transport packet validation must be an object.")
    scope = packet.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("Talent transport packet scope must be an object.")
    return transport_status, build_identity, transport_forms, raw_evidence, validation


def _validate_talent_transport_exact_forms(
    transport_forms: dict[str, Any],
    *,
    build_identity: dict[str, Any],
) -> dict[str, str | None] | None:
    wowhead_ref = transport_forms.get("wowhead_talent_calc_url")
    parsed_wowhead_ref: dict[str, str | None] | None = None
    if wowhead_ref is not None:
        if not (isinstance(wowhead_ref, str) and wowhead_ref.strip()):
            raise ValueError("Talent transport packet wowhead_talent_calc_url must be a non-empty string.")
        parsed_wowhead_ref = parse_wowhead_talent_calc_ref(wowhead_ref)
        if parsed_wowhead_ref is None or parsed_wowhead_ref["build_code"] is None:
            raise ValueError("Talent transport packet wowhead_talent_calc_url must include an explicit build code.")
        packet_actor_class, packet_spec = _packet_class_spec_identity(build_identity)
        if (
            packet_actor_class is not None
            and packet_actor_class != parsed_wowhead_ref["actor_class"]
        ) or (
            packet_spec is not None
            and packet_spec != parsed_wowhead_ref["spec"]
        ):
            raise ValueError(
                "Talent transport packet wowhead_talent_calc_url must match build_identity.class_spec_identity.identity."
            )

    wow_export = transport_forms.get("wow_talent_export")
    if wow_export is not None and not (isinstance(wow_export, str) and wow_export.strip()):
        raise ValueError("Talent transport packet wow_talent_export must be a non-empty string.")
    if (
        parsed_wowhead_ref is not None
        and isinstance(wow_export, str)
        and wow_export.strip()
        and wow_export.strip() != parsed_wowhead_ref["build_code"]
    ):
        raise ValueError(
            "Talent transport packet exact transport forms must agree when both "
            "wowhead_talent_calc_url and wow_talent_export are present."
        )
    return parsed_wowhead_ref


def _validate_talent_transport_simc_split_forms(
    transport_forms: dict[str, Any],
    *,
    build_identity: dict[str, Any],
    validation: dict[str, Any],
    parsed_wowhead_ref: dict[str, str | None] | None,
) -> None:
    split = transport_forms.get("simc_split_talents")
    if split is None:
        return
    if not isinstance(split, dict):
        raise ValueError("Talent transport packet simc_split_talents must be an object.")
    present: list[str] = []
    for key in ("class_talents", "spec_talents", "hero_talents"):
        value = split.get(key)
        if value is None:
            continue
        if not (isinstance(value, str) and value.strip()):
            raise ValueError(
                f"Talent transport packet simc_split_talents.{key} must be a non-empty string when present."
            )
        present.append(value)
    if not present:
        raise ValueError(
            "Talent transport packet simc_split_talents must include at least one non-empty class/spec/hero string."
        )
    wow_export = transport_forms.get("wow_talent_export")
    if parsed_wowhead_ref is not None or (isinstance(wow_export, str) and wow_export.strip()):
        raise ValueError(
            "Talent transport packet must not mix exact transport forms with simc_split_talents."
        )
    if validation.get("status") != "validated":
        return
    packet_actor_class, packet_spec = _packet_class_spec_identity(build_identity)
    if packet_actor_class is None or packet_spec is None:
        raise ValueError(
            "Validated simc_split_talents packets must include build_identity.class_spec_identity.identity."
        )
    validated_actor_class, validated_spec = _validated_class_spec_identity(validation)
    if validated_actor_class is None or validated_spec is None:
        raise ValueError(
            "Validated simc_split_talents packets must include validation.actor_class and validation.spec."
        )
    if (packet_actor_class, packet_spec) != (validated_actor_class, validated_spec):
        raise ValueError(
            "Validated simc_split_talents packets must keep build_identity.class_spec_identity.identity aligned "
            "with validation.actor_class/spec."
        )


def validate_talent_transport_packet(packet: Any) -> dict[str, Any]:
    if not isinstance(packet, dict):
        raise ValueError("Talent transport packet must be a JSON object.")
    transport_status, build_identity, transport_forms, raw_evidence, validation = _talent_transport_envelope(packet)
    parsed_wowhead_ref = _validate_talent_transport_exact_forms(transport_forms, build_identity=build_identity)
    _validate_talent_transport_simc_split_forms(
        transport_forms,
        build_identity=build_identity,
        validation=validation,
        parsed_wowhead_ref=parsed_wowhead_ref,
    )

    expected_status = _talent_transport_status(
        transport_forms=transport_forms,
        raw_evidence=raw_evidence,
        validation=validation,
    )
    if transport_status == "raw_only" and not _has_usable_raw_talent_evidence(raw_evidence):
        raise ValueError("Talent transport packet raw_only status requires usable raw talent_tree_entries evidence.")
    if transport_status != expected_status:
        raise ValueError(
            f"Talent transport packet transport_status {transport_status!r} does not match packet contents; expected {expected_status!r}."
        )
    return packet


def _talent_transport_status(
    *,
    transport_forms: dict[str, Any],
    raw_evidence: dict[str, Any],
    validation: dict[str, Any],
) -> TalentTransportStatus:
    if transport_forms.get("wowhead_talent_calc_url") or transport_forms.get("wow_talent_export"):
        return "exact"
    if transport_forms.get("simc_split_talents") and validation.get("status") == "validated":
        return "validated"
    if _has_usable_raw_talent_evidence(raw_evidence):
        return "raw_only"
    return "unknown"
