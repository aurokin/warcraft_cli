"""Shared on-disk cache for OAuth client-credentials tokens (Blizzard, Warcraft Logs).

Each provider keeps one entry: the last token it fetched, tagged with a digest of the token's scope
(a region or site, whose OAuth host minted it) and the credentials, so a token is never reused for
another scope or after a credential rotation. Alternating scopes across processes re-fetches the
token on each switch; a token fetch is cheap, so a multi-entry cache is deliberately not built.
The cache is an optimization: an unreadable or unwritable state file only costs a token fetch.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from warcraft_core.auth import load_provider_auth_state, save_provider_auth_state

# Treat a token as expired this many seconds early, so an in-flight request never races the expiry.
TOKEN_SKEW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ClientTokenCache:
    state_provider: str
    scope: str
    client_id: str
    client_secret: str

    def _credential_key(self) -> str:
        return hashlib.sha256(f"{self.scope}\0{self.client_id}\0{self.client_secret}".encode()).hexdigest()

    def load(self, *, now: float) -> tuple[str, float] | None:
        """The cached ``(token, expires_at)`` for this scope and these credentials, unless it is about to expire."""
        payload = load_provider_auth_state(self.state_provider) or {}
        token = payload.get("access_token")
        expires_at = payload.get("expires_at")
        if (
            payload.get("auth_mode") != "client_credentials"
            or payload.get("credential_key") != self._credential_key()
            or not isinstance(token, str)
            or not token.strip()
            or not isinstance(expires_at, (int, float))
            or now >= float(expires_at) - TOKEN_SKEW_SECONDS
        ):
            return None
        return token, float(expires_at)

    def save(self, *, token: str, expires_at: float) -> None:
        try:
            save_provider_auth_state(
                self.state_provider,
                {
                    "access_token": token,
                    "auth_mode": "client_credentials",
                    "credential_key": self._credential_key(),
                    "expires_at": expires_at,
                    "scope": self.scope,
                    "token_type": "Bearer",
                },
            )
        except OSError:
            return
