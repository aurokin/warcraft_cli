# Wowhead contract hardening

Breakage detection for Wowhead CLI surfaces (AUR-359).

Operational guidance (rate limits, logging, failure response): [../foundation/OPERATIONAL_BOUNDARIES.md](../foundation/OPERATIONAL_BOUNDARIES.md).

## Performance flags

- `--stream` on `wowhead` emits a JSONL header line plus one `record` line per row for `data.results`, `data.comments`, or `entity-page` linked entities. The header is the envelope with that collection emptied and `data.stream` naming it.
- `--max-concurrency` on `comments --hydrate-missing-replies` caps parallel reply fetches (default 4).
- The HTTP client dedupes identical in-flight URL requests within a single command invocation.

## Local checks

```bash
wowhead doctor              # live endpoint preflight (search, tooltip, entity page)
wowhead doctor --no-live    # cache/runtime only (used by `warcraft doctor`)
pytest -q tests/test_wowhead_schema_snapshots.py
```

## Live checks

```bash
make test-canary
make test-e2e E2E_ARGS="tests/e2e/test_wowhead.py"
```

Pinned parser canaries live in `tests/fixtures/wowhead_canaries.py`. Required JSON keys for core commands are in `tests/fixtures/wowhead_output_schemas.py`.

## CI

`.github/workflows/live-contracts.yml` (weekly schedule plus `workflow_dispatch`):

- Parser canary job (`make test-canary`)
- Keyless end-to-end journeys, including `tests/e2e/test_wowhead.py`

Synthetic schema snapshots need no network and run with the regular unit tests (`make test-fast`).
