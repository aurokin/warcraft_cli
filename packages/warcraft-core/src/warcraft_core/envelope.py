"""The one JSON envelope every binary emits. See docs/foundation/ERROR_CONTRACT.md."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal, NotRequired, TypedDict

SCHEMA_VERSION: Final = "1"


class EnvelopeError(TypedDict):
    code: str
    message: str
    details: NotRequired[dict[str, Any]]


class Envelope(TypedDict):
    ok: bool
    provider: str
    command: str
    kind: str
    schema_version: Literal["1"]
    query: Any  # str | dict | None
    provenance: dict[str, Any]  # {} when there is none
    data: dict[str, Any]  # {} on error
    error: NotRequired[EnvelopeError]  # present only when ok is False


ENVELOPE_KEYS: Final = frozenset({"ok", "provider", "command", "kind", "schema_version", "query", "provenance", "data", "error"})
REQUIRED_KEYS: Final = ENVELOPE_KEYS - {"error"}


def success_envelope(
    *,
    provider: str,
    command: str,
    kind: str,
    data: dict[str, Any],
    query: Any = None,
    provenance: dict[str, Any] | None = None,
) -> Envelope:
    return {
        "ok": True,
        "provider": provider,
        "command": command,
        "kind": kind,
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "provenance": dict(provenance or {}),
        "data": data,
    }


def error_envelope(
    *,
    provider: str,
    command: str,
    code: str,
    message: str,
    kind: str = "error",
    query: Any = None,
    details: dict[str, Any] | None = None,
) -> Envelope:
    error: EnvelopeError = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {
        "ok": False,
        "provider": provider,
        "command": command,
        "kind": kind,
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "provenance": {},
        "data": {},
        "error": error,
    }


def _envelope_type_violations(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if "ok" in payload and not isinstance(payload["ok"], bool):
        problems.append("ok must be a bool")
    for key in ("provider", "command", "kind"):
        if key in payload and not isinstance(payload[key], str):
            problems.append(f"{key} must be a str")
    if "schema_version" in payload and payload["schema_version"] != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION!r}")
    for key in ("provenance", "data"):
        if key in payload and not isinstance(payload[key], dict):
            problems.append(f"{key} must be a dict")
    return problems


def _envelope_error_violations(payload: Mapping[str, Any]) -> list[str]:
    ok = payload.get("ok")
    error = payload.get("error")
    if ok is True and error is not None:
        return ["error must be absent when ok is true"]
    if ok is not False:
        return []
    if not isinstance(error, dict):
        return ["error must be a dict when ok is false"]
    problems = [f"error.{key} must be a str" for key in ("code", "message") if not isinstance(error.get(key), str)]
    if "details" in error and not isinstance(error["details"], dict):
        problems.append("error.details must be a dict")
    return problems


def envelope_violations(payload: Mapping[str, Any]) -> list[str]:
    """Return why ``payload`` is not a conforming envelope; an empty list means it conforms."""
    problems = [f"missing key: {key}" for key in sorted(REQUIRED_KEYS) if key not in payload]
    problems.extend(_envelope_type_violations(payload))
    problems.extend(_envelope_error_violations(payload))
    return problems
