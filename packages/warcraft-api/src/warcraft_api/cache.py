from __future__ import annotations

import importlib
import json
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypedDict

from warcraft_core.cache_ledger import record_cache_error, record_cache_lookup, record_cache_store
from warcraft_core.paths import provider_cache_root

DEFAULT_CACHE_PREFIX = "wowhead_cli"

# Short Redis timeouts: a cache that answers slower than this costs more than it saves, and redis-py 5
# would otherwise wait forever on a hung server.
_REDIS_CONNECT_TIMEOUT_SECONDS = 1.0
_REDIS_SOCKET_TIMEOUT_SECONDS = 2.0

# Redis URLs that failed once in this process. Later operations skip them, so a down or hung Redis
# costs one timeout per invocation (the ``warcraft`` wrapper included) instead of one per lookup.
_FAILED_REDIS_URLS: set[str] = set()


@dataclass(frozen=True, slots=True)
class CacheTTLConfig:
    search_suggestions: int = 900
    tooltip_meta: int = 3600
    entity_page_html: int = 3600
    guide_page_html: int = 3600
    page_html: int = 3600
    comment_replies: int = 1800
    entity_response: int = 3600
    # Warcraft Logs finished-report TTL (24h). Other providers leave this at the default.
    report_finished: int = 86400


@dataclass(frozen=True, slots=True)
class CacheSettings:
    enabled: bool
    backend: str
    cache_dir: Path
    redis_url: str | None
    prefix: str
    ttls: CacheTTLConfig


class CacheStore(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, payload: Any, *, ttl_seconds: int) -> None: ...


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0.")
    return value


def default_cache_root() -> Path:
    return provider_cache_root("wowhead")


def default_http_cache_dir() -> Path:
    return default_cache_root() / "http"


def load_prefixed_cache_settings_from_env(
    *,
    env_prefix: str,
    default_cache_dir: Path,
    default_redis_prefix: str,
    ttl_env_overrides: dict[str, str] | None = None,
    ttl_defaults: CacheTTLConfig | None = None,
) -> CacheSettings:
    backend_var = f"{env_prefix}_CACHE_BACKEND"
    cache_dir_var = f"{env_prefix}_CACHE_DIR"
    redis_prefix_var = f"{env_prefix}_REDIS_PREFIX"
    redis_url_var = f"{env_prefix}_REDIS_URL"

    backend = os.getenv(backend_var, "file").strip().lower()
    if backend in {"none", "off", "disabled"}:
        enabled = False
        backend = "file"
    elif backend in {"file", "redis"}:
        enabled = True
    else:
        raise ValueError(f"{backend_var} must be one of: file, redis, none.")

    cache_dir = Path(os.getenv(cache_dir_var, str(default_cache_dir))).expanduser()
    prefix = os.getenv(redis_prefix_var, default_redis_prefix).strip()
    if not prefix:
        raise ValueError(f"{redis_prefix_var} cannot be empty.")

    redis_url = os.getenv(redis_url_var)
    if redis_url is not None:
        redis_url = redis_url.strip() or None
    if enabled and backend == "redis" and redis_url is None:
        raise ValueError(f"{redis_url_var} is required when {backend_var}=redis.")

    defaults = ttl_defaults if ttl_defaults is not None else CacheTTLConfig()
    ttl_values = {
        field_name: getattr(defaults, field_name)
        for field_name in defaults.__dataclass_fields__
    }
    for field_name, env_name in (ttl_env_overrides or {}).items():
        if field_name not in ttl_values:
            raise ValueError(f"Unknown cache TTL field: {field_name}")
        ttl_values[field_name] = _env_int(env_name, ttl_values[field_name])

    return CacheSettings(
        enabled=enabled,
        backend=backend,
        cache_dir=cache_dir,
        redis_url=redis_url,
        prefix=prefix,
        ttls=CacheTTLConfig(**ttl_values),
    )


def load_cache_settings_from_env() -> CacheSettings:
    return load_prefixed_cache_settings_from_env(
        env_prefix="WOWHEAD",
        default_cache_dir=default_http_cache_dir(),
        default_redis_prefix=DEFAULT_CACHE_PREFIX,
        ttl_env_overrides={
            "search_suggestions": "WOWHEAD_SEARCH_CACHE_TTL_SECONDS",
            "tooltip_meta": "WOWHEAD_TOOLTIP_CACHE_TTL_SECONDS",
            "entity_page_html": "WOWHEAD_ENTITY_PAGE_CACHE_TTL_SECONDS",
            "guide_page_html": "WOWHEAD_GUIDE_PAGE_CACHE_TTL_SECONDS",
            "page_html": "WOWHEAD_PAGE_CACHE_TTL_SECONDS",
            "comment_replies": "WOWHEAD_COMMENT_REPLIES_CACHE_TTL_SECONDS",
            "entity_response": "WOWHEAD_ENTITY_CACHE_TTL_SECONDS",
        },
    )


