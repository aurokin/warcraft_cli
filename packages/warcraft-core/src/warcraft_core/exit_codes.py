"""Process exit codes shared by every binary. See docs/foundation/ERROR_CONTRACT.md."""

from __future__ import annotations

from typing import Final, Literal

EXIT_GENERIC: Final = 1
EXIT_USAGE: Final = 2  # Typer/Click default for bad arguments and rejected option values
EXIT_AUTH: Final = 3
EXIT_NOT_FOUND: Final = 4
EXIT_NETWORK: Final = 5  # transport failures and upstream 5xx/429

ExitCode = Literal[1, 2, 3, 4, 5]

# Canonical error-code -> exit-code mapping. Providers keep their existing code strings;
# anything not listed here exits 1.
EXIT_CODE_BY_ERROR_CODE: Final[dict[str, ExitCode]] = {
    "auth_required": EXIT_AUTH,
    "auth_failed": EXIT_AUTH,
    "unauthorized": EXIT_AUTH,
    "forbidden": EXIT_AUTH,
    "not_found": EXIT_NOT_FOUND,
    "network_error": EXIT_NETWORK,
    "timeout": EXIT_NETWORK,
    "upstream_error": EXIT_NETWORK,
    "rate_limited": EXIT_NETWORK,
    "http_error": EXIT_NETWORK,
    "invalid_query": EXIT_USAGE,
    "invalid_argument": EXIT_USAGE,
    "missing_fields": EXIT_USAGE,
}


def exit_code_for(code: str, default: int = EXIT_GENERIC) -> int:
    return EXIT_CODE_BY_ERROR_CODE.get(code, default)
