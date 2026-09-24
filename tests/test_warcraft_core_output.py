from __future__ import annotations

import pytest
from warcraft_core.output import (
    OutputProjectionError,
    compact_value,
    filter_payload_fields,
    normalize_field_paths,
    resolve_output_options,
)


def test_normalize_field_paths_splits_commas_and_dedupes() -> None:
    assert normalize_field_paths(["query,count", "count", "results"]) == ("query", "count", "results")


def test_compact_value_truncates_nested_prose_and_records_each_path() -> None:
    payload = {"tooltip": {"html": "<b>x</b> " * 50}, "rows": [{"text": "one two"}, {"text": "word " * 30}]}
    cut: list[str] = []
    compacted = compact_value(payload, max_chars=100, cut=cut)
    assert len(compacted["tooltip"]["html"]) == 100
    assert compacted["tooltip"]["html"].endswith("...")
    assert compacted["rows"] == [{"text": "one two"}, {"text": ("word " * 30)[:97] + "..."}]
    assert cut == ["tooltip.html", "rows.1.text"]


def test_compact_value_never_cuts_values_another_tool_consumes() -> None:
    """A cut talent string, URL, export code or command is unusable, so --compact leaves them whole."""
    payload = {
        "transport_forms": {"simc_split_talents": {"class_talents": "/".join(f"{103000 + n}:1" for n in range(60))}},
        "url": "https://www.wowhead.com/talent-calc/" + "A" * 300,
        "next_command": "warcraftlogs report-player-talents " + "x " * 200,
        "suggested_commands": ["simc decode-build --talents " + "y " * 200],
        # A generated SimC profile: one token per line, carrying the transport strings.
        "generated_profile": 'mage="simc_decode"\nspec=fire\nclass_talents=' + "/".join(f"{103000 + n}:1" for n in range(60)) + "\n",
    }
    cut: list[str] = []
    assert compact_value(payload, max_chars=40, cut=cut) == payload
    assert cut == []


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
