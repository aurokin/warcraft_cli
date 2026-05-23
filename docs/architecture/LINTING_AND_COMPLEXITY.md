# Linting And Complexity

Static quality tooling and how to use it for refactors. DX slice (AUR-366) is complete; phase 2–3 refactors remain optional backlog.

## Commands

| Target | Purpose |
|--------|---------|
| `make check` | Lint + typecheck + import boundaries + `test-fast` (local pre-push) |
| `make lint` | Ruff on shared packages (`warcraft-core`, `warcraft-api`, `warcraft-content`) — **must pass** |
| `make lint-all` | Full-repo Ruff report (~500+ issues) — report-only, does not fail |
| `make lint-boundaries` | `import-linter` package boundaries (`.importlinter`) |
| `make test-fast` | `pytest -q -m "not live"` — default CI unit suite |
| `make complexity` | Radon CC + maintainability index on `packages/` |
| `make typecheck` | Mypy on shared packages |
| `make coverage` | Shared-package coverage (`pytest-cov` or stdlib `trace` fallback) |
| `make deadcode` | Vulture report (review before deleting) |
| `make pre-commit-install` | Install optional local hooks (`.pre-commit-config.yaml`) |
| `make benchmark-cache` | Cold vs warm Wowhead search timing (`scripts/benchmark_wowhead_cache.py`) |
| `make fixture-refresh-hints` | URLs/commands to refresh `expansion_recorded.json` |

## Rollout Status

| Phase | Status |
|-------|--------|
| 1 — Tooling targets in Makefile | **Complete** |
| 1b — CI: lint + typecheck + boundaries + non-live tests | **Complete** |
| 1c — Pre-commit, `make check`, fixture catalog/maintenance docs, cache benchmark | **Complete** |
| 2 — Backlog from reports (complexity, duplication, dead code, full-repo Ruff) | **Open** — optional |
| 3 — Refactor slices per provider | **Open** — optional |

Contract fixtures: [CONTRACT_TEST_CATALOG.md](CONTRACT_TEST_CATALOG.md), [FIXTURE_MAINTENANCE.md](FIXTURE_MAINTENANCE.md).

## Phase 2 Backlog (From Reports)

- Highest-complexity functions (especially SimC talent/branch and large CLI handlers)
- Duplicated filter/sampling code (Raider.IO, WowProgress)
- Dead code after provider iteration (verify with `make deadcode`)
- Additional `import-linter` contracts as packages split (see `.importlinter`)
- Per-provider Ruff cleanup via `lint-all`, one package per PR

## Phase 3 Refactor Rules

- One provider family per change
- Extract to shared packages only when repetition is proven
- Strengthen tests on public CLI contracts when splitting `main.py` modules

## Known Hotspots

Large `main.py` entry modules: `wowhead`, `warcraftlogs`, `simc`, `warcraft` wrapper, `raiderio`, `wowprogress`.

## Success Criteria

- Lint fast enough to run on every change
- Complexity reports justify refactors with data
- Shared packages stay typed and bounded
- New shared utilities come from repeated patterns, not speculation

## Local Install

```bash
make dev-deploy-no-link
```

Use `make dev-deploy` only to relink `~/.local/bin` to the current checkout. Worktrees: external `worktrunk`.
