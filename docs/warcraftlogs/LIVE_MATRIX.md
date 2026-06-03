# Warcraft Logs live command matrix

Repeatable live integration coverage for typed `warcraftlogs` commands (AUR-319).

## Run locally

```bash
make dev-deploy-no-link
# Client credentials in ~/.config/warcraftlogs or env (see auth docs)
make test-live-matrix
```

Optional private-report cases also need a user OAuth token with `view-private-reports`.

## Fixtures

Pinned inputs live in `tests/fixtures/live_matrix.py`. Rotate the public report code when retail logs age out of retention.

## What it checks

Each matrix row invokes one CLI invocation and asserts:

- `ok` is not false
- `command` and the canonical envelope key from `docs/warcraftlogs/PAYLOAD_KEYS.md`
- Non-empty payload data where the fixture should return rows

Sampled-cohort cases (for example `spec-kill-samples`) assert envelope shape only: a small
spec-filtered sample can legitimately be empty, so they declare no non-empty leaf.

The matrix is separate from `make test-live` so it can be throttled or scheduled without running every provider live suite.

## CI

Not scheduled in GitHub Actions initially (WCL points and runtime). Run locally before release candidates.
