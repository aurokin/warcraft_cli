# Wowhead Expansion Routing Research

Date: 2026-02-19

## Goal

Capture how Wowhead version/expansion routing works so a future `--expansion` flag can be implemented safely across all commands.

## Confirmed Routing Model

Wowhead currently routes by **path prefix** on `www.wowhead.com`, while older subdomains mostly 301 redirect to those prefixes.

Examples:

- `classic.wowhead.com` -> `https://www.wowhead.com/classic`
- `tbc.wowhead.com` -> `https://www.wowhead.com/tbc`
- `wotlk.wowhead.com` / `wrath.wowhead.com` -> `https://www.wowhead.com/wotlk`
- `cata.wowhead.com` / `cataclysm.wowhead.com` -> `https://www.wowhead.com/cata`
- `mists.wowhead.com` / `mop.wowhead.com` -> `https://www.wowhead.com/mop-classic`
- `ptr.wowhead.com` -> `https://www.wowhead.com/ptr`
- `beta.wowhead.com` -> `https://www.wowhead.com/beta`
- `classicptr.wowhead.com` -> `https://www.wowhead.com/classic-ptr`

## Confirmed Expansion -> dataEnv Mapping

From `data.pageMeta` on entity pages:

- `retail` (`/`) -> `env=1`
- `ptr` (`/ptr`) -> `env=2`
- `beta` (`/beta`) -> `env=3`
- `classic` (`/classic`) -> `env=4`
- `tbc` (`/tbc`) -> `env=5`
- `wotlk` (`/wotlk`) -> `env=8`
- `cata` (`/cata`) -> `env=11`
- `classic-ptr` (`/classic-ptr`) -> `env=14`
- `mop-classic` (`/mop-classic`) -> `env=15`

## Endpoint Behavior Notes

- Search suggestions are prefix-aware:
  - `/<prefix>/search/suggestions-template?q=...` works and can return version-specific results.
- Entity pages are prefix-aware:
  - `/<prefix>/<entity>=<id>` works with canonical URLs in that prefix (except PTR pages may canonicalize to retail in some cases).
- Tooltip endpoint supports expansion-aware routing with dataEnv:
  - `https://nether.wowhead.com/<prefix>/tooltip/<type>/<id>?dataEnv=<env>`
- Comment reply endpoint is prefix-aware:
  - `https://www.wowhead.com/<prefix>/comment/show-replies?id=<commentId>`

## URL expansion detection (AUR-362)

- `detect_expansion_from_url(url)` infers a profile from legacy subdomains (`classic.wowhead.com`, …) or path prefixes (`/wotlk/...`).
- `parse_entity_from_wowhead_url(url)` extracts `(entity_type, entity_id)` from entity page URLs.
- When `--expansion` is **omitted**, `search` auto-detects from a Wowhead URL query; `entity` / `entity-page` accept `--url` to override type/id and auto-detect expansion.
- Explicit `--expansion <key>` always wins over URL detection.
- Payloads may include `expansion_source` (`default`, `flag`, `url`) and `notes` when emitted URLs disagree with the selected profile.
- `wowhead doctor` reports `expansion_url_policy` checks for built sample URLs.

## Current Implementation State

- Expansion profiles are now codified in `src/wowhead_cli/expansion_profiles.py`.
- Discovery command exists:
  - `wowhead expansions`
- Global expansion selection is live:
  - `--expansion` is wired through `search`, `entity`, `entity-page`, `comments`, and `compare`.
- `entity` now defaults tooltip `dataEnv` from the selected expansion profile (override still possible via `--data-env`).
- Optional canonical normalization is live:
  - `--normalize-canonical-to-expansion` rewrites canonical entity page URLs to the selected expansion path.
  - default behavior remains unchanged (normalization disabled).
- Recorded fixture integration tests cover profile behavior across commands:
  - `tests/test_expansion_recorded_fixtures.py`
  - fixture dataset: `tests/fixtures/expansion_recorded.json`
- Live endpoint contract checks are available:
  - env-gated live suite: `tests/test_live_integration.py`
  - raw endpoint contract suite: `tests/test_live_endpoint_contracts.py`
  - manual workflow dispatch: `.github/workflows/live-wowhead-contracts.yml`
