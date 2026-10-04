from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.client_credentials import TOKEN_SKEW_SECONDS, ClientTokenCache
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, CachedHttpClient, hashed_cache_key, request_with_retries
from warcraft_core.paths import provider_cache_root
from warcraft_core.timestamps import parse_iso8601_utc
from warcraft_core.wow_normalization import normalize_region, profile_region, realm_slug_variants, slug_parts

from blizzard_api_cli.auth import BlizzardAuthConfig, load_blizzard_auth_config
from blizzard_api_cli.summaries import (
    BRACKET_PATTERN,
    COLLECTION_VIEWS,
    auctions_page,
    auctions_view,
    bracket_names,
    collection_page,
    leaderboard_view,
    pvp_bracket_row,
    pvp_character_summary,
    pvp_rewards_view,
    pvp_season_summary,
)

# Shared-state provider key for the cached client-credentials token.
CLIENT_CREDENTIALS_STATE_PROVIDER = "blizzard-api-client-credentials"

# Blizzard API hosts only exist for these regions. normalize_region also recognizes "oc"/"world",
# which are NOT valid Blizzard hosts, so the resolver validates against this tuple explicitly.
SUPPORTED_REGIONS = ("us", "eu", "kr", "tw", "cn")

# Game versions this slice routes, each with the infix Blizzard puts in its namespaces
# (``static-classic1x-us``). "classic" is the progression Classic line (Mists of Pandaria Classic
# today). All four answer live for Game Data and Profile reads; anything else is rejected rather
# than guessed at.
_NAMESPACE_INFIX = {"retail": "", "classic": "classic-", "classic-era": "classic1x-", "classic-anniversary": "classicann-"}
SUPPORTED_GAME_VERSIONS = tuple(_NAMESPACE_INFIX)

# Character sub-resources `blizzard character --section` reads, each live-confirmed on retail. A PvP
# bracket is any name pvp-summary lists (2v2, 3v3, rbg, shuffle-<class>-<spec>); the pattern keeps
# the value a single path segment so it cannot reach another endpoint with the bearer token.
CHARACTER_SECTIONS = ("pvp-summary", "pvp-bracket/<bracket>", "professions", "collections/mounts", "collections/pets", "collections/toys")
_CHARACTER_SECTION_PATTERN = re.compile(r"pvp-summary|pvp-bracket/[a-z0-9-]+|professions|collections/(?:mounts|pets|toys)")

# Classic pvp-summary links only some of the brackets a character has rated (live 2026-10-04: the
# #1 progression Classic 3v3 player's summary links 2v2 alone), so off retail these are also probed.
_CLASSIC_PVP_BRACKETS = ("2v2", "3v3", "5v5", "rbg")

DEFAULT_LOCALE = "en_US"
DEFAULT_REGION = "us"

# Regions whose API host, OAuth token URL, and namespace strings have been confirmed against live
# Blizzard endpoints (Game Data and Profile, every game version). CN routes through a different
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
    f"{'/'.join(region for region in SUPPORTED_REGIONS if region in VERIFIED_REGIONS)} (Game Data "
    "and Profile, every game version), whose payloads report provenance.verified=true. "
    + _UNVERIFIED_CN_NOTE
)


def load_blizzard_cache_settings_from_env() -> tuple[CacheSettings, int, int, int]:
    """Resolve cache settings plus the static, the dynamic/profile and the snapshot TTLs.

    Static data (items) and indexes change with a patch; realms and character profiles change as
    people play. Snapshots are the hourly dumps (auctions, PvP leaderboards and cutoffs) and the
    large character collections, kept for about as long as Blizzard takes to publish the next one.
    """
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="BLIZZARD",
        # Resolved per call, not at import, so the cache root follows HOME/XDG as they are now.
        default_cache_dir=provider_cache_root("blizzard-api") / "http",
        default_redis_prefix="blizzard_cli",
        # The shared TTL config has no Blizzard-named fields, so three of its slots carry ours.
        ttl_defaults=CacheTTLConfig(search_suggestions=86400, entity_response=900, page_html=3600),
        ttl_env_overrides={
            "search_suggestions": "BLIZZARD_STATIC_CACHE_TTL_SECONDS",
            "entity_response": "BLIZZARD_DYNAMIC_CACHE_TTL_SECONDS",
            "page_html": "BLIZZARD_SNAPSHOT_CACHE_TTL_SECONDS",
        },
    )
    return settings, settings.ttls.search_suggestions, settings.ttls.entity_response, settings.ttls.page_html


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
        # version — including the retail default spelled out or classic-era — is a
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
            f"--game-version must be one of: {', '.join(SUPPORTED_GAME_VERSIONS)}; got {resolved!r}.",
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
    namespace = f"{namespace_class}-{_NAMESPACE_INFIX[resolved_version]}{region}"
    return BlizzardRouting(
        region=region,
        host=_api_host(region),
        oauth_token_url=_oauth_token_url(region),
        namespace=namespace,
        namespace_class=namespace_class,
        game_version=resolved_version,
        locale=(locale or DEFAULT_LOCALE),
    )


