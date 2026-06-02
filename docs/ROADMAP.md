# Roadmap

Sequencing and status for the repo. **Open engineering work lives in [Linear — Warcraft CLI](https://linear.app/aurokin/project/warcraft-cli-a9a133da0d88)** — not duplicated here.

Stable reference material:
- product philosophy: [PRODUCT_PRINCIPLES.md](foundation/PRODUCT_PRINCIPLES.md)
- analytics and comparison safety rules: [SAFE_ANALYTICS_RULES.md](foundation/SAFE_ANALYTICS_RULES.md)
- shared identity semantics: [IDENTITY_CONTRACT.md](foundation/IDENTITY_CONTRACT.md)
- wrapper boundary: [WRAPPER_PROVIDER_CONTRACT.md](foundation/WRAPPER_PROVIDER_CONTRACT.md)
- architecture and package boundaries: [architecture/README.md](architecture/README.md)
- provider-specific behavior: `docs/<cli>/README.md`

## Goal

Grow the repo as a Warcraft data monorepo with:
- individually runnable provider CLIs
- shared libraries only where the behavior is genuinely shared
- a root `warcraft` wrapper for routing and orchestration
- agent-friendly outputs that preserve source identity and trust boundaries

## Current State

Working now:
- shared packages: `warcraft-core`, `warcraft-api`, `warcraft-content`
- root wrapper: `warcraft`
- provider CLIs: `wowhead`, `method`, `icy-veins`, `raiderio`, `warcraft-wiki`, `wowprogress`, `warcraftlogs`, `simc`
- root `warcraft` skill

Validated shared systems:
- output and error shaping
- cache and HTTP infrastructure
- bundle export/load/query scaffolding
- wrapper routing and provider passthrough
- article bundle and guide-comparison primitives
- wrapper expansion filtering and provider metadata
- ranking policy for wrapper discovery
- sample-backed analytics direction for profile and leaderboard providers
- worktree-local data/cache isolation with shared config/state credentials
- DX: `make check`, CI lint/typecheck/boundaries/tests, full-repo Ruff, contract fixture catalog

## Priority Order

Work in this order unless a dependency or incident says otherwise:

| Priority | Issue | Theme |
|----------|-------|--------|
| Hygiene (optional) | [AUR-382](https://linear.app/aurokin/issue/AUR-382) | Phase 3 complexity refactors (E/F extractions) |
| 1 | [AUR-384](https://linear.app/aurokin/issue/AUR-384) | Wrapper routing, doctor, provider registration |
| 2 | [AUR-385](https://linear.app/aurokin/issue/AUR-385) | Shared identity and cross-provider handoffs |
| 3 | [AUR-386](https://linear.app/aurokin/issue/AUR-386) | Guide comparison, evidence metadata, SimC handoff |
| 4 | [AUR-387](https://linear.app/aurokin/issue/AUR-387) | WCL character-rankings, report coverage, explicit-scope analytics |
| 5 | [AUR-388](https://linear.app/aurokin/issue/AUR-388) | WCL finished-report caching and derived-output trust metadata |
| 6 | [AUR-389](https://linear.app/aurokin/issue/AUR-389) | Expansion filtering deferred (simc, WCL classic/fresh) |
| 7 | [AUR-390](https://linear.app/aurokin/issue/AUR-390) | Blizzard API provider bootstrap |
| 8 | [AUR-391](https://linear.app/aurokin/issue/AUR-391) / [AUR-392](https://linear.app/aurokin/issue/AUR-392) | Raider.IO and WowProgress analytics depth |
| 9 | [AUR-393](https://linear.app/aurokin/issue/AUR-393) | Raidbots report consumption and SimC handoff |
| — | [AUR-394](https://linear.app/aurokin/issue/AUR-394) | Wowhead scoped-extraction policy (track, don’t expand blindly) |
| — | [AUR-395](https://linear.app/aurokin/issue/AUR-395) | Provider candidate decisions — CurseForge **GO** (→ AUR-499), Undermine + RaidPlan **DEFER** |
| 10 | [AUR-499](https://linear.app/aurokin/issue/AUR-499) | CurseForge provider scaffold (doctor + addon lookup) |

Refactors (AUR-382) can run in parallel with product work when they are behavior-preserving extractions; prefer product issues above when choosing the next PR.

## Provider Docs

Command behavior and boundaries stay in provider READMEs — use Linear issues above for *what to build next*:

| Provider | Doc |
|----------|-----|
| Warcraft Logs | [warcraftlogs/README.md](warcraftlogs/README.md) |
| Blizzard API | [blizzard-api/README.md](blizzard-api/README.md) |
| Raider.IO | [raiderio/README.md](raiderio/README.md) |
| WowProgress | [wowprogress/README.md](wowprogress/README.md) |
| Wowhead | [wowhead/README.md](wowhead/README.md) |
| Raidbots | [raidbots/README.md](raidbots/README.md) |
| Wrapper | [warcraft/README.md](warcraft/README.md) |

## Recently Completed

Tracked in Linear (Done) and `CHANGELOG.md` `[Unreleased]`:

- FUTURE_TASKS migration (Wowhead, monorepo ergonomics, expansion filtering phase 3)
- AUR-366 DX tooling, CI, Ruff phase 2; warcraftlogs boss-kills slice (PR #31)
- WCL payload key parity, live command matrix, graphql passthrough, typed buff previews
- Worktree-local data/cache isolation; removal of repo-owned deploy/skill-export workflow

Operational note: worktree creation and trunk hygiene are owned outside this repo by `worktrunk`.

## Sequencing Rules

- Keep **priority order** and status narrative here; keep **open tasks** in Linear.
- Keep repo-wide rules in foundation/architecture docs unless they directly affect sequencing.
- Only extract shared code after a second provider proves the abstraction is real.
- Prefer feature delivery, reliability, and trust metadata over broadening auth-heavy surfaces too early.

## Risks

- over-generalizing too early
- hiding source differences behind fake shared schemas
- pushing too much logic into the root wrapper
- adding broad analytics semantics before the source contract is strong enough
- letting documentation drift away from the actual CLI surfaces

## Success Criteria

- agents can start from the root skill and reach the right provider quickly
- each provider remains independently runnable and testable
- shared code stays genuinely shared
- the wrapper improves discovery without erasing provenance
- roadmap sequencing stays here; open backlog stays in Linear; stable rules and provider behavior stay in their own docs
