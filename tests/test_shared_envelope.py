from __future__ import annotations

import pytest
from warcraft_core.envelope import (
    ENVELOPE_KEYS,
    SCHEMA_VERSION,
    envelope_violations,
    error_envelope,
    success_envelope,
    with_legacy_keys,
)
from warcraft_core.exit_codes import EXIT_AUTH, EXIT_GENERIC, EXIT_NOT_FOUND, exit_code_for
from warcraft_core.provider import ProviderError, ProviderSurface


def test_success_envelope_has_every_required_key_and_no_error() -> None:
    envelope = success_envelope(provider="wowhead", command="search", kind="search_results", data={"results": []}, query="thunderfury")
    assert set(envelope) == ENVELOPE_KEYS - {"error"}
    assert envelope["ok"] is True
    assert envelope["schema_version"] == SCHEMA_VERSION == "1"
    assert envelope["provenance"] == {}
    assert envelope_violations(envelope) == []


def test_error_envelope_carries_code_message_and_optional_details() -> None:
    envelope = error_envelope(provider="wowhead", command="entity", code="not_found", message="no such item", details={"id": 1})
    assert envelope["ok"] is False
    assert envelope["data"] == {}
    assert envelope["error"] == {"code": "not_found", "message": "no such item", "details": {"id": 1}}
    assert "details" not in error_envelope(provider="p", command="c", code="x", message="y")["error"]
    assert envelope_violations(envelope) == []


def test_with_legacy_keys_merges_and_rejects_collisions() -> None:
    envelope = success_envelope(provider="p", command="search", kind="k", data={"results": [1]})
    merged = with_legacy_keys(envelope, {"results": [1], "count": 1})
    assert merged["results"] == [1]
    assert merged["data"] == {"results": [1]}
    with pytest.raises(ValueError, match="collide.*data"):
        with_legacy_keys(envelope, {"data": {}})


def test_envelope_violations_flags_shape_problems() -> None:
    assert "missing key: schema_version" in envelope_violations({"ok": True})
    ok_with_error = {**success_envelope(provider="p", command="c", kind="k", data={}), "error": {"code": "x", "message": "y"}}
    assert envelope_violations(ok_with_error) == ["error must be absent when ok is true"]
    bad_data = {**success_envelope(provider="p", command="c", kind="k", data={}), "data": []}
    assert envelope_violations(bad_data) == ["data must be a dict"]
    error_missing = {**error_envelope(provider="p", command="c", code="x", message="y"), "error": {"code": "x"}}
    assert envelope_violations(error_missing) == ["error.message must be a str"]


def test_exit_code_for_maps_known_codes_and_defaults_to_generic() -> None:
    assert exit_code_for("auth_required") == EXIT_AUTH
    assert exit_code_for("not_found") == EXIT_NOT_FOUND
    assert exit_code_for("something_else") == EXIT_GENERIC


def test_provider_error_defaults_exit_code_from_code() -> None:
    assert ProviderError("auth_required", "login first").exit_code == EXIT_AUTH
    assert ProviderError("auth_required", "login first", exit_code=1).exit_code == 1
    assert str(ProviderError("x", "boom")) == "boom"


def test_provider_surface_is_runtime_checkable() -> None:
    class Dummy:
        name = "dummy"

        def search(self, query: str, *, limit: int = 10, **options: object) -> dict[str, object]:
            return {}

        def resolve(self, target: str, **options: object) -> dict[str, object]:
            return {}

        def doctor(self, **options: object) -> dict[str, object]:
            return {}

    assert isinstance(Dummy(), ProviderSurface)
    assert not isinstance(object(), ProviderSurface)
