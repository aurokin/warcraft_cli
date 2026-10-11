"""Output-free contracts and envelope helpers shared by wrapper features.

Importing this module does not initialize provider apps or the concrete registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from warcraft_core.envelope import ENVELOPE_KEYS, SCHEMA_VERSION
from warcraft_core.exit_codes import EXIT_GENERIC, EXIT_NETWORK, exit_code_for
from warcraft_core.shapes import as_dict

if TYPE_CHECKING:
    from warcraft_cli.providers import DescribeOptions, PacketInput

SimcCommand = Literal["identify-build", "decode-build", "describe-build", "validate-talent-transport"]


def source_exit_code(source_result: Mapping[str, Any]) -> int:
    """Exit with the failing source's own code (blocked -> 5, not_found -> 4) instead of a flat 1."""
    code = source_result.get("exit_code")
    if isinstance(code, int) and code != 0:
        return code
    error = source_result.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    return exit_code_for(error_code) if isinstance(error_code, str) else EXIT_GENERIC


def wrapper_envelope(command: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a wrapper-built payload as the contract envelope: exactly the envelope keys, nothing else.

    Envelope keys the payload sets win over the defaults, except that a failure's ``kind`` is always
    ``error``. Every other key is payload content: it goes under ``data`` on success, and under
    ``error.details`` on failure, where ``data`` is ``{}``.
    """
    ok = bool(payload.get("ok", "error" not in payload))
    body = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    envelope: dict[str, Any] = {
        "ok": ok,
        "provider": "warcraft",
        "command": command,
        "kind": command,
        "schema_version": SCHEMA_VERSION,
        "query": None,
        "provenance": {},
        **{key: value for key, value in payload.items() if key in ENVELOPE_KEYS},
    }
    if ok:
        envelope["data"] = {**body, **as_dict(payload.get("data"))}
        return envelope
    error = dict(as_dict(payload.get("error")))
    details = {**body, **as_dict(error.get("details"))}
    if details:
        error["details"] = details
    envelope.update(ok=False, kind="error", data={}, error=error)
    return envelope


def provider_payload_data(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``data`` body of a provider envelope: the only place a wrapper composite reads its fields."""
    return as_dict(as_dict(payload).get("data"))


class ProviderFetch(Protocol):
    """Runs one provider command and returns ``{provider, status, payload, error?, exit_code}``."""

    def __call__(self, provider: str, args: list[str], *, expansion: str | None) -> dict[str, Any]: ...


class ProviderInvoke(Protocol):
    """``provider_invoke``: one provider command, as ``{provider, exit_code, payload}``."""

    def __call__(self, provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]: ...


class ProviderGuideExport(Protocol):
    """One provider's complete guide bundle written to an explicit directory."""

    def __call__(self, provider: str, guide_ref: str, *, out: Path, expansion: str | None = None) -> dict[str, Any]: ...


class ProviderLookup(Protocol):
    """``provider_search`` / ``provider_resolve``: one free-text lookup, as ``{provider, exit_code, payload}``."""

    def __call__(
        self, provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()
    ) -> dict[str, Any]: ...


class SimcCall(Protocol):
    """``simc_call``: one in-process simc build command."""

    def __call__(self, command: SimcCommand, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProviderCalls:
    """The provider seams a composite command runs through.

    ``warcraft_cli.main`` builds this from its own module globals at call time, so a test that
    replaces ``warcraft_cli.main.provider_invoke`` (or ``simc_call``) reaches every feature module.
    """

    invoke: ProviderInvoke
    resolve: ProviderLookup
    search: ProviderLookup
    simc: SimcCall

    def guide_export(self, provider: str, guide_ref: str, *, out: Path, expansion: str | None = None) -> dict[str, Any]:
        """Typed guide operation; the invoke adapter preserves call-time injection for composite tests."""
        return self.invoke(provider, ["guide-export", guide_ref, "--out", str(out)], expansion=expansion)


def failed_call(result: Mapping[str, Any]) -> tuple[dict[str, Any], int] | None:
    """The provider's error (always with a ``code`` and ``message``) and exit code; ``None`` on success."""
    payload = as_dict(result.get("payload"))
    exit_code = result.get("exit_code")
    if exit_code == 0 and payload.get("ok") is not False:
        return None
    error = as_dict(payload.get("error"))
    message = error.get("message") or f"{result.get('provider') or 'The provider'} exited {exit_code}."
    failure = {**error, "code": error.get("code") or "provider_failed", "message": message}
    return failure, source_exit_code({"exit_code": exit_code, "error": failure})


def shared_failure(failed_rows: list[dict[str, Any]]) -> tuple[str, int]:
    """``(error.code, exit code)`` for a command that failed because every provider it needed failed.

    Agreeing providers lend their own code and exit code. Disagreeing ones that all failed upstream
    (exit 5) are ``upstream_error``; any other mix is ``providers_failed``, exit 1, because a
    deterministic crash or a bad argument must not read as "retry later".
    """
    codes = {row.get("code") for row in failed_rows}
    exit_codes = {row.get("exit_code") for row in failed_rows}
    exit_code = exit_codes.pop() if len(exit_codes) == 1 else EXIT_GENERIC
    if not isinstance(exit_code, int) or exit_code == 0:
        exit_code = EXIT_GENERIC
    if len(codes) == 1 and isinstance(code := next(iter(codes)), str):
        return code, exit_code
    return ("upstream_error" if exit_code == EXIT_NETWORK else "providers_failed"), exit_code
