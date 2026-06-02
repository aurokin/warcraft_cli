from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, request_with_retries
from warcraft_core.auth import load_provider_auth_state, save_provider_auth_state
from warcraft_core.wow_normalization import normalize_region

from blizzard_api_cli.auth import BlizzardAuthConfig, load_blizzard_auth_config

# Shared-state provider key for the cached client-credentials token. Distinct from the doctor/auth
# posture provider ("blizzard-api") so the token cache and the credential-discovery state never
# collide in the same state file.
CLIENT_CREDENTIALS_STATE_PROVIDER = "blizzard-api-client-credentials"

# Blizzard API hosts only exist for these regions. normalize_region also recognizes "oc"/"world",
# which are NOT valid Blizzard hosts, so the resolver validates against this tuple explicitly.
SUPPORTED_REGIONS = ("us", "eu", "kr", "tw", "cn")

# Game versions this slice routes. Retail + the current classic line are documented + stable;
# era/Season-of-Discovery namespaces (e.g. "classic1x") are deferred until a live spike confirms
# them, so anything outside this set is rejected rather than guessed at.
SUPPORTED_GAME_VERSIONS = ("retail", "classic")

DEFAULT_LOCALE = "en_US"
DEFAULT_REGION = "us"

# Token-expiry safety skew (seconds): refresh slightly early so an in-flight request never races a
# server-side expiry. Mirrors the warcraftlogs client.
_TOKEN_SKEW_SECONDS = 60

# Hosts, OAuth token URL, and namespace strings below follow documented Blizzard API conventions
# and are pending one-time live confirmation (run BLIZZARD_LIVE_TESTS=1). doctor + every command
# payload carry provenance.verified=false to keep that posture honest.
_VERIFICATION_NOTE = (
    "Hosts, OAuth token URL, and namespace strings follow documented Blizzard API conventions and "
    "are pending one-time live confirmation (run BLIZZARD_LIVE_TESTS=1). CN endpoints are especially "
    "unconfirmed; classic namespace strings are best-effort."
)


