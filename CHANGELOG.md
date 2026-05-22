# Changelog

All notable changes to this repo are recorded here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The workspace ships under a single version — every `pyproject.toml` in the monorepo bumps together.

Add user-visible changes to `[Unreleased]` in the same PR that ships them. See [docs/RELEASE.md](docs/RELEASE.md) for the release flow.

## [Unreleased]

### Added

- Shared output ergonomics in `warcraft_core.output`: field projection, `--fields-strict`, `--compact-max-chars`, output profiles (`agent|human|debug`), and `DiagnosticsCollector` for `diagnostics` metadata.
- Wowhead global flags: `--profile`, `--fields-strict`, and `--compact-max-chars` (wired through the shared output layer).
- GitHub Actions CI (`.github/workflows/ci.yml`) runs `make lint` and `make typecheck` on pull requests and pushes to `main`.
- Warcraft Logs live command matrix: `tests/fixtures/live_matrix.py`, `tests/test_live_command_matrix.py`, and `make test-live-matrix` (see `docs/warcraftlogs/LIVE_MATRIX.md`).
- Wrapper expansion policy review: `warcraft-wiki` is `fixed` to `retail` (phase 3 complete; `simc` remains deferred).
- Wowhead contract hardening: `wowhead doctor`, parser canaries (`tests/fixtures/wowhead_canaries.py`), schema snapshot tests, and expanded `.github/workflows/live-wowhead-contracts.yml` matrix jobs.
- Wowhead request efficiency: per-command session HTTP dedupe, `--stream` JSONL output for large arrays, and `--max-concurrency` for parallel comment reply hydration.
- Import boundary checks via `import-linter` (`make lint-boundaries`, CI job) enforcing provider isolation and core layering.
- `warcraftlogs graphql` now provides a raw official GraphQL passthrough with literal/file/stdin query input, JSON variables, declared-variable scoping helpers, explicit endpoint selection, opt-in caching, introspection, and the same `graphql_warnings` envelope used by typed Warcraft Logs commands.
- `report-encounter-buffs` now returns a typed `buffs.preview` payload with per-row `source` (or `target` when `--view-by target`), `aura` (with identity contract), and reported buff-table fields (`reported_total_uptime`, `reported_total_uses`, `reported_bands`), parallel to `report-encounter-casts`. New `--preview-limit` flag (default 20, max 200) bounds the preview length; `buffs.total` and `buffs.preview_truncated` mirror the casts contract so agents can compose follow-ups against either sibling command without learning two output shapes.

### Changed

- WowProgress CLI: split the monolithic `main.py` into `context`, `identity`, `search`, and `analytics` modules; `main.py` now holds Typer wiring and command handlers only.
- Warcraft Logs typed commands emit a canonical command-based top-level envelope key (for example `boss_kills`, `report_encounter_buffs`). Legacy primary keys remain dual-emitted for one minor with `deprecated_keys` metadata. See `docs/warcraftlogs/PAYLOAD_KEYS.md`.
- Architecture docs use progressive disclosure: active reference under `docs/architecture/`, completed milestones under `docs/architecture/history/`. Open engineering work moved to Linear (Warcraft CLI project).
- `simc` repo resolution no longer falls back to a machine-specific default path; unset installs use the managed checkout path with `source: unset` until `simc checkout` or `--set-root`.

### Fixed

### Removed

### Deprecated

## [0.3.0] - 2026-05-08

### Added

- `view-private-reports` scope is now first-class alongside `view-user-profile`. `auth login`, `auth pkce-login`, `auth status`, `auth token`, and `doctor` decode the access token's JWT body to surface `scopes.has_view_private_reports`, and `scope_warning` walks you through what to re-auth for. WCL's token response omits the granted-scope list, so reading the JWT is the only reliable way to know what the user actually has.
- `report-events` now appends a hint when a narrowed slice still resolves to `events.data: null`, pointing at the `--data-type` flag (`casts`, `damage-done`, `healing`, …) rather than surfacing a bare null.

### Changed

- Every WCL command surfaces partial-error context as `notes` and `graphql_warnings` in JSON output via a centralized `_emit(client=client)` path.
- `GuildAttendance` no longer selects `Zone.frozen` — that field was nulling out for real guilds (e.g. `gn / us-malganis`) and tripping the partial-error path. Pagination, rosters, and zone identity continue to flow.

### Fixed

- Private and guild-stealth typed encounter analytics now return populated rows. `v0.2.0` routed authenticated traffic to `/api/v2/user`, but the parsers still expected the legacy `report.table.entries` shape; live WCL responses use `report.table.data.entries` (and `data.auras` for buffs). `report-encounter-damage-source-summary`, `report-encounter-damage-target-summary`, `report-encounter-damage-breakdown`, and `report-encounter-aura-summary` were silently returning zero rows on real reports despite passing the fixtures. The parser now drills into the `data` wrapper.
- WCL `{ "data": ..., "errors": [...] }` partials are tolerated. The client uses a recursive useful-data check: if any leaf value (or non-empty list) is populated at any depth, the response flows through with the error list captured as `last_warnings`. If every leaf is null, the original GraphQL error is raised so users see the real failure rather than a misleading `not_found`.
- `null` GraphQL variables are pruned at the wire. WCL rejects `null` variable values with `Internal server error` instead of treating them as omitted, so `character-rankings ... --spec-name <slug>` and similar flag-driven calls were 500'ing whenever an optional flag was unset alongside a set one. Cache keys use the pruned variable set so cache identity stays stable across `null`/omitted callers.

### Upgrading

```bash
pip install -U -e '.[dev]'
warcraftlogs auth login \
  --scope view-user-profile \
  --scope view-private-reports \
  --redirect-uri http://localhost:8787/callback
```

Existing tokens issued without `view-private-reports` continue to work for public queries; `auth status` reports the missing scope.

## [0.2.0] - 2026-05-06

### Added

- `view-user-profile` scope is now first-class. `auth login`, `auth pkce-login`, `auth token`, `auth status`, and `doctor` expose `scopes.granted`, `scopes.requested`, `scopes.has_view_user_profile`, and a `scope_warning` when the scope is missing. WCL requires this scope to establish a user identity; without it, `userData.currentUser` resolves to null and ACL checks fall through to anonymous.

### Changed

- WCL routes report and guild GraphQL queries through `/api/v2/user` whenever a non-expired user token is saved. Previously every query went to `/api/v2/client` with the client-credentials token, so private and guild-stealth reports returned "permission denied" no matter how many times the user re-authenticated. Cache payloads are namespaced by endpoint family so client- and user-API responses can never collide on disk.
- Public-API probes stay pinned to the client endpoint via a new `force_client=True` opt-out on `rate_limit()` and `probe_live_public_api()`. Without this, broken client credentials could be masked by a working user token.

### Fixed

- `load_provider_auth_state` returns `None` on `OSError` / `JSONDecodeError`, matching the contract `provider_auth_status` already followed. Corrupt or hand-edited state files no longer crash the CLI.
- `auth token` only emits `scope_warning` when there's actually a saved user token; fresh setup, logged-out, and corrupt-state cases now report `scope_warning: null`.

### Upgrading

```bash
pip install -U -e '.[dev]'
warcraftlogs auth login \
  --scope view-user-profile \
  --redirect-uri http://localhost:8787/callback
```

Existing tokens issued without `view-user-profile` continue to work for public queries; `auth status` reports the missing scope.

[Unreleased]: https://github.com/aurokin/warcraft_cli/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/aurokin/warcraft_cli/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/aurokin/warcraft_cli/releases/tag/v0.2.0
