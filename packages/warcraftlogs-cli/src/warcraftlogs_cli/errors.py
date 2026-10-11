"""Provider-specific exit classification shared by native CLI and wrapper operations."""

from warcraft_core.exit_codes import EXIT_AUTH, EXIT_USAGE, exit_code_for

# Warcraft Logs error codes that mean "the caller is not authorised", on top of the shared vocabulary.
# `site_profile_mismatch` belongs here: the saved token exists but is not usable for the selected site.
_AUTH_ERROR_CODES = frozenset(
    {
        "missing_client_credentials",
        "missing_public_auth",
        "missing_user_auth",
        "site_profile_mismatch",
        "user_token_expired",
    }
)


# Rejected or contradictory command input is a usage error (exit 2), like Click's own parse failures.
# Keep every locally-raised input code here: an omission silently downgrades the command to exit 1.
_USAGE_ERROR_CODES = frozenset(
    {
        "ambiguous_boss",
        "boss_scope_mismatch",
        "invalid_variables",
        "missing_boss",
        "missing_query",
        "missing_scope",
        "missing_spec",
        "missing_state",
        "redirect_uri_mismatch",
        "state_mismatch",
    }
)


def client_error_exit_code(code: str) -> int:
    if code in _AUTH_ERROR_CODES:
        return EXIT_AUTH
    if code in _USAGE_ERROR_CODES:
        return EXIT_USAGE
    return exit_code_for(code)
