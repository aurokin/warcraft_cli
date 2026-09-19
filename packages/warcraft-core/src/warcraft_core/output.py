from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

import orjson
import typer

OutputProfile = Literal["agent", "human"]
DEFAULT_COMPACT_MAX_CHARS = 280

# Key added to a --fields projection listing the requested dot-paths the payload did not have, so a
# thin or empty projection is never mistaken for a genuinely empty result. See
# docs/foundation/ERROR_CONTRACT.md.
FIELDS_MISSING_KEY = "fields_missing"


class OutputProjectionError(ValueError):
    """Raised when --fields-strict is set and a requested dot-path is absent."""

    def __init__(self, missing_fields: tuple[str, ...]) -> None:
        self.missing_fields = missing_fields
        super().__init__(f"Missing requested fields: {', '.join(missing_fields)}")


@dataclass(frozen=True, slots=True)
class OutputOptions:
    pretty: bool = False
    compact: bool = False
    compact_max_chars: int = DEFAULT_COMPACT_MAX_CHARS
    fields: tuple[str, ...] = ()
    fields_strict: bool = False

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
        if key not in {"agent", "human"}:
            raise ValueError("--profile must be one of: agent, human")
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
    """Project ``payload`` down to ``fields``.

    Requested paths the payload does not have are reported: under ``strict`` as an
    ``OutputProjectionError`` (exit 2), otherwise as the ``fields_missing`` key, so a caller never
    reads a thin or empty projection as a genuinely empty result.
    """
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
        else:
            missing.append(path)

    if missing and strict:
        raise OutputProjectionError(tuple(missing))
    if missing:
        filtered[FIELDS_MISSING_KEY] = missing
    return filtered


def shape_payload(payload: dict[str, Any], options: OutputOptions) -> dict[str, Any]:
    """Apply --compact and --fields to ``payload``."""
    rendered: dict[str, Any] = payload
    if options.compact:
        rendered = compact_value(rendered, max_chars=options.compact_max_chars)
    if options.fields:
        rendered = filter_payload_fields(rendered, fields=options.fields, strict=options.fields_strict)
    return rendered


def to_json(payload: Any, *, pretty: bool) -> str:
    option = 0
    if pretty:
        option |= orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS
    return orjson.dumps(payload, option=option).decode("utf-8")


def emit(payload: Any, *, pretty: bool, err: bool = False) -> None:
    typer.echo(to_json(payload, pretty=pretty), err=err)


def emit_shaped(payload: dict[str, Any], options: OutputOptions, *, err: bool = False) -> None:
    emit(shape_payload(payload, options), pretty=options.pretty, err=err)
