"""Per-invocation record of shared cache lookups, reported as the envelope's ``provenance.cache``.

``warcraft_api.cache`` records every store it builds and every lookup it answers into the ledger
active in the current ``contextvars`` context; with no ledger active, recording does nothing. Each
binary opens one ledger per invocation (``warcraft_core.cli.configure``) and ``emit`` stamps its
summary on the success envelope. The ``warcraft`` wrapper opens a nested ledger per provider call,
so each embedded provider envelope carries its own block; a nested ledger adds its counts to its
parent when it closes, so the wrapper's envelope reports the aggregate.

A ledger is shared by reference, not copied: work handed to a thread pool must run under
``contextvars.copy_context()`` taken in the submitting thread to reach it, and recording takes a
lock so those threads can share it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

CacheBackend = Literal["file", "redis"]


class CacheProvenance(TypedDict):
    """The ``provenance.cache`` block.

    Ages are null when any hit came from Redis, which records no store time, so a mixed aggregate never
    understates how stale the replayed data may be. ``errors`` counts cache reads and writes that never
    reached the backend (Redis down, hung or refusing the credentials); each such lookup is also a miss.
    """

    backend: Literal["file", "redis", "mixed"]  # "mixed" when a wrapper command read both backends
    lookups: int
    hits: int
    hit: bool
    all_hits: bool
    oldest_hit_age_seconds: int | None
    oldest_hit_ttl_seconds: int | None
    errors: int


@dataclass(slots=True)
class CacheLedger:
    backends: set[CacheBackend] = field(default_factory=set)
    lookups: int = 0
    hits: int = 0
    oldest_hit_age_seconds: int | None = None
    oldest_hit_ttl_seconds: int | None = None
    hit_without_age: bool = False
    errors: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _add_oldest(self, age_seconds: int | None, ttl_seconds: int | None) -> None:
        if age_seconds is not None and (self.oldest_hit_age_seconds is None or age_seconds > self.oldest_hit_age_seconds):
            self.oldest_hit_age_seconds, self.oldest_hit_ttl_seconds = age_seconds, ttl_seconds

    def add_store(self, backend: CacheBackend) -> None:
        with self._lock:
            self.backends.add(backend)

    def add_lookup(self, backend: CacheBackend, *, hit: bool, age_seconds: int | None, ttl_seconds: int | None) -> None:
        with self._lock:
            self.backends.add(backend)
            self.lookups += 1
            if hit:
                self.hits += 1
                self.hit_without_age = self.hit_without_age or age_seconds is None
                self._add_oldest(age_seconds, ttl_seconds)

    def add_error(self, backend: CacheBackend) -> None:
        with self._lock:
            self.backends.add(backend)
            self.errors += 1

    def merge(self, other: CacheLedger) -> None:
        with self._lock:
            self.backends |= other.backends
            self.lookups += other.lookups
            self.hits += other.hits
            self.errors += other.errors
            self.hit_without_age = self.hit_without_age or other.hit_without_age
            self._add_oldest(other.oldest_hit_age_seconds, other.oldest_hit_ttl_seconds)

    def provenance(self) -> CacheProvenance | None:
        """The block to report, or ``None`` when this scope built no cache store."""
        if not self.backends:
            return None
        age_known = not self.hit_without_age
        return {
            "backend": next(iter(self.backends)) if len(self.backends) == 1 else "mixed",
            "lookups": self.lookups,
            "hits": self.hits,
            "hit": self.hits > 0,
            "all_hits": self.lookups > 0 and self.hits == self.lookups,
            "oldest_hit_age_seconds": self.oldest_hit_age_seconds if age_known else None,
            "oldest_hit_ttl_seconds": self.oldest_hit_ttl_seconds if age_known else None,
            "errors": self.errors,
        }


_CURRENT: ContextVar[CacheLedger | None] = ContextVar("warcraft_cache_ledger", default=None)


def current_cache_ledger() -> CacheLedger | None:
    return _CURRENT.get()


@contextmanager
def cache_ledger() -> Iterator[CacheLedger]:
    """Record lookups made inside the block; on exit the counts are also added to the enclosing ledger."""
    parent = _CURRENT.get()
    ledger = CacheLedger()
    token = _CURRENT.set(ledger)
    try:
        yield ledger
    finally:
        _CURRENT.reset(token)
        if parent is not None:
            parent.merge(ledger)


def record_cache_store(backend: CacheBackend) -> None:
    ledger = _CURRENT.get()
    if ledger is not None:
        ledger.add_store(backend)


def record_cache_lookup(
    backend: CacheBackend, *, hit: bool, age_seconds: int | None = None, ttl_seconds: int | None = None
) -> None:
    ledger = _CURRENT.get()
    if ledger is not None:
        ledger.add_lookup(backend, hit=hit, age_seconds=age_seconds, ttl_seconds=ttl_seconds)


def record_cache_error(backend: CacheBackend) -> None:
    ledger = _CURRENT.get()
    if ledger is not None:
        ledger.add_error(backend)


def with_cache_provenance(envelope: Mapping[str, Any], ledger: CacheLedger | None) -> dict[str, Any]:
    """``envelope`` with ``provenance.cache`` from ``ledger`` on success; an existing block is kept."""
    payload = dict(envelope)
    block = ledger.provenance() if ledger is not None else None
    provenance = payload.get("provenance")
    if block is None or payload.get("ok") is not True or not isinstance(provenance, dict) or "cache" in provenance:
        return payload
    payload["provenance"] = {**provenance, "cache": block}
    return payload