class BlizzardClient(CachedHttpClient):
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
        settings, static_ttl, dynamic_ttl, snapshot_ttl = load_blizzard_cache_settings_from_env()
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._static_ttl = static_ttl
        self._dynamic_ttl = dynamic_ttl
        self._snapshot_ttl = snapshot_ttl
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._access_token: str | None = None
        self._token_region: str | None = None
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

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
        self,
        routing: BlizzardRouting,
        path: str,
        *,
        localized: bool = True,
        ttl_seconds: int | None = None,
        reduce: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """GET one API path, replaying a cached answer first; the token never reaches the cache key.

        ``localized=False`` leaves ``locale`` out, so Blizzard answers every localized string as a
        per-locale dict. ``reduce`` shrinks the body before it is cached and is part of the key, so
        a multi-megabyte dump is parsed once per TTL and replayed in its compact form.
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
        # A reduced view gets its own key; the unreduced key stays what it has always been.
        key_parts: list[Any] = [routing.host, path, params, *([reduce.__name__] if reduce is not None else [])]
        key = hashed_cache_key("blizzard", json.dumps(key_parts, sort_keys=True).encode())
        cached = self._read_cache(key) if ttl else None
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
        payload = self._decode_json(response)
        result = {
            "payload": reduce(payload) if reduce is not None else payload,
            "source_url": str(response.request.url),
            "last_modified": _http_date_to_iso(response.headers.get("Last-Modified")),
        }
        if ttl:
            self._write_cache(key, result, ttl_seconds=ttl)
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
            else replace(routing, namespace=f"dynamic-{_NAMESPACE_INFIX[routing.game_version]}{routing.region}", namespace_class="dynamic")
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

    def _get_realm_scoped(
        self, routing: BlizzardRouting, realm: str, path_for: Callable[[str], str], **get_options: Any
    ) -> dict[str, Any]:
        """GET ``path_for(slug)`` for each slug spelling of ``realm``, moving on only on HTTP 404.

        Blizzard slugs drop apostrophes and keep word breaks (``Mal'Ganis`` -> ``malganis``, ``Tarren
        Mill`` -> ``tarren-mill``), so neither spelling alone covers every realm or every way it is typed.
        Slugs are English, so a native-script name that no spelling finds is looked up in the realm index.
        The answer's ``realm_slug`` is the spelling that worked, for follow-up reads on the same realm.
        """
        variants = realm_slug_variants(realm)
        if not variants:
            # An empty slug would GET the realm index and return it as this realm.
            raise BlizzardClientError("invalid_query", f"Realm {realm!r} has no letters or digits to look up.")
        for slug in variants:
            try:
                return {**self._get(routing, path_for(slug), **get_options), "realm_slug": slug}
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404 or (slug == variants[-1] and realm.isascii()):
                    raise
                last_miss = exc
        indexed = self._slug_from_realm_index(routing, realm)
        if indexed is None or indexed in variants:
            raise last_miss
        return {**self._get(routing, path_for(indexed), **get_options), "realm_slug": indexed}

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
        section: str | None = None,
    ) -> dict[str, Any]:
        character = _character_segment(name)
        suffix = f"/{section}" if section else ""
        if section is not None and not _CHARACTER_SECTION_PATTERN.fullmatch(section):
            raise BlizzardClientError(
                "invalid_query", f"--section must be one of: {', '.join(CHARACTER_SECTIONS)}; got {section!r}."
            )
        routing = resolve_routing(
            region_input=region or self._default_region,
            game_version=game_version,
            classic=classic,
            locale=locale,
            namespace_class="profile",
        )
        return self._get_realm_scoped(routing, realm, lambda realm_slug: f"/profile/wow/character/{realm_slug}/{character}{suffix}")

    def _route(self, namespace_class: str, routing_options: Mapping[str, Any]) -> BlizzardRouting:
        """Routing for one read from the shared --region/--game-version/--classic/--locale values."""
        return resolve_routing(
            region_input=routing_options.get("region") or self._default_region,
            game_version=routing_options.get("game_version"),
            classic=bool(routing_options.get("classic")),
            locale=routing_options.get("locale"),
            namespace_class=namespace_class,
        )

    def _season_id(self, routing: BlizzardRouting, season: int | None) -> tuple[int, dict[str, Any]]:
        """``season``, or the current PvP season the season index names, plus that index."""
        index = self._get(routing, "/data/wow/pvp-season/index", ttl_seconds=self._static_ttl)
        if season is not None:
            return season, index
        current = index["payload"].get("current_season")
        current_id = current.get("id") if isinstance(current, dict) else None
        if not isinstance(current_id, int):
            raise BlizzardClientError("invalid_response", "The Blizzard PvP season index names no current season.")
        return current_id, index

    def fetch_pvp_season(self, season: int | None = None, **routing_options: Any) -> dict[str, Any]:
        """One PvP season (the current one by default): name, start, bracket names and title cutoffs."""
        routing = self._route("dynamic", routing_options)
        season_id, index = self._season_id(routing, season)
        base = f"/data/wow/pvp-season/{season_id}"
        detail = self._get(routing, base, ttl_seconds=self._static_ttl)
        boards = self._get(routing, f"{base}/pvp-leaderboard/index", ttl_seconds=self._static_ttl)
        # Cutoffs move daily while a season runs, so they are a snapshot, not an index.
        rewards = self._get(routing, f"{base}/pvp-reward/index", ttl_seconds=self._snapshot_ttl, reduce=pvp_rewards_view)
        payload = {
            "season_id": season_id,
            **pvp_season_summary(index["payload"], detail["payload"], boards["payload"]),
            **rewards["payload"],
            "freshness": snapshot_freshness(rewards.get("last_modified")),
        }
        return {"payload": payload, "source_url": detail["source_url"], "routing": routing}

    def fetch_pvp_leaderboard(self, bracket: str, *, season: int | None = None, limit: int, **routing_options: Any) -> dict[str, Any]:
        """The top ``limit`` rows of one PvP leaderboard (current season by default)."""
        bracket = bracket.strip().lower()
        if not BRACKET_PATTERN.fullmatch(bracket):
            raise BlizzardClientError("invalid_query", f"Bracket {bracket!r} is not a leaderboard name such as 3v3 or shuffle-overall.")
        routing = self._route("dynamic", routing_options)
        season_id, _ = self._season_id(routing, season)
        board = self._get(
            routing,
            f"/data/wow/pvp-season/{season_id}/pvp-leaderboard/{bracket}",
            localized=False,
            ttl_seconds=self._snapshot_ttl,
            reduce=leaderboard_view,
        )
        entries = board["payload"]["entries"]
        payload = {
            **{key: value for key, value in board["payload"].items() if key != "entries"},
            "total_entries": len(entries),
            "returned": min(len(entries), limit),
            "truncated": len(entries) > limit,
            "entries": entries[:limit],
            "freshness": snapshot_freshness(board.get("last_modified")),
        }
        return {"payload": payload, "source_url": board["source_url"], "routing": routing}

    def fetch_pvp_character(self, realm: str, name: str, **routing_options: Any) -> dict[str, Any]:
        """A character's honor, battleground record and every rated bracket it has (see _CLASSIC_PVP_BRACKETS)."""
        character = _character_segment(name)
        routing = self._route("profile", routing_options)
        summary = self._get_realm_scoped(routing, realm, lambda slug: f"/profile/wow/character/{slug}/{character}/pvp-summary")
        base = f"/profile/wow/character/{summary['realm_slug']}/{character}"
        linked = bracket_names(summary["payload"])
        brackets = [pvp_bracket_row(bracket, self._get(routing, f"{base}/pvp-bracket/{bracket}")["payload"]) for bracket in linked]
        if routing.game_version != "retail":
            for bracket in (name for name in _CLASSIC_PVP_BRACKETS if name not in linked):
                try:
                    payload = self._get(routing, f"{base}/pvp-bracket/{bracket}")["payload"]
                except httpx.HTTPStatusError as exc:
                    # 404 is a bracket the character never played.
                    if exc.response.status_code != 404:
                        raise
                    continue
                brackets.append(pvp_bracket_row(bracket, payload))
        return {**summary, "payload": pvp_character_summary(summary["payload"], brackets)}

    def fetch_collections(
        self, realm: str, name: str, *, kinds: Sequence[str], match: str | None, limit: int, **routing_options: Any
    ) -> dict[str, Any]:
        """Counts plus a filtered, limited list for each collection kind in ``kinds``."""
        character = _character_segment(name)
        routing = self._route("profile", routing_options)
        first, *rest = kinds
        head = self._get_realm_scoped(
            routing,
            realm,
            lambda slug: f"/profile/wow/character/{slug}/{character}/collections/{first}",
            reduce=COLLECTION_VIEWS[first],
            ttl_seconds=self._snapshot_ttl,
        )
        # The first read settled the realm's slug spelling; the rest go straight to it.
        base = f"/profile/wow/character/{head['realm_slug']}/{character}/collections"
        views = {first: head["payload"]} | {
            kind: self._get(routing, f"{base}/{kind}", reduce=COLLECTION_VIEWS[kind], ttl_seconds=self._snapshot_ttl)["payload"]
            for kind in rest
        }
        payload = {
            # The collection bodies do not name the character, so this echoes the realm slug that answered.
            "character": {"name": name, "realm": head["realm_slug"]},
            "collections": {kind: collection_page(view, match=match, limit=limit) for kind, view in views.items()},
        }
        return {**head, "payload": payload}

    def fetch_auctions(self, realm: str, *, item_ids: Sequence[int], limit: int, **routing_options: Any) -> dict[str, Any]:
        """The connected realm's auction house for ``realm``, summarized per item id."""
        routing = self._route("dynamic", routing_options)
        record = self._get_realm_scoped(routing, realm, lambda slug: f"/data/wow/realm/{slug}")["payload"]
        match = re.search(r"/connected-realm/(\d+)", str((record.get("connected_realm") or {}).get("href") or ""))
        if match is None:
            raise BlizzardClientError("invalid_response", f"The Blizzard realm record for {realm!r} names no connected realm.")
        connected_realm_id = int(match.group(1))
        result = self._get(
            routing,
            f"/data/wow/connected-realm/{connected_realm_id}/auctions",
            localized=False,
            ttl_seconds=self._snapshot_ttl,
            reduce=auctions_view,
        )
        payload = {
            "realm": record.get("slug"),
            "connected_realm_id": connected_realm_id,
            **auctions_page(result["payload"], item_ids=item_ids, limit=limit),
            "freshness": snapshot_freshness(result.get("last_modified")),
        }
        return {**result, "payload": payload}

    def fetch_commodities(self, *, item_ids: Sequence[int], limit: int, **routing_options: Any) -> dict[str, Any]:
        """The region-wide commodity market (retail only), summarized per item id."""
        routing = self._route("dynamic", routing_options)
        if routing.game_version != "retail":
            # Live 2026-10-04: dynamic-classic-us answers this path with `_links` only, and Classic realm
            # auction houses carry only quantity-1 non-commodity listings, so no Classic trade-good prices exist.
            raise BlizzardClientError(
                "unsupported_game_version",
                f"Blizzard's API publishes no commodity (stackable trade goods) prices on {routing.game_version}: "
                "the region-wide market is empty there and realm auction houses list non-commodity items only. "
                "Commodity prices are retail-only.",
            )
        result = self._get(routing, "/data/wow/auctions/commodities", localized=False, ttl_seconds=self._snapshot_ttl, reduce=auctions_view)
        payload = {
            **auctions_page(result["payload"], item_ids=item_ids, limit=limit),
            "freshness": snapshot_freshness(result.get("last_modified")),
        }
        return {**result, "payload": payload}