class FileCacheStore:
    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir.expanduser()
        record_cache_store("file")

    def _path_for_key(self, key: str) -> Path:
        parts = [part for part in key.split(":") if part]
        if not parts:
            parts = ["cache"]
        return self._cache_dir.joinpath(*parts).with_suffix(".json")

    def get(self, key: str) -> Any | None:
        """The live payload under ``key``, recorded in the cache ledger as a hit or a miss.

        A hit's age comes from the entry file's mtime: ``set`` replaces the file atomically, so the
        mtime is when the entry was stored, and its TTL is ``expires_at`` minus that time.
        """
        entry = self._read(self._path_for_key(key))
        if entry is None or entry[0] is None:
            record_cache_lookup("file", hit=False)
            return None
        payload, stored_at, expires_at = entry
        record_cache_lookup(
            "file",
            hit=True,
            age_seconds=max(0, int(time.time() - stored_at)),
            ttl_seconds=max(0, round(expires_at - stored_at)),
        )
        return payload

    @staticmethod
    def _read(path: Path) -> tuple[Any, float, float] | None:
        """``(payload, mtime, expires_at)`` of a live entry, or ``None`` for a missing, broken or expired one."""
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            stored_at = path.stat().st_mtime
        except Exception:
            with suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        if not isinstance(data, dict):
            return None
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            return None
        if expires_at <= time.time():
            with suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        return data.get("payload"), stored_at, float(expires_at)

    def set(self, key: str, payload: Any, *, ttl_seconds: int) -> None:
        """Store ``payload`` through a temp file unique to this writer, so concurrent writers of one key never tear it.

        A failed write is dropped (the cache never fails a command) and leaves no temp file behind.
        """
        try:
            path = self._path_for_key(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"expires_at": time.time() + ttl_seconds, "payload": payload}, separators=(",", ":"))
            # A random name rather than mkstemp, so the entry keeps the umask's mode instead of 0600.
            temp = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
            try:
                temp.write_text(data, encoding="utf-8")
                os.replace(temp, path)
            except BaseException:
                with suppress(OSError):
                    temp.unlink(missing_ok=True)
                raise
        except Exception:
            return


def _build_redis_client(
    redis_url: str,
    *,
    import_module_func: Any = importlib.import_module,
) -> Any:
    if not redis_url:
        raise ValueError("A Redis URL is required for the redis cache backend.")
    try:
        redis_module = import_module_func("redis")
    except ImportError as exc:
        raise ValueError(
            "The redis cache backend needs the redis extra: install warcraft[redis] (or the redis package)."
        ) from exc
    try:
        return redis_module.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=_REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        # redis-py's message can quote part of the userinfo (a bad port is read from it), so only the
        # redacted URL is reported.
        raise ValueError(
            f"Invalid Redis URL {redacted_redis_url(redis_url)}: expected redis://, rediss:// or unix:// "
            "with a numeric port and database."
        ) from exc


class RedisCacheStore:
    def __init__(
        self,
        *,
        redis_url: str,
        prefix: str,
        import_module_func: Any = importlib.import_module,
    ) -> None:
        self._client = _build_redis_client(redis_url, import_module_func=import_module_func)
        self._redis_url = redis_url
        self._prefix = prefix
        record_cache_store("redis")

    def _redis_key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    def _run(self, operation: str, *args: Any, **kwargs: Any) -> Any | None:
        """The client ``operation``'s result, or ``None`` when Redis failed now or earlier in this process.

        A failure is recorded in the cache ledger (``provenance.cache.errors``) so an unreachable Redis
        is not mistaken for a cold cache, and trips the per-process breaker in ``_FAILED_REDIS_URLS``.
        """
        if self._redis_url not in _FAILED_REDIS_URLS:
            try:
                return getattr(self._client, operation)(*args, **kwargs)
            except Exception:
                _FAILED_REDIS_URLS.add(self._redis_url)
        record_cache_error("redis")
        return None

    def get(self, key: str) -> Any | None:
        """The payload under ``key``, recorded in the cache ledger; Redis keeps no store time, so a hit has no age."""
        payload = self._read(key)
        record_cache_lookup("redis", hit=payload is not None)
        return payload

    def _read(self, key: str) -> Any | None:
        raw = self._run("get", self._redis_key(key))
        if raw in (None, ""):
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def set(self, key: str, payload: Any, *, ttl_seconds: int) -> None:
        # File entries written with zero TTL immediately expire, including an older value of this key.
        if ttl_seconds <= 0:
            self._run("delete", self._redis_key(key))
            return
        # A payload JSON cannot hold is dropped, as in FileCacheStore: the cache never fails a command.
        with suppress(TypeError, ValueError):
            self._run("set", self._redis_key(key), json.dumps(payload, separators=(",", ":")), ex=ttl_seconds)


