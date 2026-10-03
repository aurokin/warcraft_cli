from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

from warcraft_core.cache_ledger import (
    cache_ledger,
    current_cache_ledger,
    record_cache_lookup,
    record_cache_store,
    with_cache_provenance,
)
from warcraft_core.envelope import error_envelope, success_envelope


def _ok(**provenance: object) -> dict[str, object]:
    return dict(success_envelope(provider="p", command="c", kind="k", data={}, provenance=provenance))


def test_recording_without_a_ledger_does_nothing() -> None:
    record_cache_store("file")
    record_cache_lookup("file", hit=True, age_seconds=5, ttl_seconds=60)
    assert current_cache_ledger() is None
    assert with_cache_provenance(_ok(), None) == _ok()


def test_a_scope_that_built_a_store_reports_it_even_without_lookups() -> None:
    with cache_ledger() as ledger:
        record_cache_store("file")
    assert with_cache_provenance(_ok(), ledger)["provenance"] == {
        "cache": {
            "backend": "file",
            "lookups": 0,
            "hits": 0,
            "hit": False,
            "all_hits": False,
            "oldest_hit_age_seconds": None,
            "oldest_hit_ttl_seconds": None,
        }
    }


def test_a_scope_without_a_store_reports_nothing() -> None:
    with cache_ledger() as ledger:
        pass
    assert with_cache_provenance(_ok(source_url="u"), ledger)["provenance"] == {"source_url": "u"}


def test_a_nested_ledger_keeps_its_own_counts_and_adds_them_to_its_parent() -> None:
    with cache_ledger() as outer:
        record_cache_lookup("file", hit=True, age_seconds=10, ttl_seconds=900)
        with cache_ledger() as inner:
            record_cache_lookup("redis", hit=True)
            record_cache_lookup("file", hit=True, age_seconds=300, ttl_seconds=3600)
            record_cache_lookup("file", hit=False)
        assert current_cache_ledger() is outer
    assert current_cache_ledger() is None

    inner_block = inner.provenance()
    assert inner_block is not None
    assert (inner_block["backend"], inner_block["lookups"], inner_block["hits"]) == ("mixed", 3, 2)
    assert (inner_block["oldest_hit_age_seconds"], inner_block["oldest_hit_ttl_seconds"]) == (300, 3600)
    outer_block = outer.provenance()
    assert outer_block is not None
    assert (outer_block["lookups"], outer_block["hits"], outer_block["all_hits"]) == (4, 3, False)
    assert (outer_block["oldest_hit_age_seconds"], outer_block["oldest_hit_ttl_seconds"]) == (300, 3600)


def test_threads_running_in_a_copied_context_record_into_the_same_ledger() -> None:
    # Proves the copied context shares the ledger by reference; the ledger's lock is an invariant
    # this test cannot catch being removed, since the GIL rarely exposes the unlocked race.
    with cache_ledger() as ledger, ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, record_cache_lookup, "file", hit=index % 2 == 0, age_seconds=index, ttl_seconds=600)
            for index in range(400)
        ]
        for future in futures:
            future.result()
    block = ledger.provenance()
    assert block is not None
    assert (block["lookups"], block["hits"], block["oldest_hit_age_seconds"]) == (400, 200, 398)


def test_only_a_success_envelope_without_a_cache_block_is_stamped() -> None:
    with cache_ledger() as ledger:
        record_cache_lookup("file", hit=False)
    failure = dict(error_envelope(provider="p", command="c", code="not_found", message="m"))
    assert with_cache_provenance(failure, ledger) == failure
    provider_block = {"backend": "redis", "lookups": 1, "hits": 1}
    assert with_cache_provenance(_ok(cache=provider_block), ledger)["provenance"]["cache"] == provider_block
