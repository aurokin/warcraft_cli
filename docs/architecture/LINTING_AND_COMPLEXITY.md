# Linting And Complexity

Static quality tooling and how to use it for refactors. Open phase 2–3 work: Linear [AUR-366](https://linear.app/aurokin/issue/AUR-366/monorepo-tooling-ci-and-developer-experience).

## Commands

| Target | Purpose |
|--------|---------|
| `make lint` | Ruff on shared packages (`warcraft-core`, `warcraft-api`, `warcraft-content`) — **must pass** |
| `make lint-all` | Full-repo Ruff report (~500+ issues) — report-only, does not fail |
| `make complexity` | Radon CC + maintainability index on `packages/` |
| `make typecheck` | Mypy on shared packages |
| `make coverage` | Shared-package coverage (`pytest-cov` or stdlib `trace` fallback) |
| `make deadcode` | Vulture report (review before deleting) |

## Rollout Status

| Phase | Status |
|-------|--------|
| 1 — Tooling targets in Makefile | **Complete** |
| 1b — CI: `make lint` + `make typecheck` on PRs (`.github/workflows/ci.yml`) | **Complete** |
| 2 — Backlog from reports (complexity, duplication, dead code, boundaries) | **Open** — track in Linear |
| 3 — Refactor slices per provider | **Open** |

## Phase 2 Backlog (From Reports)

- Highest-complexity functions (especially SimC talent/branch and large CLI handlers)
- Duplicated filter/sampling code (Raider.IO, WowProgress)
- Dead code after provider iteration (verify with `make deadcode`)
- `import-linter` package boundary contracts (not yet added)
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
