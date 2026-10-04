from __future__ import annotations

from warcraft_core.identity import (
    WowheadTalentCalcRef,
    WowheadTalentCalcRefError,
    ability_identity_payload,
    build_identity_payload,
    build_reference_payload,
    build_reference_transport_packet_payload,
    class_spec_identity_payload,
    encounter_identity_payload,
    normalize_actor_class,
    normalize_spec_name,
    parse_wowhead_talent_calc,
    parse_wowhead_talent_calc_ref,
    refresh_talent_transport_packet,
    report_actor_identity_payload,
    talent_transport_packet_payload,
    unique_spec_class,
    validate_talent_transport_packet,
)


def test_unique_spec_class_names_the_one_class_a_spec_word_belongs_to() -> None:
    assert unique_spec_class("shadow") == "priest"
    assert unique_spec_class("feral") == "druid"
    # Shared by two classes, or no spec at all.
    assert [unique_spec_class(word) for word in ("frost", "holy", "restoration", "druid")] == [None, None, None, None]


def test_class_spec_identity_distinguishes_normalized_inferred_and_ambiguous() -> None:
    normalized = class_spec_identity_payload(actor_class="Demon Hunter", spec="Havoc", canonical=False)
    assert normalized["status"] == "normalized"
    assert normalized["identity"] == {"actor_class": "demonhunter", "spec": "havoc"}

    inferred = class_spec_identity_payload(
        actor_class="Demon Hunter",
        spec="Havoc",
        inferred=True,
        confidence="high",
        source="simc_probe",
    )
    assert inferred["status"] == "inferred"
    assert inferred["confidence"] == "high"

    ambiguous = class_spec_identity_payload(
        actor_class="Monk",
        spec=None,
        candidates=[("Monk", "Mistweaver"), ("Demon Hunter", "Havoc")],
        confidence="low",
    )
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["candidate_count"] == 2


def test_encounter_and_ability_identity_only_become_canonical_with_stable_ids() -> None:
    encounter = encounter_identity_payload(encounter_id=3012, journal_id=9001, name="Dimensius, the All-Devouring")
    assert encounter["status"] == "canonical"
    assert encounter["identity"]["normalized_name"] == "dimensius-the-all-devouring"

    ability = ability_identity_payload(name="Avenging Wrath")
    assert ability["status"] == "normalized"
    assert ability["identity"]["normalized_name"] == "avenging_wrath"

    canonical_ability = ability_identity_payload(spell_id=31884, name="Avenging Wrath")
    assert canonical_ability["status"] == "canonical"


def test_encounter_identity_emit_shape_covers_normalized_and_unknown() -> None:
    normalized = encounter_identity_payload(
        encounter_id=None,
        name="Dimensius, the All-Devouring",
        zone_id=44,
        provider="warcraftlogs",
        source="report_encounter",
    )
    assert normalized["kind"] == "encounter_identity"
    assert normalized["status"] == "normalized"
    assert normalized["identity"] == {
        "encounter_id": None,
        "journal_id": None,
        "zone_id": 44,
        "normalized_name": "dimensius-the-all-devouring",
    }
    assert normalized["source"] == {"provider": "warcraftlogs", "source": "report_encounter"}
    assert normalized["notes"] == []

    unknown = encounter_identity_payload(encounter_id=None)
    assert unknown["kind"] == "encounter_identity"
    assert unknown["status"] == "unknown"
    assert unknown["identity"] == {"encounter_id": None, "journal_id": None, "zone_id": None, "normalized_name": None}


def test_ability_identity_emit_shape_covers_game_id_and_unknown() -> None:
    by_game_id = ability_identity_payload(game_id=12345, name="Avenging Wrath")
    assert by_game_id["kind"] == "ability_identity"
    assert by_game_id["status"] == "canonical"
    assert by_game_id["identity"] == {"spell_id": None, "game_id": 12345, "normalized_name": "avenging_wrath"}

    unknown = ability_identity_payload()
    assert unknown["kind"] == "ability_identity"
    assert unknown["status"] == "unknown"
    assert unknown["identity"] == {"spell_id": None, "game_id": None, "normalized_name": None}


def test_report_actor_identity_is_only_canonical_inside_local_report_scope() -> None:
    scoped = report_actor_identity_payload(
        report_code="abcd1234",
        fight_id=3,
        actor_id=42,
        name="Auropower",
        actor_class="Paladin",
        spec="Retribution",
    )
    assert scoped["status"] == "canonical"
    assert scoped["identity"]["local_key"] == "abcd1234:3:42"

    unscoped = report_actor_identity_payload(
        report_code=None,
        fight_id=None,
        actor_id=None,
        name="Auropower",
        actor_class="Paladin",
        spec="Retribution",
    )
    assert unscoped["status"] == "normalized"


