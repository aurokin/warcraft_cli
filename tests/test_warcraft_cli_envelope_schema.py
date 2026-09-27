"""The checked-in envelope schema must match the generator and accept a real envelope.

Validation here uses a tiny local checker rather than `jsonschema`: the repo ships no runtime schema
dependency, and the generator only emits the handful of keywords this file understands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner
from warcraft_cli.main import app as warcraft_app
from warcraft_cli.schema import envelope_json_schema, envelope_schema_document
from warcraft_core.envelope import envelope_violations

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "envelope.schema.json"
JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}
runner = CliRunner()


def validate(instance: Any, schema: dict[str, Any], *, root: dict[str, Any], path: str = "$") -> list[str]:
    """Return why `instance` fails `schema`; supports only the keywords the generator emits."""
    if "$ref" in schema:
        target = schema["$ref"].removeprefix("#/$defs/")
        return validate(instance, root["$defs"][target], root=root, path=path)
    expected = schema.get("type")
    if expected is not None and not isinstance(instance, JSON_TYPES[expected]):
        return [f"{path}: expected {expected}, got {type(instance).__name__}"]
    problems: list[str] = []
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    if "const" in schema and instance != schema["const"]:
        problems.append(f"{path}: {instance!r} is not {schema['const']!r}")
    for subschema in schema.get("allOf", []):
        problems.extend(validate(instance, subschema, root=root, path=path))
    if "if" in schema:
        branch = "then" if not validate(instance, schema["if"], root=root, path=path) else "else"
        if branch in schema:
            problems.extend(validate(instance, schema[branch], root=root, path=path))
    if "not" in schema and not validate(instance, schema["not"], root=root, path=path):
        problems.append(f"{path}: must not match {schema['not']}")
    if not isinstance(instance, dict):
        return problems
    properties: dict[str, Any] = schema.get("properties", {})
    problems.extend(f"{path}: missing required key {key!r}" for key in schema.get("required", []) if key not in instance)
    if schema.get("additionalProperties") is False:
        problems.extend(f"{path}.{key}: unexpected key" for key in instance if key not in properties)
    for key, subschema in properties.items():
        if key in instance:
            problems.extend(validate(instance[key], subschema, root=root, path=f"{path}.{key}"))
    return problems


def test_checked_in_schema_matches_the_generator() -> None:
    assert SCHEMA_PATH.read_text(encoding="utf-8") == envelope_schema_document(), (
        "schemas/envelope.schema.json is stale. Regenerate it with:\n"
        '  .venv/bin/python -c "import pathlib; from warcraft_cli.schema import envelope_schema_document; '
        "pathlib.Path('schemas/envelope.schema.json').write_text(envelope_schema_document())\""
    )


def test_schema_command_prints_the_checked_in_schema() -> None:
    result = runner.invoke(warcraft_app, ["schema"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "envelope_schema"
    assert payload["data"]["schema"] == json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_doctor_payload_validates_against_the_schema() -> None:
    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    schema = envelope_json_schema()
    assert envelope_violations(payload) == []
    assert validate(payload, schema, root=schema) == []


def test_schema_rejects_envelopes_the_contract_forbids() -> None:
    """Without this, a validator that accepted everything would still pass the test above."""
    schema = envelope_json_schema()
    broken = {
        "ok": "yes",
        "provider": "warcraft",
        "command": "doctor",
        "kind": "error",
        "schema_version": "2",
        "query": None,
        "data": {},
        "error": {"message": "no code here"},
    }

    problems = validate(broken, schema, root=schema)
    assert "$: missing required key 'provenance'" in problems
    assert "$.ok: expected boolean, got str" in problems
    assert "$.schema_version: '2' is not one of ['1']" in problems
    assert "$.error: missing required key 'code'" in problems


def test_schema_rejects_keys_outside_the_envelope() -> None:
    schema = envelope_json_schema()
    base = {
        "ok": False,
        "provider": "warcraft",
        "command": "doctor",
        "kind": "error",
        "schema_version": "1",
        "query": None,
        "provenance": {},
        "data": {},
        "error": {"code": "x", "message": "y", "hint": "z"},
    }

    problems = validate({**base, "results": []}, schema, root=schema)
    assert problems == ["$.results: unexpected key", "$.error.hint: unexpected key"]


def test_schema_ties_error_to_ok() -> None:
    schema = envelope_json_schema()
    base = {
        "provider": "warcraft",
        "command": "doctor",
        "kind": "doctor",
        "schema_version": "1",
        "query": None,
        "provenance": {},
        "data": {},
    }

    failure_without_error = validate({**base, "ok": False}, schema, root=schema)
    assert "$: missing required key 'error'" in failure_without_error

    success_with_error = validate({**base, "ok": True, "error": {"code": "x", "message": "y"}}, schema, root=schema)
    assert any(problem.startswith("$: must not match") for problem in success_with_error)

    assert validate({**base, "ok": True}, schema, root=schema) == []
    assert validate({**base, "ok": False, "kind": "error", "error": {"code": "x", "message": "y"}}, schema, root=schema) == []
