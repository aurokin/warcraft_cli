from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import orjson
import typer

OutputProfile = Literal["agent", "human", "debug"]
DEFAULT_COMPACT_MAX_CHARS = 280


class OutputProjectionError(ValueError):
    """Raised when --fields-strict is set and a requested dot-path is absent."""

    def __init__(self, missing_fields: tuple[str, ...]) -> None:
        self.missing_fields = missing_fields
        super().__init__(f"Missing requested fields: {', '.join(missing_fields)}")


@dataclass(slots=True)
class DiagnosticsCollector:
    timings_ms: dict[str, float] = field(default_factory=dict)
    request_count: int = 0
    cache_hits: int = 0
    cache_misses: int = 0

    def record_request(self) -> None:
        self.request_count += 1

    def record_cache_hit(self) -> None:
        self.cache_hits += 1

    def record_cache_miss(self) -> None:
        self.cache_misses += 1

    def set_timing(self, label: str, milliseconds: float) -> None:
        self.timings_ms[label] = round(float(milliseconds), 3)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.timings_ms:
            payload["timings_ms"] = dict(self.timings_ms)
        if self.request_count:
            payload["request_count"] = self.request_count
        if self.cache_hits:
            payload["cache_hits"] = self.cache_hits
        if self.cache_misses:
            payload["cache_misses"] = self.cache_misses
        return payload

    def has_values(self) -> bool:
        return bool(self.to_payload())


@dataclass(frozen=True, slots=True)
class OutputOptions:
    pretty: bool = False
    compact: bool = False
    compact_max_chars: int = DEFAULT_COMPACT_MAX_CHARS
    fields: tuple[str, ...] = ()
    fields_strict: bool = False
    include_diagnostics: bool = False

    @property
    def profile(self) -> OutputProfile | None:
        return self._profile

    _profile: OutputProfile | None = field(default=None, repr=False)


def normalize_field_paths(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for candidate in str(raw).split(","):
            path = candidate.strip()
            if not path or path in seen:
                continue
            seen.add(path)
            normalized.append(path)
    return tuple(normalized)


def resolve_output_options(
    *,
    profile: str | None = None,
    pretty: bool = False,
    compact: bool = False,
    compact_max_chars: int = DEFAULT_COMPACT_MAX_CHARS,
    fields: list[str] | tuple[str, ...] = (),
    fields_strict: bool = False,
) -> OutputOptions:
    normalized_profile: OutputProfile | None = None
    if profile is not None:
        key = profile.strip().lower()
        if key not in {"agent", "human", "debug"}:
            raise ValueError("--profile must be one of: agent, human, debug")
        normalized_profile = key  # type: ignore[assignment]

    options = OutputOptions(
        pretty=pretty,
        compact=compact,
        compact_max_chars=max(40, int(compact_max_chars)),
        fields=normalize_field_paths(fields),
        fields_strict=fields_strict,
        _profile=normalized_profile,
    )

    if normalized_profile == "human":
        options = replace(options, pretty=True)
    elif normalized_profile == "debug":
        options = replace(options, pretty=True, include_diagnostics=True)
    elif normalized_profile == "agent":
        options = replace(options, pretty=False)

    return options


def truncate_string(value: str, *, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3] + "..."


def compact_value(value: Any, *, max_chars: int) -> Any:
    if isinstance(value, str):
        return truncate_string(value, max_chars=max_chars)
    if isinstance(value, list):
        return [compact_value(row, max_chars=max_chars) for row in value]
    if isinstance(value, dict):
        return {key: compact_value(item, max_chars=max_chars) for key, item in value.items()}
    return value


def extract_dict_path(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = payload
    for key in path.split("."):
        if not isinstance(current, dict):
            return False, None
        if key not in current:
            return False, None
        current = current[key]
    return True, current


def assign_dict_path(target: dict[str, Any], path: str, value: Any) -> None:
    keys = [key for key in path.split(".") if key]
    if not keys:
        return
    cursor = target
    for key in keys[:-1]:
        existing = cursor.get(key)
        if not isinstance(existing, dict):
            existing = {}
            cursor[key] = existing
        cursor = existing
    cursor[keys[-1]] = value


def filter_payload_fields(
    payload: dict[str, Any],
    *,
    fields: tuple[str, ...],
    strict: bool = False,
) -> dict[str, Any]:
    if not fields:
        return payload

    filtered: dict[str, Any] = {}
    if payload.get("ok") is False:
        filtered["ok"] = payload["ok"]
    if payload.get("ok") is False and "error" in payload:
        filtered["error"] = payload["error"]

    missing: list[str] = []
    for path in fields:
        found, value = extract_dict_path(payload, path)
        if found:
            assign_dict_path(filtered, path, value)
        elif strict:
            missing.append(path)

    if missing:
        raise OutputProjectionError(tuple(missing))
    return filtered


def attach_diagnostics(payload: dict[str, Any], diagnostics: DiagnosticsCollector | Mapping[str, Any] | None) -> dict[str, Any]:
    if diagnostics is None:
        return payload
    block = diagnostics.to_payload() if isinstance(diagnostics, DiagnosticsCollector) else dict(diagnostics)
    if not block:
        return payload
    merged = dict(payload)
    merged["diagnostics"] = block
    return merged


def shape_payload(
    payload: dict[str, Any],
    options: OutputOptions,
    *,
    diagnostics: DiagnosticsCollector | None = None,
) -> dict[str, Any]:
    rendered: dict[str, Any] = payload
    if options.compact:
        rendered = compact_value(rendered, max_chars=options.compact_max_chars)
    if options.fields:
        rendered = filter_payload_fields(rendered, fields=options.fields, strict=options.fields_strict)
    if options.include_diagnostics:
        rendered = attach_diagnostics(rendered, diagnostics)
    return rendered


def to_json(payload: Any, *, pretty: bool) -> str:
    option = 0
    if pretty:
        option |= orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS
    return orjson.dumps(payload, option=option).decode("utf-8")


def emit(payload: Any, *, pretty: bool, err: bool = False) -> None:
    typer.echo(to_json(payload, pretty=pretty), err=err)


def emit_shaped(
    payload: dict[str, Any],
    options: OutputOptions,
    *,
    diagnostics: DiagnosticsCollector | None = None,
    err: bool = False,
) -> None:
    emit(shape_payload(payload, options, diagnostics=diagnostics), pretty=options.pretty, err=err)