def test_build_identity_contract_never_claims_canonical_status() -> None:
    resolved = build_identity_payload(
        actor_class="Demon Hunter",
        spec="Havoc",
        confidence="high",
        source="simc_probe",
        candidates=[("Demon Hunter", "Havoc")],
        source_notes=["identified by SimC probe"],
    )
    assert resolved["status"] == "inferred"
    assert resolved["canonical"] is False
    assert resolved["class_spec_identity"]["status"] == "inferred"

    ambiguous = build_identity_payload(
        actor_class=None,
        spec=None,
        confidence="low",
        source="simc_probe",
        candidates=[("Monk", "Mistweaver"), ("Demon Hunter", "Havoc")],
    )
    assert ambiguous["status"] == "ambiguous"


def test_actor_class_and_spec_normalizers_stay_small_and_predictable() -> None:
    assert normalize_actor_class("Demon Hunter") == "demonhunter"
    assert normalize_spec_name("Beast Mastery") == "beast_mastery"
    # Warcraft Logs spells the spec BeastMastery; every source has to reach the same identity.
    assert normalize_spec_name("BeastMastery") == normalize_spec_name("beast-mastery") == "beast_mastery"
    assert normalize_spec_name("HUNTER_BEAST_MASTERY") == "hunter_beast_mastery"


def test_parse_wowhead_talent_calc_ref_supports_prefixed_paths_and_relative_urls() -> None:
    payload = parse_wowhead_talent_calc_ref("/cata/talent-calc/hunter/beast-mastery/XYZ987")
    assert payload == {
        "actor_class": "hunter",
        "spec": "beast_mastery",
        "build_code": "XYZ987",
        "reference_url": "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987",
        "source_kind": "wowhead_talent_calc_url",
    }


def test_parse_wowhead_talent_calc_ref_supports_scheme_less_wowhead_urls() -> None:
    payload = parse_wowhead_talent_calc_ref("wowhead.com/talent-calc/druid/balance/ABC123")
    assert payload == {
        "actor_class": "druid",
        "spec": "balance",
        "build_code": "ABC123",
        "reference_url": "https://wowhead.com/talent-calc/druid/balance/ABC123",
        "source_kind": "wowhead_talent_calc_url",
    }
    www_payload = parse_wowhead_talent_calc_ref("www.wowhead.com/talent-calc/druid/balance/ABC123")
    assert www_payload == {
        "actor_class": "druid",
        "spec": "balance",
        "build_code": "ABC123",
        "reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
        "source_kind": "wowhead_talent_calc_url",
    }


def test_parse_wowhead_talent_calc_ref_rejects_non_wowhead_domains() -> None:
    assert parse_wowhead_talent_calc_ref("https://notwowhead.com/talent-calc/druid/balance/ABC123") is None
    assert parse_wowhead_talent_calc_ref("//evil.com/talent-calc/druid/balance/ABC123") is None


def test_build_references_preserve_protocol_relative_wowhead_urls() -> None:
    parsed = parse_wowhead_talent_calc_ref("//www.wowhead.com/talent-calc/druid/balance/ABC123")
    assert parsed is not None
    assert parsed["reference_url"] == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"


def test_parse_wowhead_talent_calc_ref_rejects_buried_talent_calc_segments() -> None:
    assert parse_wowhead_talent_calc_ref("foo/talent-calc/druid/balance/ABC123") is None
    assert parse_wowhead_talent_calc_ref("https://www.wowhead.com/items/talent-calc/druid/balance/ABC123") is None


def test_parse_wowhead_talent_calc_ref_only_reads_a_real_spec_from_the_spec_slot() -> None:
    # Classic-era calculators put the build code where retail puts the spec; reading it as a spec
    # made simc say a URL that carries a build code had none.
    assert parse_wowhead_talent_calc_ref("https://www.wowhead.com/classic/talent-calc/warrior/30305001302-05050005525010051") is None
    assert parse_wowhead_talent_calc_ref("https://www.wowhead.com/talent-calc/warrior/notaspec/ABC123") is None
    assert parse_wowhead_talent_calc_ref("https://www.wowhead.com/talent-calc/warrior/balance/ABC123") is None
    parsed = parse_wowhead_talent_calc_ref("https://www.wowhead.com/talent-calc/warrior/fury/ABC123")
    assert parsed is not None and (parsed["actor_class"], parsed["spec"]) == ("warrior", "fury")


