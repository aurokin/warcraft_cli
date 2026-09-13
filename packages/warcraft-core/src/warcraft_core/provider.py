"""Pure provider surface shared by every provider package.

Each provider package exports ``PROVIDER`` (an object satisfying ``ProviderSurface``) from
``<pkg>/provider.py``. Surface methods never print and never raise ``typer.Exit``; they return an
``Envelope`` or raise ``ProviderError``, which the CLI layer (``warcraft_core.cli``) turns into an
error envelope plus exit code. This lets the ``warcraft`` wrapper call providers in-process.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from warcraft_core.envelope import Envelope
from warcraft_core.exit_codes import exit_code_for


class ProviderError(Exception):
    """Failure raised by pure provider functions instead of ``typer.Exit``."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.exit_code = exit_code if exit_code is not None else exit_code_for(code)


@runtime_checkable
class ProviderSurface(Protocol):
    # Read-only so both plain attributes and frozen dataclass fields satisfy the protocol.
    @property
    def name(self) -> str: ...

    def search(self, query: str, *, limit: int = 10, **options: Any) -> Envelope: ...

    def resolve(self, target: str, **options: Any) -> Envelope: ...

    def doctor(self, **options: Any) -> Envelope: ...
