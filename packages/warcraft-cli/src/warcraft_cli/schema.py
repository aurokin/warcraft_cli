"""JSON Schema (draft 2020-12) for the shared envelope, derived from the envelope TypedDicts.

`warcraft schema` prints this document and `schemas/envelope.schema.json` checks it in, so agents can
validate envelopes without importing Python. Deriving it from ``warcraft_core.envelope.Envelope``
keeps the two from drifting; `tests/test_warcraft_cli_envelope_schema.py` fails when they do and
prints the one-liner that rewrites the checked-in copy.
"""

from __future__ import annotations

import json
from typing import Any, Literal, NotRequired, get_args, get_origin, get_type_hints

from warcraft_core.envelope import REQUIRED_KEYS, Envelope, EnvelopeError

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _optional_keys(typed_dict: Any) -> frozenset[str]:
    """Keys the TypedDict marks ``NotRequired``.

    ``__required_keys__``/``__optional_keys__`` are unreliable for ``warcraft_core.envelope``: that
    module uses PEP 563 annotations, so the TypedDict machinery never saw the ``NotRequired``
    wrapper and reports every key as required. Resolving the hints with ``include_extras`` keeps
    the wrapper, so it can be read back here.
    """
    hints = get_type_hints(typed_dict, include_extras=True)
    return frozenset(name for name, annotation in hints.items() if get_origin(annotation) is NotRequired)


def _property_schema(annotation: Any) -> dict[str, Any]:
    """JSON Schema for one envelope field. Unhandled annotations raise so the schema cannot go stale."""
    if annotation is Any:
        return {}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is str:
        return {"type": "string"}
    if get_origin(annotation) is Literal:
        return {"enum": list(get_args(annotation))}
    if get_origin(annotation) is dict:
        # dict[str, Any]: an open object whose contents each provider owns.
        return {"type": "object"}
    if hasattr(annotation, "__annotations__"):
        return {"$ref": f"#/$defs/{annotation.__name__}"}
    raise TypeError(f"No JSON Schema mapping for envelope annotation {annotation!r}")


def _typed_dict_schema(typed_dict: Any, *, required: frozenset[str], description: str) -> dict[str, Any]:
    hints = get_type_hints(typed_dict)
    return {
        "type": "object",
        "description": description,
        "properties": {name: _property_schema(annotation) for name, annotation in hints.items()},
        "required": [name for name in hints if name in required],
        # Closed: payload fields live under `data` and failure context under `error.details`.
        "additionalProperties": False,
    }


def envelope_json_schema() -> dict[str, Any]:
    """The envelope schema every `warcraft`/provider payload validates against."""
    error_keys = frozenset(get_type_hints(EnvelopeError)) - _optional_keys(EnvelopeError)
    return {
        "$schema": SCHEMA_DIALECT,
        "title": "Warcraft CLI envelope",
        **_typed_dict_schema(
            Envelope,
            required=REQUIRED_KEYS,
            description=(
                "The single JSON object every binary in this repo writes to stdout on success or to "
                "stderr on failure. See docs/foundation/ERROR_CONTRACT.md."
            ),
        ),
        "$defs": {
            "EnvelopeError": _typed_dict_schema(
                EnvelopeError,
                required=error_keys,
                description="Present only when `ok` is false.",
            )
        },
        # TypedDicts cannot express that `error` depends on `ok`; state it here so validators
        # reject a failure without an error and a success that carries one.
        "allOf": [
            {"if": {"required": ["ok"], "properties": {"ok": {"const": False}}}, "then": {"required": ["error"]}},
            {"if": {"required": ["ok"], "properties": {"ok": {"const": True}}}, "then": {"not": {"required": ["error"]}}},
        ],
    }


def envelope_schema_document() -> str:
    """Exactly the text checked in at ``schemas/envelope.schema.json``."""
    return json.dumps(envelope_json_schema(), indent=2) + "\n"
