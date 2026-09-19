from __future__ import annotations

import pytest
from warcraft_core.output import (
    OutputProjectionError,
    compact_value,
    filter_payload_fields,
    normalize_field_paths,
    resolve_output_options,
    truncate_string,
)


def test_normalize_field_paths_splits_commas_and_dedupes() -> None:
    assert normalize_field_paths(["query,count", "count", "results"]) == ("query", "count", "results")


def test_compact_value_truncates_nested_strings() -> None:
    payload = {"tooltip": {"html": "x" * 400}}
    compacted = compact_value(payload, max_chars=100)
    assert len(compacted["tooltip"]["html"]) == 100
    assert compacted["tooltip"]["html"].endswith("...")


def test_truncate_string_respects_limit() -> None:
    assert truncate_string("abcdef", max_chars=6) == "abcdef"
    assert truncate_string("abcdefgh", max_chars=6) == "abc..."


def test_filter_payload_fields_projects_nested_paths() -> None:
    payload = {"entity": {"name": "Foo"}, "tooltip": {"quality": 4, "summary": "bar"}}
    projected = filter_payload_fields(payload, fields=("entity.name", "tooltip.quality"))
    assert projected == {"entity": {"name": "Foo"}, "tooltip": {"quality": 4}}


def test_filter_payload_fields_strict_raises_for_missing_paths() -> None:
    payload = {"entity": {"name": "Foo"}}
    with pytest.raises(OutputProjectionError) as exc_info:
        filter_payload_fields(payload, fields=("entity.name", "tooltip.quality"), strict=True)
    assert exc_info.value.missing_fields == ("tooltip.quality",)


def test_filter_payload_fields_reports_missing_paths_instead_of_dropping_them() -> None:
    """Without --fields-strict a typo must still be visible, not an unexplained thin projection."""
    payload = {"entity": {"name": "Foo"}}
    projected = filter_payload_fields(payload, fields=("entity.name", "tooltip.quality"))
    assert projected == {"entity": {"name": "Foo"}, "fields_missing": ["tooltip.quality"]}


def test_filter_payload_fields_reports_a_projection_that_matched_nothing() -> None:
    projected = filter_payload_fields({"entity": {"name": "Foo"}}, fields=("nope",))
    assert projected == {"fields_missing": ["nope"]}


def test_resolve_output_options_human_profile_enables_pretty() -> None:
    options = resolve_output_options(profile="human")
    assert options.pretty is True


@pytest.mark.parametrize("profile", ["verbose", "debug"])
def test_resolve_output_options_rejects_a_profile_outside_the_presets(profile: str) -> None:
    """The message enumerates every accepted preset, so it must not name one the CLI no longer has."""
    with pytest.raises(ValueError, match=r"^--profile must be one of: agent, human$"):
        resolve_output_options(profile=profile)
