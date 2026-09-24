from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from warcraft_core.expansions import list_expansions
from warcraft_core.expansions import normalize_expansion_key as normalize_expansion_key
from warcraft_core.expansions import resolve_expansion as resolve_shared_expansion

from wowhead_cli.entity_types import PARSER_ENTITY_TYPES

WOWHEAD_ROOT = "https://www.wowhead.com"
NETHER_ROOT = "https://nether.wowhead.com"


@dataclass(frozen=True, slots=True)
class ExpansionProfile:
    key: str
    label: str
    path_prefix: str
    data_env: int
    aliases: tuple[str, ...]
    legacy_subdomains: tuple[str, ...]

    @property
    def wowhead_base(self) -> str:
        if self.path_prefix:
            return f"{WOWHEAD_ROOT}/{self.path_prefix}"
        return WOWHEAD_ROOT

    @property
    def nether_base(self) -> str:
        if self.path_prefix:
            return f"{NETHER_ROOT}/{self.path_prefix}"
        return NETHER_ROOT


# Wowhead-only profile facts keyed by shared expansion key; keys without a Wowhead site (e.g. fresh) are absent.
_DATA_ENV_BY_KEY: dict[str, int] = {
    "retail": 1,
    "classic": 4,
    "tbc": 5,
    "wotlk": 8,
    "cata": 11,
    "mop-classic": 15,
    "ptr": 2,
    "beta": 3,
    "classic-ptr": 14,
}
_LEGACY_SUBDOMAINS_BY_KEY: dict[str, tuple[str, ...]] = {
    "retail": ("wowhead.com", "www.wowhead.com"),
    "classic": ("classic.wowhead.com",),
    "tbc": ("tbc.wowhead.com",),
    "wotlk": ("wotlk.wowhead.com", "wrath.wowhead.com"),
    "cata": ("cata.wowhead.com", "cataclysm.wowhead.com"),
    "mop-classic": ("mists.wowhead.com", "mop.wowhead.com"),
    "ptr": ("ptr.wowhead.com",),
    "beta": ("beta.wowhead.com",),
    "classic-ptr": ("classicptr.wowhead.com",),
}

_PROFILES: tuple[ExpansionProfile, ...] = tuple(
    ExpansionProfile(
        key=expansion.key,
        label=expansion.label,
        path_prefix=expansion.wowhead_path_prefix,
        data_env=_DATA_ENV_BY_KEY[expansion.key],
        aliases=expansion.aliases,
        legacy_subdomains=_LEGACY_SUBDOMAINS_BY_KEY[expansion.key],
    )
    for expansion in list_expansions()
    if expansion.wowhead_path_prefix is not None
)

_BY_KEY = {profile.key: profile for profile in _PROFILES}
_PREFIX_PROFILES: tuple[ExpansionProfile, ...] = tuple(
    sorted((profile for profile in _PROFILES if profile.path_prefix), key=lambda row: len(row.path_prefix), reverse=True)
)

_LEGACY_HOST_TO_PROFILE: dict[str, ExpansionProfile] = {}
for _profile in _PROFILES:
    for _host in _profile.legacy_subdomains:
        _LEGACY_HOST_TO_PROFILE[_host] = _profile

_ENTITY_PATH_RE = re.compile(
    r"""^/(?:(?:[a-z]{2}(?:-[A-Z]{2})?|[a-z0-9-]+)/)*(?P<etype>[a-z-]+)=(?P<eid>\d+)""",
)


def list_profiles() -> tuple[ExpansionProfile, ...]:
    return _PROFILES


def resolve_expansion(value: str | None) -> ExpansionProfile:
    """Resolve a shared expansion key/alias to its Wowhead profile; keys without a Wowhead site are unknown here."""
    try:
        profile = _BY_KEY.get(resolve_shared_expansion(value).key)
    except ValueError:
        profile = None
    if profile is None:
        options = ", ".join(row.key for row in _PROFILES)
        raise ValueError(f"Unknown expansion {value!r}. Supported: {options}")
    return profile


def build_entity_url(profile: ExpansionProfile, entity_type: str, entity_id: int) -> str:
    return f"{profile.wowhead_base}/{entity_type}={entity_id}"


