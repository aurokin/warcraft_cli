# Wowhead contract hardening

Breakage detection for Wowhead CLI surfaces (AUR-359).

## Performance flags

- `--stream` on `wowhead` emits a JSONL header line plus one `record` line per row for `search.results`, `comments`, or `entity-page` linked entities.
- `--max-concurrency` on `comments --hydrate-missing-replies` caps parallel reply fetches (default 4).
- The HTTP client dedupes identical in-flight URL requests within a single command invocation.

## Local checks

```bash
wowhead doctor              # live endpoint preflight (search, tooltip, entity page)
wowhead doctor --no-live    # cache/runtime only (used by `warcraft doctor`)
pytest -q tests/test_wowhead_schema_snapshots.py
```

## Live suites

```bash
WOWHEAD_LIVE_TESTS=1 pytest -q tests/test_live_endpoint_contracts.py
WOWHEAD_LIVE_TESTS=1 pytest -q tests/test_wowhead_parser_canaries.py
```

Pinned parser canaries live in `tests/fixtures/wowhead_canaries.py`. Required JSON keys for core commands are in `tests/fixtures/wowhead_output_schemas.py`.

## CI

`.github/workflows/live-wowhead-contracts.yml` (`workflow_dispatch`):

- Recorded schema snapshots (no network)
- Matrix live jobs per expansion profile
- Parser canary job
