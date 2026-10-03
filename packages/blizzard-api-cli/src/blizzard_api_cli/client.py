from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.client_credentials import TOKEN_SKEW_SECONDS, ClientTokenCache
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, build_client, request_with_retries
from warcraft_core.paths import provider_cache_root
from warcraft_core.wow_normalization import normalize_region, profile_region, realm_slug_variants, slug_parts

from blizzard_api_cli.auth import BlizzardAuthConfig, load_blizzard_auth_config

# Shared-state provider key for the cached client-credentials token.
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

# Regions whose API host, OAuth token URL, and namespace strings have been confirmed against live
# Blizzard endpoints (retail and classic Game Data, retail Profile). CN routes through a different
# host and OAuth server that is unreachable from outside China, so it stays unconfirmed and its
# payloads keep provenance.verified=false.
VERIFIED_REGIONS = frozenset({"us", "eu", "kr", "tw"})

# The one statement of the verification posture. `blizzard --help` (and docs/reference/blizzard.md,
# generated from it), doctor's notes, and every payload's provenance.verification_note all print
# these strings, so the region list is derived from VERIFIED_REGIONS instead of re-typed in each
# place and left to drift out of sync with what provenance.verified actually reports.
_UNVERIFIED_CN_NOTE = (
    "CN routing (gateway.battlenet.com.cn + oauth.battlenet.com.cn) follows documented Blizzard API "
    "conventions and is unconfirmed; those hosts are unreachable from outside China, so CN payloads "
    "report provenance.verified=false."
)
_VERIFIED_NOTE = (
    "Host, OAuth token URL, and namespace strings are confirmed against live Blizzard endpoints for "
    f"{'/'.join(region for region in SUPPORTED_REGIONS if region in VERIFIED_REGIONS)} (retail and "
    "classic Game Data, retail Profile), whose payloads report provenance.verified=true. "
    + _UNVERIFIED_CN_NOTE
)


