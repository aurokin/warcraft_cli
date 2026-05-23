from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urljoin, urlparse, urlunparse

from warcraft_core.wow_normalization import normalized_text

IdentityStatus = Literal["unknown", "normalized", "canonical", "inferred", "ambiguous"]
IdentityConfidence = Literal["none", "low", "medium", "high"]
TalentTransportStatus = Literal["unknown", "raw_only", "validated", "exact"]
WOWHEAD_TALENT_CALC_SEGMENT = "talent-calc"
WOWHEAD_EXPANSION_PREFIXES = frozenset({"classic", "tbc", "wotlk", "cata", "mop-classic", "ptr", "beta", "classic-ptr"})
WOW_CLASS_SLUGS = frozenset(
    {
        "deathknight",
        "demonhunter",
        "druid",
        "evoker",
        "hunter",
        "mage",
        "monk",
        "paladin",
        "priest",
        "rogue",
        "shaman",
        "warlock",
        "warrior",
    }
)


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


def _is_transport_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _split_reference_path_parts(path: str) -> list[str] | None:
    parts = path.split("/")
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if any(part == "" for part in parts):
        return None
    return parts


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


def parse_wowhead_talent_calc_ref(ref: str) -> dict[str, str | None] | None:
    candidate = _clean_text(ref)
    if candidate is None:
        return None
    lowered = candidate.lower()
    if "://" not in candidate and (lowered.startswith("wowhead.com/") or lowered.startswith("www.wowhead.com/")):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        if not _is_wowhead_hostname(parsed.hostname):
            return None
        reference_url = urlunparse(parsed._replace(query="", fragment=""))
    else:
        reference_url = urljoin("https://www.wowhead.com", candidate if candidate.startswith("/") else f"/{candidate}")
        parsed = urlparse(reference_url)
        if not _is_wowhead_hostname(parsed.hostname):
            return None
    path_parts = _split_reference_path_parts(parsed.path)
    if path_parts is None:
        return None
    if path_parts and path_parts[0] in WOWHEAD_EXPANSION_PREFIXES:
        path_parts = path_parts[1:]
    if not path_parts or path_parts[0] != WOWHEAD_TALENT_CALC_SEGMENT:
        return None
    if len(path_parts) not in {3, 4}:
        return None
    actor_class = normalize_actor_class(path_parts[1])
    spec = normalize_spec_name(path_parts[2])
    build_code = path_parts[3] if len(path_parts) > 3 else None
    if not actor_class or actor_class not in WOW_CLASS_SLUGS or not spec:
        return None
    return {
        "actor_class": actor_class,
        "spec": spec,
        "build_code": _clean_text(build_code),
        "reference_url": reference_url,
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
    return all(_is_transport_int(row.get(key)) for key in ("entry", "node_id", "rank"))


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
    candidates: list[tuple[str | None, str | None]] | None = None,
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
            candidates=candidates,
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
    for key in ("class_talents", "spec_talents", "hero_talents"):
        if key not in split or split.get(key) is None:
            continue
        value = split.get(key)
        if not (isinstance(value, str) and value.strip()):
            raise ValueError(
                f"Talent transport packet simc_split_talents.{key} must be a non-empty string when present."
            )
    if not any(
        isinstance(value := split.get(key), str) and bool(value.strip())
        for key in ("class_talents", "spec_talents", "hero_talents")
    ):
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