class CacheBackendHealth(TypedDict):
    available: bool
    error: str | None


def cache_backend_health(
    settings: CacheSettings,
    *,
    import_module_func: Any = importlib.import_module,
) -> CacheBackendHealth:
    """Whether the configured cache backend answers, for every provider's ``doctor``.

    A Redis backend is pinged, so a server that is down, refuses the credentials, has a bad URL or
    lacks the redis extra reports ``available: false`` with the reason; a doctor then reports
    ``degraded``. The file backend and a disabled cache need no probe.
    """
    if not settings.enabled or settings.backend != "redis":
        return {"available": True, "error": None}
    try:
        _build_redis_client(settings.redis_url or "", import_module_func=import_module_func).ping()
    except Exception as exc:
        return {"available": False, "error": str(exc) or type(exc).__name__}
    return {"available": True, "error": None}


def build_cache_store(settings: CacheSettings) -> CacheStore | None:
    if not settings.enabled:
        return None
    if settings.backend == "file":
        return FileCacheStore(settings.cache_dir)
    if settings.backend == "redis":
        return RedisCacheStore(redis_url=settings.redis_url or "", prefix=settings.prefix)
    raise ValueError(f"Unsupported cache backend: {settings.backend}")


def _file_cache_namespace(cache_dir: Path, path: Path) -> str:
    relative = path.relative_to(cache_dir)
    if len(relative.parts) > 1:
        return relative.parts[0]
    stem = relative.stem
    if len(stem) == 64 and all(ch in "0123456789abcdef" for ch in stem.lower()):
        return "legacy_unscoped"
    return stem


def _iter_file_cache_entries(cache_dir: Path) -> list[dict[str, Any]]:
    root = cache_dir.expanduser()
    if not root.exists() or not root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    now = time.time()
    for path in sorted(root.rglob("*.json")):
        if not path.is_file():
            continue
        status = "invalid"
        expires_at: float | None = None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            raw_expires_at = payload.get("expires_at")
            if isinstance(raw_expires_at, (int, float)):
                expires_at = float(raw_expires_at)
                status = "expired" if expires_at <= now else "active"
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            modified_at = None
        entries.append(
            {
                "path": path,
                "namespace": _file_cache_namespace(root, path),
                "status": status,
                "expires_at": expires_at,
                "modified_at": modified_at,
            }
        )
    return entries


def _ordered_file_counts(*, total: int, active: int, expired: int, invalid: int) -> dict[str, int]:
    return {
        "active": active,
        "expired": expired,
        "invalid": invalid,
        "total": total,
    }


def inspect_file_cache(cache_dir: Path) -> dict[str, Any]:
    root = cache_dir.expanduser()
    entries = _iter_file_cache_entries(root)
    namespaces: dict[str, dict[str, int]] = {}
    totals = {"total": 0, "active": 0, "expired": 0, "invalid": 0}
    modified_values = [float(entry["modified_at"]) for entry in entries if isinstance(entry.get("modified_at"), (int, float))]
    for entry in entries:
        namespace = entry["namespace"]
        status = entry["status"]
        row = namespaces.setdefault(namespace, {"total": 0, "active": 0, "expired": 0, "invalid": 0})
        row["total"] += 1
        totals["total"] += 1
        if status in {"active", "expired", "invalid"}:
            row[status] += 1
            totals[status] += 1
    payload = {
        "kind": "file",
        "root": str(root),
        "exists": root.exists(),
        "totals": _ordered_file_counts(**totals),
        "namespaces": {
            namespace: _ordered_file_counts(**counts)
            for namespace, counts in sorted(namespaces.items())
        },
    }
    if modified_values:
        now = time.time()
        oldest = min(modified_values)
        newest = max(modified_values)
        payload["age_summary"] = {
            "oldest_entry_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(oldest)),
            "oldest_entry_age_hours": round(max(0.0, now - oldest) / 3600, 2),
            "newest_entry_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(newest)),
            "newest_entry_age_hours": round(max(0.0, now - newest) / 3600, 2),
        }
    return payload


def clear_file_cache(
    cache_dir: Path,
    *,
    namespaces: tuple[str, ...] = (),
    expired_only: bool = False,
) -> dict[str, Any]:
    selected = set(namespaces)
    removed_by_namespace: dict[str, int] = {}
    removed_total = 0
    for entry in _iter_file_cache_entries(cache_dir.expanduser()):
        namespace = entry["namespace"]
        if selected and namespace not in selected:
            continue
        if expired_only and entry["status"] != "expired":
            continue
        path = entry["path"]
        try:
            path.unlink()
        except OSError:
            continue
        removed_total += 1
        removed_by_namespace[namespace] = removed_by_namespace.get(namespace, 0) + 1
    return {
        "total": removed_total,
        "namespaces": dict(sorted(removed_by_namespace.items())),
    }