def build_guide_lookup_url(profile: ExpansionProfile, guide_id: int) -> str:
    return f"{profile.wowhead_base}/guide={guide_id}"


def build_search_url(profile: ExpansionProfile, query: str) -> str:
    return f"{profile.wowhead_base}/search?q={quote(query)}"


def build_search_suggestions_url(profile: ExpansionProfile) -> str:
    return f"{profile.wowhead_base}/search/suggestions-template"


def build_comment_replies_url(profile: ExpansionProfile) -> str:
    return f"{profile.wowhead_base}/comment/show-replies"


def build_tooltip_url(profile: ExpansionProfile, entity_type: str, entity_id: int) -> str:
    return f"{profile.nether_base}/tooltip/{entity_type}/{entity_id}"


def build_news_url(profile: ExpansionProfile, *, page: int = 1) -> str:
    url = f"{profile.wowhead_base}/news"
    if page > 1:
        return f"{url}?page={page}"
    return url


def build_blue_tracker_url(profile: ExpansionProfile, *, page: int = 1) -> str:
    url = f"{profile.wowhead_base}/blue-tracker"
    if page > 1:
        return f"{url}?page={page}"
    return url


def build_guide_category_url(profile: ExpansionProfile, category: str) -> str:
    slug = category.strip().strip("/")
    return f"{profile.wowhead_base}/guides/{quote(slug)}"


def normalize_wowhead_url(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("www."):
        text = f"https://{text}"
    elif "wowhead.com" in text and not text.startswith(("http://", "https://")):
        text = f"https://{text.lstrip('/')}"
    if not text.startswith(("http://", "https://")):
        return None
    return text


def is_wowhead_host(hostname: str) -> bool:
    host = hostname.lower()
    return host == "wowhead.com" or host.endswith(".wowhead.com")


def _profile_for_hostname(hostname: str) -> ExpansionProfile | None:
    host = hostname.lower()
    if host in _LEGACY_HOST_TO_PROFILE and host not in {"wowhead.com", "www.wowhead.com"}:
        return _LEGACY_HOST_TO_PROFILE[host]
    return None


def _profile_for_path_prefix(path: str) -> ExpansionProfile | None:
    parts = [part for part in path.split("/") if part]
    if not parts:
        return _BY_KEY["retail"]
    head = parts[0]
    for profile in _PREFIX_PROFILES:
        if head == profile.path_prefix:
            return profile
    return _BY_KEY["retail"]


def detect_expansion_from_url(raw: str) -> ExpansionProfile | None:
    """Infer the Wowhead expansion profile from a Wowhead or legacy subdomain URL."""
    normalized = normalize_wowhead_url(raw)
    if normalized is None:
        return None
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if not is_wowhead_host(host):
        return None

    legacy = _profile_for_hostname(host)
    if legacy is not None:
        return legacy

    return _profile_for_path_prefix(parsed.path)


def parse_entity_from_wowhead_url(raw: str) -> tuple[str, int] | None:
    normalized = normalize_wowhead_url(raw)
    if normalized is None:
        return None
    parsed = urlparse(normalized)
    if not is_wowhead_host(parsed.hostname or ""):
        return None
    match = _ENTITY_PATH_RE.match(parsed.path)
    if match is None:
        return None
    entity_type = match.group("etype")
    if entity_type not in PARSER_ENTITY_TYPES:
        return None
    entity_id = int(match.group("eid"))
    if entity_id <= 0:
        return None
    return entity_type, entity_id


def url_matches_expansion_profile(url: str, profile: ExpansionProfile) -> bool:
    detected = detect_expansion_from_url(url)
    return detected is not None and detected.key == profile.key


def expansion_url_policy_issues(url: str | None, *, profile: ExpansionProfile) -> list[str]:
    if not isinstance(url, str) or not url.strip():
        return []
    if url_matches_expansion_profile(url.strip(), profile):
        return []
    detected = detect_expansion_from_url(url)
    if detected is None:
        return [f"Could not infer expansion from URL {url!r}."]
    return [f"URL targets expansion {detected.key!r} but selected profile is {profile.key!r}."]
