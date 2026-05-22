from __future__ import annotations

import pytest

from warcraft_core.output import (
    DiagnosticsCollector,
    OutputProjectionError,
    compact_value,
    filter_payload_fields,
    normalize_field_paths,
    resolve_output_options,
    shape_payload,
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


def test_resolve_output_options_human_profile_enables_pretty() -> None:
    options = resolve_output_options(profile="human")
    assert options.pretty is True
    assert options.include_diagnostics is False


def test_resolve_output_options_debug_profile_enables_diagnostics() -> None:
    options = resolve_output_options(profile="debug")
    assert options.pretty is True
    assert options.include_diagnostics is True


def test_shape_payload_attaches_diagnostics_block() -> None:
    collector = DiagnosticsCollector(request_count=2, cache_hits=1)
    collector.set_timing("search", 12.5)
    payload = shape_payload({"ok": True}, resolve_output_options(profile="debug"), diagnostics=collector)
    assert payload["diagnostics"]["request_count"] == 2
    assert payload["diagnostics"]["cache_hits"] == 1
    assert payload["diagnostics"]["timings_ms"]["search"] == 12.5


def test_resolve_output_options_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError, match="agent, human, debug"):
        resolve_output_options(profile="verbose")