def _redis_iter_keys(client: Any, pattern: str) -> list[str]:
    return [str(key) for key in client.scan_iter(match=pattern)]


def inspect_redis_cache(
    redis_url: str | None,
    *,
    prefix: str,
    include_prefix_visibility: bool = False,
    prefix_limit: int = 10,
    import_module_func: Any = importlib.import_module,
) -> dict[str, Any]:
    if not redis_url:
        return {
            "kind": "redis",
            "available": False,
            "count": 0,
            "namespaces": {},
            "error": "A Redis URL is required for the redis cache backend.",
        }
    try:
        client = _build_redis_client(redis_url, import_module_func=import_module_func)
        keys = _redis_iter_keys(client, f"{prefix}:*")
        # Read in the same try: a Redis that drops between the two scans is unavailable, not a crash.
        all_keys = _redis_iter_keys(client, "*") if include_prefix_visibility else []
    except Exception as exc:
        return {
            "kind": "redis",
            "available": False,
            "count": 0,
            "namespaces": {},
            "error": str(exc),
        }
    namespaces: dict[str, int] = {}
    for key in keys:
        raw = key[len(prefix) + 1:] if key.startswith(f"{prefix}:") else key
        namespace = raw.split(":", 1)[0] if raw else "cache"
        namespaces[namespace] = namespaces.get(namespace, 0) + 1
    summary = {
        "kind": "redis",
        "available": True,
        "count": len(keys),
        "namespaces": dict(sorted(namespaces.items())),
        "error": None,
    }
    if not include_prefix_visibility:
        return summary

    prefix_counts: dict[str, int] = {}
    for key in all_keys:
        key_prefix = key.split(":", 1)[0] if ":" in key else key
        prefix_counts[key_prefix] = prefix_counts.get(key_prefix, 0) + 1
    ordered_prefixes = sorted(prefix_counts.items(), key=lambda item: (-item[1], item[0]))
    other_prefix_count = sum(count for name, count in ordered_prefixes if name != prefix)
    summary["prefix_visibility"] = {
        "current_prefix": prefix,
        "current_prefix_count": prefix_counts.get(prefix, 0),
        "other_prefix_count": other_prefix_count,
        "other_prefixes_present": other_prefix_count > 0,
        "isolated": other_prefix_count == 0,
        "total_prefixes": len(prefix_counts),
        "prefixes": [
            {
                "prefix": name,
                "count": count,
                "current": name == prefix,
            }
            for name, count in ordered_prefixes[:prefix_limit]
        ],
        "truncated": len(ordered_prefixes) > prefix_limit,
    }
    return summary


def clear_redis_cache(
    redis_url: str | None,
    *,
    prefix: str,
    namespaces: tuple[str, ...] = (),
    import_module_func: Any = importlib.import_module,
) -> dict[str, Any]:
    client = _build_redis_client(redis_url or "", import_module_func=import_module_func)
    removed_by_namespace: dict[str, int] = {}
    patterns = [f"{prefix}:{namespace}:*" for namespace in namespaces] if namespaces else [f"{prefix}:*"]
    seen: set[str] = set()
    for pattern in patterns:
        for key in _redis_iter_keys(client, pattern):
            if key in seen:
                continue
            seen.add(key)
            raw = key[len(prefix) + 1:] if key.startswith(f"{prefix}:") else key
            namespace = raw.split(":", 1)[0] if raw else "cache"
            deleted = client.delete(key)
            if deleted:
                removed_by_namespace[namespace] = removed_by_namespace.get(namespace, 0) + int(deleted)
    return {
        "total": sum(removed_by_namespace.values()),
        "namespaces": dict(sorted(removed_by_namespace.items())),
    }


def redacted_redis_url(url: str | None) -> str | None:
    """The Redis URL without its credentials or query string, for doctor and cache output.

    Agents keep that output in their context and logs, so a password must never reach it. Everything up
    to the last '@' is hidden, so a password holding '@', '[' or an unescaped '/' (which redis-py would
    misread as the path) is hidden whole; no URL parser is involved, so no password character can make
    this raise.
    """
    if url is None:
        return None
    scheme, sep, rest = url.partition("://")
    if not sep:
        return "***"
    _, at, location = rest.rpartition("@")
    location = location.split("?", 1)[0].split("#", 1)[0]
    return f"{scheme}://{'***@' if at else ''}{location}"