def test_parse_wowhead_talent_calc_ref_rejects_empty_or_extra_segments() -> None:
    assert parse_wowhead_talent_calc_ref("https://www.wowhead.com/talent-calc/druid//balance/ABC123") is None
    assert parse_wowhead_talent_calc_ref("https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra") is None


def test_build_references_preserve_normalized_spec_aliases() -> None:
    for actor_class, spec_slug, expected in (
        ("druid", "Balance", "balance"),
        ("hunter", "beast_mastery", "beast_mastery"),
        ("hunter", "BeastMastery", "beast_mastery"),
    ):
        ref = f"https://www.wowhead.com/talent-calc/{actor_class}/{spec_slug}/ABC123"
        parsed = parse_wowhead_talent_calc_ref(ref)
        assert parsed is not None
        assert parsed["spec"] == expected
        assert parsed["reference_url"] == ref
        assert isinstance(parse_wowhead_talent_calc(ref), WowheadTalentCalcRefError)


def test_parse_wowhead_talent_calc_reads_each_calculators_path_layout() -> None:
    classic = parse_wowhead_talent_calc("https://www.wowhead.com/classic/talent-calc/warrior/30305001302-05050005525010051/1aab0cC")
    assert isinstance(classic, WowheadTalentCalcRef)
    assert (classic.expansion, classic.actor_class, classic.spec, classic.build_code, classic.extra_segment) == (
        "classic", "warrior", None, "30305001302-05050005525010051", "1aab0cC",
    )
    mop = parse_wowhead_talent_calc("https://www.wowhead.com/mop-classic/talent-calc/druid/balance/323222/AA4FGtB4TpcC4ToOD4F3gE4TpX")
    assert isinstance(mop, WowheadTalentCalcRef)
    assert (mop.spec, mop.build_code, mop.extra_segment) == ("balance", "323222", "AA4FGtB4TpcC4ToOD4F3gE4TpX")
    forever = parse_wowhead_talent_calc("forever/death-knight/v205_t0")
    assert isinstance(forever, WowheadTalentCalcRef)
    assert (forever.expansion, forever.actor_class, forever.explicit_path) == ("forever", "deathknight", False)
    assert forever.reference_url == "https://www.wowhead.com/forever/talent-calc/death-knight/v205_t0"
    shorthand = parse_wowhead_talent_calc("druid/balance/ABC123", default_expansion="wotlk")
    assert isinstance(shorthand, WowheadTalentCalcRef)
    assert shorthand.reference_url == "https://www.wowhead.com/wotlk/talent-calc/druid/balance/ABC123"


def test_parse_wowhead_talent_calc_says_why_and_whether_the_ref_aims_at_a_calculator() -> None:
    """A malformed calculator ref still aims at one, so the wrapper routes it to Wowhead for this message."""
    cases = {
        "druid//balance/ABC123": ("talent-calc reference must not include empty path segments.", True),
        "https://www.wowhead.com/items/talent-calc/druid": ("Talent calculator URL must point to /talent-calc.", True),
        "paladin/frost/ABC": ("Talent calculator spec 'frost' is not a paladin spec.", True),
        "https://www.wowhead.com/talent-calc/warrior/3030-05/0ab": ("Talent calculator spec segment '3030-05' is not a spec name.", True),
        "https://notwowhead.com/talent-calc/druid/balance": ("talent-calc URL must point to wowhead.com.", False),
        "tmp/foo": (
            "Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>] or "
            "/talent-calc/<class>/<build-code> with a WoW class.",
            False,
        ),
    }
    for ref, expected in cases.items():
        assert parse_wowhead_talent_calc(ref) == WowheadTalentCalcRefError(*expected), ref


def test_parse_wowhead_talent_calc_ref_keeps_to_retail_spec_paths() -> None:
    """Build references and SimC read only ``/talent-calc/<class>/<spec>[/<code>]`` on a known site."""
    for ref in (
        "https://www.wowhead.com/mop-classic/talent-calc/druid/balance/323222/AA4FGtB4TpcC4ToOD4F3gE4TpX",
        "https://www.wowhead.com/forever/talent-calc/druid/balance/ABC123",
        "druid/balance/ABC123",
    ):
        assert parse_wowhead_talent_calc_ref(ref) is None, ref