def _character_segment(name: str) -> str:
    """``name`` as one URL path segment, so a slash, ? or # cannot reach another endpoint with the token."""
    # A name of only dots would be a dot segment that climbs the path, and must not be blank.
    if not name.strip(" ."):
        raise BlizzardClientError("invalid_query", "Character name must not be blank.")
    return quote(name.lower(), safe="")


def _http_date_to_iso(value: str | None) -> str | None:
    """An HTTP date header (``Sun, 4 Oct 2026 05:24:03 GMT``) as ISO 8601 UTC, or ``None``."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def snapshot_freshness(last_modified: str | None) -> dict[str, Any]:
    """When Blizzard last rebuilt a snapshot (its ``Last-Modified``) and how old that is right now.

    The age is computed on every read, so a cached replay reports how stale the data really is.
    """
    parsed = parse_iso8601_utc(last_modified)
    age = max(0, int((datetime.now(UTC) - parsed).total_seconds())) if parsed is not None else None
    return {"last_modified": last_modified, "age_seconds": age}


def verification_note(region: str | None = None) -> str:
    """Verification posture: the CN caveat alone for an unconfirmed region, the full summary otherwise."""
    if region is not None and region not in VERIFIED_REGIONS:
        return _UNVERIFIED_CN_NOTE
    return _VERIFIED_NOTE