class BlizzardClientError(RuntimeError):
    """Typed client error so the command layer can emit a structured ok:false envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class BlizzardRouting:
    region: str
    host: str
    oauth_token_url: str
    namespace: str
    namespace_class: str
    game_version: str
    locale: str


def _api_host(region: str) -> str:
    if region == "cn":
        return "https://gateway.battlenet.com.cn"
    return f"https://{region}.api.blizzard.com"


def _oauth_token_url(region: str) -> str:
    if region == "cn":
        return "https://oauth.battlenet.com.cn/token"
    return "https://oauth.battle.net/token"


def resolve_game_version(*, game_version: str | None, classic: bool) -> str:
    """Reconcile the --game-version option and the --classic shorthand into one token."""
    resolved = (game_version or "retail").strip().lower()
    if classic and resolved not in ("retail", "classic"):
        # --classic is a shorthand for --game-version classic; combining it with an unsupported
        # explicit version is surfaced under the documented unsupported_game_version code.
        raise BlizzardClientError(
            "unsupported_game_version",
            f"--classic conflicts with --game-version {resolved!r}; classic-era / Season of "
            "Discovery namespaces are deferred pending a live spike.",
        )
    if classic:
        resolved = "classic"
    if resolved not in SUPPORTED_GAME_VERSIONS:
        raise BlizzardClientError(
            "unsupported_game_version",
            f"Blizzard routing supports game versions {SUPPORTED_GAME_VERSIONS}; got {resolved!r}. "
            "Classic-era / Season of Discovery namespaces are deferred pending a live spike.",
        )
    return resolved


def resolve_routing(
    *,
    region_input: str | None,
    game_version: str | None = None,
    classic: bool = False,
    locale: str | None = None,
    namespace_class: str,
) -> BlizzardRouting:
    region = normalize_region(region_input) if region_input and region_input.strip() else DEFAULT_REGION
    if region not in SUPPORTED_REGIONS:
        raise BlizzardClientError(
            "unsupported_region",
            f"Blizzard API supports regions {SUPPORTED_REGIONS}; got {region!r}.",
        )
    resolved_version = resolve_game_version(game_version=game_version, classic=classic)
    if resolved_version == "classic" and namespace_class == "profile":
        raise BlizzardClientError(
            "classic_profile_unsupported",
            "The Blizzard Profile API has no classic namespace; character lookups are retail-only.",
        )
    infix = "classic-" if resolved_version == "classic" else ""
    namespace = f"{namespace_class}-{infix}{region}"
    return BlizzardRouting(
        region=region,
        host=_api_host(region),
        oauth_token_url=_oauth_token_url(region),
        namespace=namespace,
        namespace_class=namespace_class,
        game_version=resolved_version,
        locale=(locale or DEFAULT_LOCALE),
    )


class BlizzardClient:
    def __init__(
        self,
        *,
        auth: BlizzardAuthConfig | None = None,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        auth = auth if auth is not None else load_blizzard_auth_config()
        self._client_id = auth.client_id or ""
        self._client_secret = auth.client_secret or ""
        self._default_region = normalize_region(auth.region) if auth.region else DEFAULT_REGION
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._http_client: httpx.Client | None = None
        self._access_token: str | None = None
        self._token_region: str | None = None
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    @property
    def default_region(self) -> str:
        return self._default_region

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self._timeout_seconds, follow_redirects=True)
        return self._http_client

    def _credential_cache_key(self, region: str) -> str:
        raw = f"{region}\0{self._client_id}\0{self._client_secret}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_shared_client_token(self, *, region: str, now: float) -> str | None:
        try:
            payload = load_provider_auth_state(CLIENT_CREDENTIALS_STATE_PROVIDER)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("auth_mode") != "client_credentials":
            return None
        if payload.get("credential_key") != self._credential_cache_key(region):
            return None
        token = payload.get("access_token")
        expires_at = payload.get("expires_at")
        if not isinstance(token, str) or not token.strip():
            return None
        if not isinstance(expires_at, (int, float)):
            return None
        if now >= float(expires_at) - _TOKEN_SKEW_SECONDS:
            return None
        self._access_token = token
        self._token_region = region
        self._token_expires_at = float(expires_at)
        return token

    def _save_shared_client_token(self, *, region: str, token: str, expires_at: float) -> None:
        # Single-entry shared cache (one JSON object per provider-state path), mirroring the
        # warcraftlogs client. Alternating regions/credentials across separate processes re-fetches
        # the token on each switch; that is a deliberate non-goal here (a token fetch is cheap and
        # tokens are long-lived). The credential_key still guards against serving a stale token for
        # the wrong region/credentials. A multi-entry keyed cache is intentionally not built.
        try:
            save_provider_auth_state(
                CLIENT_CREDENTIALS_STATE_PROVIDER,
                {
                    "access_token": token,
                    "auth_mode": "client_credentials",
                    "credential_key": self._credential_cache_key(region),
                    "expires_at": expires_at,
                    "region": region,
                    "token_type": "Bearer",
                },
            )
        except OSError:
            # Shared token caching is an optimization; commands still work without a writable
            # state directory.
            return

    def _token(self, routing: BlizzardRouting) -> str:
        now = time.time()
        # Tokens are region-scoped (CN uses a different OAuth host from us/eu/kr/tw), so the
        # in-memory cache must match the target region before it can be reused.
        if self._access_token and self._token_region == routing.region and now < self._token_expires_at - _TOKEN_SKEW_SECONDS:
            return self._access_token
        if not self.configured:
            raise BlizzardClientError(
                "missing_client_credentials",
                "Blizzard commands need BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET. Set them in "
                ".env.local, the provider env file, or the environment.",
            )
        shared_token = self._load_shared_client_token(region=routing.region, now=now)
        if shared_token is not None:
            return shared_token
        response = request_with_retries(
            self._client(),
            routing.oauth_token_url,
            method="POST",
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
            retry_attempts=self._retry_attempts,
        )
        payload = self._decode_json(response)
        token = payload.get("access_token")
        expires_in = payload.get("expires_in", 3600)
        if not isinstance(token, str) or not token:
            raise BlizzardClientError("invalid_response", "Blizzard token response did not include an access token.")
        try:
            expires_seconds = int(expires_in)
        except (TypeError, ValueError) as exc:
            raise BlizzardClientError("invalid_response", "Blizzard token response had a non-numeric expires_in.") from exc
        self._access_token = token
        self._token_region = routing.region
        self._token_expires_at = now + expires_seconds
        self._save_shared_client_token(region=routing.region, token=token, expires_at=self._token_expires_at)
        return token

    @staticmethod
    def _decode_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise BlizzardClientError("invalid_response", "Blizzard response was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise BlizzardClientError("invalid_response", "Blizzard response was not a JSON object.")
        return payload

    def _get(self, routing: BlizzardRouting, path: str) -> dict[str, Any]:
        token = self._token(routing)
        url = f"{routing.host}{path}"
        response = request_with_retries(
            self._client(),
            url,
            method="GET",
            params={"namespace": routing.namespace, "locale": routing.locale},
            headers={"Authorization": f"Bearer {token}"},
            retry_attempts=self._retry_attempts,
        )
        return {
            "payload": self._decode_json(response),
            "routing": routing,
            "source_url": str(response.request.url),
        }

    def fetch_realm(
        self,
        slug: str,
        *,
        region: str | None = None,
        game_version: str | None = None,
        classic: bool = False,
        locale: str | None = None,
    ) -> dict[str, Any]:
        routing = resolve_routing(
            region_input=region or self._default_region,
            game_version=game_version,
            classic=classic,
            locale=locale,
            namespace_class="dynamic",
        )
        # Blizzard realm slugs are lowercase (e.g. "illidan", "mal-ganis").
        return self._get(routing, f"/data/wow/realm/{slug.lower()}")

    def fetch_item(
        self,
        item_id: int,
        *,
        region: str | None = None,
        game_version: str | None = None,
        classic: bool = False,
        locale: str | None = None,
    ) -> dict[str, Any]:
        routing = resolve_routing(
            region_input=region or self._default_region,
            game_version=game_version,
            classic=classic,
            locale=locale,
            namespace_class="static",
        )
        return self._get(routing, f"/data/wow/item/{item_id}")

    def fetch_character(
        self,
        realm: str,
        name: str,
        *,
        region: str | None = None,
        game_version: str | None = None,
        classic: bool = False,
        locale: str | None = None,
    ) -> dict[str, Any]:
        routing = resolve_routing(
            region_input=region or self._default_region,
            game_version=game_version,
            classic=classic,
            locale=locale,
            namespace_class="profile",
        )
        return self._get(routing, f"/profile/wow/character/{realm.lower()}/{name.lower()}")


def verification_note() -> str:
    return _VERIFICATION_NOTE