def test_build_reference_payload_only_accepts_explicit_wowhead_talent_calc_urls() -> None:
    payload = build_reference_payload(
        ref="https://www.wowhead.com/talent-calc/druid/balance/ABC123",
        provider="method",
        source="guide_embedded_link",
        source_url="https://www.method.gg/guides/balance-druid",
        label="Raid Build",
        notes=["embedded guide link"],
    )
    assert payload is not None
    assert payload["reference_type"] == "wowhead_talent_calc_url"
    assert payload["build_code"] == "ABC123"
    assert payload["build_identity"]["status"] == "inferred"
    assert payload["build_identity"]["class_spec_identity"]["identity"] == {"actor_class": "druid", "spec": "balance"}
    assert build_reference_payload(ref="https://www.wowhead.com/spell=116670", provider="method", source="guide_embedded_link") is None


def test_build_reference_transport_packet_payload_marks_explicit_refs_as_exact() -> None:
    payload = build_reference_transport_packet_payload(
        ref="https://www.wowhead.com/talent-calc/druid/balance/ABC123",
        provider="warcraft",
        source="guide_build_reference_handoff",
        source_url="https://www.method.gg/guides/balance-druid",
        source_urls=["https://www.method.gg/guides/balance-druid"],
        label="Raid Build",
        notes=["explicit guide build ref"],
        scope={"type": "guide_build_reference_handoff"},
    )
    assert payload is not None
    assert payload["transport_status"] == "exact"
    assert payload["transport_forms"]["wowhead_talent_calc_url"] == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    assert payload["raw_evidence"]["reference_type"] == "wowhead_talent_calc_url"
    assert payload["scope"] == {"type": "guide_build_reference_handoff"}


def test_build_reference_transport_packet_payload_requires_build_code() -> None:
    payload = build_reference_transport_packet_payload(
        ref="https://www.wowhead.com/talent-calc/druid/balance",
        provider="warcraft",
        source="guide_build_reference_handoff",
    )
    assert payload is None


def test_talent_transport_packet_distinguishes_exact_validated_and_raw_only() -> None:
    exact = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="wowhead_talent_calc_url",
        transport_forms={"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
        source_notes=["explicit wowhead ref"],
    )
    assert exact["transport_status"] == "exact"
    assert exact["build_identity"]["status"] == "inferred"

    validated = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        transport_forms={
            "simc_split_talents": {
                "class_talents": "103324:1",
                "spec_talents": "109839:1",
                "hero_talents": "117176:1",
            }
        },
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
        validation={"status": "validated", "actor_class": "druid", "spec": "balance"},
    )
    assert validated["transport_status"] == "validated"

    raw_only = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="medium",
        source="warcraftlogs_talent_tree",
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
    )
    assert raw_only["transport_status"] == "raw_only"


def test_refresh_talent_transport_packet_preserves_scope_and_upgrades_status() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        provider="warcraftlogs",
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
        validation={"status": "not_validated"},
        scope={"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
    )

    refreshed = refresh_talent_transport_packet(
        packet,
        transport_forms={
            "simc_split_talents": {
                "class_talents": "103324:1",
                "spec_talents": "109839:1",
            }
        },
        validation={
            "status": "validated",
            "source": "simc_trait_data_round_trip",
            "actor_class": "druid",
            "spec": "balance",
        },
    )

    assert refreshed["transport_status"] == "validated"
    assert refreshed["scope"] == packet["scope"]
    assert refreshed["source"] == packet["source"]
    assert refreshed["raw_evidence"] == packet["raw_evidence"]


def test_validate_talent_transport_packet_accepts_exact_packets() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="wowhead_talent_calc_url",
        transport_forms={"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
    )

    assert validate_talent_transport_packet(packet) == packet


