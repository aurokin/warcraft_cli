from __future__ import annotations

import json
from pathlib import Path

import pytest
from warcraft_core.identity import normalize_actor_class, normalize_spec_name
from warcraft_core.wow_specs import (
    _SPECS_BY_SPELLING,
    CLASS_SPEC_ALIASES,
    WOW_CLASS_NAMES,
    WOW_SPECS,
    close_specs,
    lookup_class,
    lookup_spec,
    raiderio_class_slug,
    warcraftlogs_class_slug,
)

SHARED_SPEC_WORDS = {"frost", "holy", "protection", "restoration"}


@pytest.mark.parametrize("spec", WOW_SPECS, ids=lambda spec: spec.lorrgs_slug)
def test_every_providers_spelling_of_a_spec_looks_up_that_spec(spec) -> None:
    class_name = WOW_CLASS_NAMES[spec.class_key]
    spellings = [
        spec.lorrgs_slug,
        spec.raiderio_slug,
        warcraftlogs_class_slug(spec.class_key) + spec.warcraftlogs_spec_slug,
        f"{spec.raiderio_spec_slug}-{raiderio_class_slug(spec.class_key)}",
        f"{spec.name} {class_name}",
        f"{class_name} {spec.name}",
        f"{spec.class_key}_{spec.key}",
    ]
    assert {spelling: lookup_spec(spelling) for spelling in spellings} == dict.fromkeys(spellings, spec)
    # A bare spec names its spec when no other class has one by that name, and with its class as a hint always.
    assert lookup_spec(spec.warcraftlogs_spec_slug, class_hint=warcraftlogs_class_slug(spec.class_key)) == spec
    assert lookup_spec(spec.raiderio_spec_slug) == (None if spec.key in SHARED_SPEC_WORDS else spec)


def test_lorrgs_slugs_and_spec_ids_match_the_captured_lorrgs_spec_list() -> None:
    captured = json.loads((Path(__file__).parent / "fixtures" / "lorrgs" / "specs.json").read_text(encoding="utf-8"))
    lorrgs = {row["full_name_slug"]: row["id"] for row in captured["specs"] if not row["full_name_slug"].startswith("other-")}
    assert {spec.lorrgs_slug: spec.spec_id for spec in WOW_SPECS} == lorrgs


def test_spec_keys_match_the_identity_normalizers() -> None:
    assert all(normalize_spec_name(spec.name) == spec.key for spec in WOW_SPECS)
    assert all(normalize_actor_class(name) == key for key, name in WOW_CLASS_NAMES.items())


def test_only_the_shared_bare_spec_words_have_more_than_one_spec() -> None:
    shared = {spelling: specs for spelling, specs in _SPECS_BY_SPELLING.items() if len(specs) > 1}

    assert set(shared) == SHARED_SPEC_WORDS
    assert all(spec.key == spelling for spelling, specs in shared.items() for spec in specs)
    assert [lookup_spec(word) for word in sorted(SHARED_SPEC_WORDS)] == [None] * 4
    assert len({spec.spec_id for spec in WOW_SPECS}) == len(WOW_SPECS)


def test_shorthand_never_shadows_a_class_or_spec_word() -> None:
    words = set(WOW_CLASS_NAMES) | {word for spec in WOW_SPECS for word in spec.key.split("_")}
    assert not words & set(CLASS_SPEC_ALIASES)


@pytest.mark.parametrize(
    ("text", "lorrgs_slug"),
    [("bm hunter", "hunter-beastmastery"), ("ret pally", "paladin-retribution"), ("bdk", "deathknight-blood"), ("boomkin", "druid-balance")],
)
def test_shorthand_looks_up_its_spec(text: str, lorrgs_slug: str) -> None:
    spec = lookup_spec(text)
    assert spec is not None and spec.lorrgs_slug == lorrgs_slug


def test_a_class_hint_picks_a_shared_spec_but_never_overrides_a_named_class() -> None:
    frost_dk = lookup_spec("frost", class_hint="Death Knight")
    mage_frost = lookup_spec("mage-frost", class_hint="dk")
    assert (frost_dk and frost_dk.lorrgs_slug, mage_frost and mage_frost.lorrgs_slug) == ("deathknight-frost", "mage-frost")


@pytest.mark.parametrize("text", ["deathknight", "death-knight", "Death Knight", "DeathKnight", "dk"])
def test_every_class_spelling_looks_up_the_class(text: str) -> None:
    assert lookup_class(text) == "deathknight"


def test_close_specs_suggest_the_specs_a_miss_was_near() -> None:
    assert [spec.lorrgs_slug for spec in close_specs("frost")] == ["deathknight-frost", "mage-frost"]
    assert close_specs("druid-balanse")[0].lorrgs_slug == "druid-balance"
    assert close_specs("other-trinkets") == []