def load_blizzard_cache_settings_from_env() -> tuple[CacheSettings, int, int]:
    """Resolve cache settings plus the static and the dynamic/profile namespace TTLs.

    Static data (items) changes with a patch; realms and character profiles change as people play.
    """
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="BLIZZARD",
        # Resolved per call, not at import, so the cache root follows HOME/XDG as they are now.
        default_cache_dir=provider_cache_root("blizzard-api") / "http",
        default_redis_prefix="blizzard_cli",
        ttl_defaults=CacheTTLConfig(search_suggestions=86400, entity_response=900),
        ttl_env_overrides={
            "search_suggestions": "BLIZZARD_STATIC_CACHE_TTL_SECONDS",
            "entity_response": "BLIZZARD_DYNAMIC_CACHE_TTL_SECONDS",
        },
    )
    return settings, settings.ttls.search_suggestions, settings.ttls.entity_response


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
    explicit = game_version is not None and game_version.strip() != ""
    resolved = (game_version or "retail").strip().lower()
    if classic and explicit and resolved != "classic":
        # --classic is shorthand for --game-version classic. Pairing it with any *other* explicit
        # version — including the retail default spelled out, or a deferred era/SoD string — is a
        # contradiction we refuse rather than silently letting --classic win the routing. (--classic
        # alone leaves game_version=None, so explicit is False and the shorthand still works.)
        raise BlizzardClientError(
            "unsupported_game_version",
            f"--classic conflicts with --game-version {resolved!r}; pass only one "
            "(--classic is shorthand for --game-version classic).",
        )
    if classic:
        resolved = "classic"
    if resolved not in SUPPORTED_GAME_VERSIONS:
        raise BlizzardClientError(
            "unsupported_game_version",
            f"--game-version must be one of: {', '.join(SUPPORTED_GAME_VERSIONS)}; got {resolved!r}. "
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
    # Oceanic realms live in Blizzard's US region, so `oce` routes there like on every other provider.
    region = profile_region(region_input) if region_input and region_input.strip() else DEFAULT_REGION
    if region not in SUPPORTED_REGIONS:
        raise BlizzardClientError(
            "unsupported_region",
            f"--region must be one of: {', '.join(SUPPORTED_REGIONS)}; got {(region_input or '').strip()!r}.",
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
        settings, static_ttl, dynamic_ttl = load_blizzard_cache_settings_from_env()
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._static_ttl = static_ttl
        self._dynamic_ttl = dynamic_ttl
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._http_client: httpx.Client | None = None
        self._access_token: str | None = None
        self._token_region: str | None = None
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = build_client(timeout=self._timeout_seconds)
        return self._http_client

    def _token(self, routing: BlizzardRouting) -> str:
        now = time.time()
        # Tokens are region-scoped (CN uses a different OAuth host from us/eu/kr/tw), so the
        # in-memory cache must match the target region before it can be reused.
        if self._access_token and self._token_region == routing.region and now < self._token_expires_at - TOKEN_SKEW_SECONDS:
            return self._access_token
        token_cache = ClientTokenCache(CLIENT_CREDENTIALS_STATE_PROVIDER, routing.region, self._client_id, self._client_secret)
        cached = token_cache.load(now=now)
        if cached is not None:
            self._access_token, self._token_expires_at = cached
            self._token_region = routing.region
            return self._access_token
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
        token_cache.save(token=token, expires_at=self._token_expires_at)
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

    def _get(
        self, routing: BlizzardRouting, path: str, *, localized: bool = True, ttl_seconds: int | None = None
    ) -> dict[str, Any]:
        """GET one API path, replaying a cached answer first; the token never reaches the cache key.

        ``localized=False`` leaves ``locale`` out, so Blizzard answers every localized string as a
        per-locale dict.
        """
        # Checked before the cache too, so every read without credentials fails the same way.
        if not self.configured:
            raise BlizzardClientError(
                "missing_client_credentials",
                "Blizzard commands need BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET. Set them in "
                ".env.local, the provider env file, or the environment.",
            )
        ttl = ttl_seconds if ttl_seconds is not None else (self._static_ttl if routing.namespace_class == "static" else self._dynamic_ttl)
        params = {"namespace": routing.namespace, **({"locale": routing.locale} if localized else {})}
        key = f"blizzard:{hashlib.sha256(json.dumps([routing.host, path, params], sort_keys=True).encode()).hexdigest()}"
        cached = self._cache_store.get(key) if self._cache_store is not None and ttl else None
        if isinstance(cached, dict) and "payload" in cached:
            return {**cached, "routing": routing}
        token = self._token(routing)
        response = request_with_retries(
            self._client(),
            f"{routing.host}{path}",
            method="GET",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            retry_attempts=self._retry_attempts,
        )
        result = {"payload": self._decode_json(response), "source_url": str(response.request.url)}
        if self._cache_store is not None and ttl:
            self._cache_store.set(key, result, ttl_seconds=ttl)
        return {**result, "routing": routing}

    def _slug_from_realm_index(self, routing: BlizzardRouting, realm: str) -> str | None:
        """Blizzard's slug for a realm typed in any locale (``Ревущий фьорд``, ``아즈샤라``), or ``None``.

        Slugs are always English, but the unlocalized realm index names every realm in every locale.
        A name two realms share (the zh_TW ``閃電之刃``) maps to neither. Character lookups route
        through the profile namespace, which has no index, so this always reads the dynamic one.
        """
        index_routing = (
            routing
            if routing.namespace_class == "dynamic"
            else replace(routing, namespace=f"dynamic-{routing.region}", namespace_class="dynamic")
        )
        realms = self._get(index_routing, "/data/wow/realm/index", localized=False, ttl_seconds=self._static_ttl)["payload"].get("realms")
        wanted = "".join(slug_parts(realm))
        slugs = {
            row["slug"]
            for row in (realms if isinstance(realms, list) else [])
            if isinstance(row, dict)
            and isinstance(row.get("slug"), str)
            and isinstance(row.get("name"), dict)
            and any(isinstance(name, str) and "".join(slug_parts(name)) == wanted for name in row["name"].values())
        }
        return slugs.pop() if len(slugs) == 1 else None

    def _get_realm_scoped(self, routing: BlizzardRouting, realm: str, path_for: Callable[[str], str]) -> dict[str, Any]:
        """GET ``path_for(slug)`` for each slug spelling of ``realm``, moving on only on HTTP 404.

        Blizzard slugs drop apostrophes and keep word breaks (``Mal'Ganis`` -> ``malganis``, ``Tarren
        Mill`` -> ``tarren-mill``), so neither spelling alone covers every realm or every way it is typed.
        Slugs are English, so a native-script name that no spelling finds is looked up in the realm index.
        """
        variants = realm_slug_variants(realm)
        if not variants:
            # An empty slug would GET the realm index and return it as this realm.
            raise BlizzardClientError("invalid_query", f"Realm {realm!r} has no letters or digits to look up.")
        for slug in variants:
            try:
                return self._get(routing, path_for(slug))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404 or (slug == variants[-1] and realm.isascii()):
                    raise
                last_miss = exc
        indexed = self._slug_from_realm_index(routing, realm)
        if indexed is None or indexed in variants:
            raise last_miss
        return self._get(routing, path_for(indexed))

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
        return self._get_realm_scoped(routing, slug, lambda realm_slug: f"/data/wow/realm/{realm_slug}")

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
        if not name.strip():
            raise BlizzardClientError("invalid_query", "Character name must not be blank.")
        routing = resolve_routing(
            region_input=region or self._default_region,
            game_version=game_version,
            classic=classic,
            locale=locale,
            namespace_class="profile",
        )
        return self._get_realm_scoped(routing, realm, lambda realm_slug: f"/profile/wow/character/{realm_slug}/{name.lower()}")


def verification_note(region: str | None = None) -> str:
    """Verification posture: the CN caveat alone for an unconfirmed region, the full summary otherwise."""
    if region is not None and region not in VERIFIED_REGIONS:
        return _UNVERIFIED_CN_NOTE
    return _VERIFIED_NOTE
