# Fixture maintenance

How to refresh pinned contract data when Wowhead pages or Warcraft Logs reports age out.

## When to refresh

- Parser canary or live matrix test skips or fails with HTTP 404
- `wowhead doctor` live probes fail for a pinned expansion profile
- Schema snapshot tests fail after an intentional CLI output change (update fixture or schema keys)

## Wowhead parser canaries

1. Pick a replacement entity on the target expansion (same entity type).
2. Update `tests/fixtures/wowhead_canaries.py` (`entity_id`, `label`).
3. Run:

```bash
WOWHEAD_LIVE_TESTS=1 pytest -q tests/test_wowhead_parser_canaries.py
```

## Wowhead `expansion_recorded.json`

Recorded transport responses for offline schema tests.

1. Choose one stable item (default: item 19019) per expansion profile.
2. Capture live responses for search, tooltip, entity-page HTML, and comment replies using the profile's URLs (see `tests/test_expansion_recorded_fixtures.py` for expected endpoints).
3. Update `tests/fixtures/expansion_recorded.json` preserving keys: `query`, `profiles`, `search_result`, `tooltip`, `comment`, `reply_thread`.
4. Verify:

```bash
pytest -q tests/test_expansion_recorded_fixtures.py tests/test_wowhead_schema_snapshots.py
```

Optional helper (prints guidance only):

```bash
make fixture-refresh-hints WOWHEAD_EXPANSION=retail WOWHEAD_ITEM_ID=19019
```

## Warcraft Logs live matrix

1. Find a public report with fights at the desired difficulty.
2. Update `tests/fixtures/live_matrix.py` (`PUBLIC_REPORT_CODE`, fight difficulty constants, zone/encounter ids as needed).
3. For private-report rows, update `PRIVATE_REPORT_CODE` only when you have user auth with `view-private-reports`.
4. Run:

```bash
make test-live-matrix
```

## After any fixture change

- Run `pytest -q -m "not live"` for fast regression.
- Note the rotation in the PR or commit message (report codes and entity ids go stale over time).

## Related

- [CONTRACT_TEST_CATALOG.md](CONTRACT_TEST_CATALOG.md)
