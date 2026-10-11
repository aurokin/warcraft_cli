"""Strict input adapter for the finite operations existing composites consume.

The provider operations themselves have typed arguments and never invoke or parse CLI output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import overload

from warcraft_core.provider import ProviderError


@dataclass(frozen=True, slots=True)
class OperationArgs:
    positionals: list[str]
    options: dict[str, list[str]]
    switches: frozenset[str]

    @classmethod
    def parse(
        cls,
        args: list[str],
        *,
        count: int,
        values: frozenset[str] = frozenset(),
        switches: frozenset[str] = frozenset(),
        repeated: frozenset[str] = frozenset(),
    ) -> OperationArgs:
        positionals: list[str] = []
        options: dict[str, list[str]] = {}
        flags: set[str] = set()
        iterator = iter(args)
        for token in iterator:
            if token in switches:
                if token in flags:
                    raise ProviderError("invalid_argument", f"Duplicate option {token}.")
                flags.add(token)
            elif token in values:
                value = next(iterator, None)
                if value is None or value.startswith("--"):
                    raise ProviderError("invalid_argument", f"Missing value for {token}.")
                if token in options and token not in repeated:
                    raise ProviderError("invalid_argument", f"Duplicate option {token}.")
                options.setdefault(token, []).append(value)
            elif token.startswith("--"):
                raise ProviderError("invalid_argument", f"Unknown operation option {token}.")
            else:
                positionals.append(token)
        if len(positionals) != count:
            raise ProviderError("invalid_argument", f"Expected {count} positional arguments, got {len(positionals)}.")
        return cls(positionals, options, frozenset(flags))

    def text(self, name: str, default: str | None = None) -> str | None:
        return self.options.get(name, [default])[-1]

    @overload
    def integer(self, name: str, default: int) -> int: ...

    @overload
    def integer(self, name: str, default: None = None) -> int | None: ...

    def integer(self, name: str, default: int | None = None) -> int | None:
        value = self.text(name)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise ProviderError("invalid_argument", f"{name} must be an integer.") from exc

    def integers(self, name: str) -> list[int] | None:
        try:
            return [int(value) for value in self.options.get(name, [])] or None
        except ValueError as exc:
            raise ProviderError("invalid_argument", f"{name} must contain integers.") from exc