def test_validate_talent_transport_packet_rejects_mismatched_status() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="wowhead_talent_calc_url",
        transport_forms={"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
    )
    packet["transport_status"] = "validated"

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "does not match packet contents" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_mixed_exact_and_split_forms() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="wowhead_talent_calc_url",
        transport_forms={"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
    )
    packet["transport_forms"]["simc_split_talents"] = {"class_talents": "103324:1"}

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "must not mix exact transport forms with simc_split_talents" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_empty_simc_split_forms() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
    )
    packet["transport_forms"] = {"simc_split_talents": {"class_talents": "  "}}

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "simc_split_talents" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_exact_wowhead_ref_without_build_code() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
    )
    packet["transport_forms"] = {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance"}
    packet["transport_status"] = "exact"

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "build code" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_exact_identity_mismatch() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="wowhead_talent_calc_url",
        transport_forms={"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
    )
    packet["build_identity"]["class_spec_identity"]["identity"] = {
        "actor_class": "hunter",
        "spec": "beast_mastery",
    }

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "must match build_identity.class_spec_identity.identity" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_partial_exact_identity_mismatch() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="wowhead_talent_calc_url",
        transport_forms={"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
    )
    packet["build_identity"]["class_spec_identity"]["identity"] = {
        "actor_class": "hunter",
    }

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "must match build_identity.class_spec_identity.identity" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_conflicting_exact_transport_forms() -> None:
    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "exact",
        "build_identity": {
            "class_spec_identity": {
                "identity": {"actor_class": "druid", "spec": "balance"},
            }
        },
        "transport_forms": {
            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
            "wow_talent_export": "XYZ789",
        },
        "raw_evidence": {},
        "validation": {},
        "scope": {},
    }

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "exact transport forms must agree" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_raw_only_without_usable_rows() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        raw_evidence={"source_contract": "warcraftlogs_talent_tree"},
    )
    assert packet["transport_status"] == "unknown"
    packet["transport_status"] = "raw_only"

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "raw_only status requires usable raw talent_tree_entries evidence" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_incomplete_raw_only_rows() -> None:
    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "raw_only",
        "build_identity": {
            "class_spec_identity": {
                "identity": {"actor_class": "druid", "spec": "balance"},
            }
        },
        "transport_forms": {},
        "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "rank": 1}]},
        "validation": {"status": "not_validated"},
        "scope": {},
    }

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "raw_only status requires usable raw talent_tree_entries evidence" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_mixed_valid_and_invalid_raw_only_rows() -> None:
    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "raw_only",
        "build_identity": {
            "class_spec_identity": {
                "identity": {"actor_class": "druid", "spec": "balance"},
            }
        },
        "transport_forms": {},
        "raw_evidence": {
            "talent_tree_entries": [
                {"entry": 103324, "node_id": 82244, "rank": 1},
                {"entry": 109839, "rank": 1},
            ]
        },
        "validation": {"status": "not_validated"},
        "scope": {},
    }

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "raw_only status requires usable raw talent_tree_entries evidence" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_boolean_raw_only_rows() -> None:
    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "raw_only",
        "build_identity": {
            "class_spec_identity": {
                "identity": {"actor_class": "druid", "spec": "balance"},
            }
        },
        "transport_forms": {},
        "raw_evidence": {"talent_tree_entries": [{"entry": True, "node_id": 82244, "rank": 1}]},
        "validation": {"status": "not_validated"},
        "scope": {},
    }

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "raw_only status requires usable raw talent_tree_entries evidence" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_refresh_talent_transport_packet_rejects_invalid_transport_forms() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
    )

    try:
        refresh_talent_transport_packet(
            packet,
            transport_forms={"simc_split_talents": []},
            validation={"status": "validated"},
        )
    except ValueError as exc:
        assert "simc_split_talents" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_invalid_split_field_types() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
    )
    packet["transport_forms"] = {
        "simc_split_talents": {
            "class_talents": "103324:1",
            "spec_talents": 42,
        }
    }
    packet["transport_status"] = "validated"
    packet["validation"] = {"status": "validated", "actor_class": "druid", "spec": "balance"}

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "simc_split_talents.spec_talents" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_validated_split_without_class_spec_identity() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
    )
    packet["build_identity"] = {}
    packet["transport_forms"] = {
        "simc_split_talents": {
            "class_talents": "103324:1",
        }
    }
    packet["transport_status"] = "validated"
    packet["validation"] = {"status": "validated", "actor_class": "druid", "spec": "balance"}

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "Validated simc_split_talents packets must include build_identity.class_spec_identity.identity" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_validated_split_without_validation_identity() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
    )
    packet["transport_forms"] = {
        "simc_split_talents": {
            "class_talents": "103324:1",
        }
    }
    packet["transport_status"] = "validated"
    packet["validation"] = {"status": "validated"}

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "validation.actor_class and validation.spec" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_talent_transport_packet_rejects_validated_split_identity_mismatch() -> None:
    packet = talent_transport_packet_payload(
        actor_class="Druid",
        spec="Balance",
        confidence="high",
        source="warcraftlogs_talent_tree",
        raw_evidence={"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
    )
    packet["transport_forms"] = {
        "simc_split_talents": {
            "class_talents": "103324:1",
        }
    }
    packet["transport_status"] = "validated"
    packet["validation"] = {"status": "validated", "actor_class": "priest", "spec": "shadow"}

    try:
        validate_talent_transport_packet(packet)
    except ValueError as exc:
        assert "aligned with validation.actor_class/spec" in str(exc)
    else:
        raise AssertionError("expected ValueError")
