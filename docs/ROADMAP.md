# Roadmap

This file is the sequencing document for the repo.

Use other docs for stable reference material:
- product philosophy: [PRODUCT_PRINCIPLES.md](foundation/PRODUCT_PRINCIPLES.md)
- analytics and comparison safety rules: [SAFE_ANALYTICS_RULES.md](foundation/SAFE_ANALYTICS_RULES.md)
- shared identity semantics: [IDENTITY_CONTRACT.md](foundation/IDENTITY_CONTRACT.md)
- wrapper boundary: [WRAPPER_PROVIDER_CONTRACT.md](foundation/WRAPPER_PROVIDER_CONTRACT.md)
- architecture and package boundaries: [architecture/README.md](architecture/README.md)
- provider-specific behavior and boundaries: `docs/<cli>/README.md`

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

## Now

Highest-value work:
1. Continue tightening the root wrapper.
   - improve `warcraftlogs` routing beyond explicit report references
   - keep wrapper `doctor`, search, resolve, passthrough, and ranking policy aligned with actual provider capability
   - preserve provider registration status instead of overstating readiness
2. Continue shared identity and cross-provider handoff work.
   - class/spec/build identity
   - encounter identity
   - ability identity
   - report-actor identity
   - guide/build/log crosswalks only where the source contract allows them
3. Continue comparison and evidence surfaces.
   - additive guide comparison
   - exact-build handoff into `simc`
   - consistent freshness, citations, and scope metadata for sampled, merged, cached, and derived outputs
4. Continue safe Warcraft Logs growth.
   - explicit-scope encounter primitives
   - sampled cohort analytics
   - deeper report/detail coverage where the public contract is stable
   - user-auth expansion only after the public/report contract is tighter

Recently completed:
- worktree-local data/cache isolation with shared config/state credentials
- removal of the repo-owned stable deploy, rollback, skill export, and worktree creation workflow

Operational note:
- worktree creation and trunk hygiene are owned outside this repo by `worktrunk`

## Provider Priorities

### Warcraft Logs

Near-term:
- better wrapper routing
- stronger public `character-rankings` reliability
- broader report-detail coverage where the API contract is stable
- deeper explicit-scope analytics before any broader segmentation work
- finished-report caching and derived-output trust metadata

Deferred:
- wave and phase segmentation
- classic/fresh site-profile routing
- wrapper-level user-auth routing

See [warcraftlogs/README.md](warcraftlogs/README.md).

### Blizzard API

Next major provider when auth-heavy official data is the priority.

Focus:
- canonical game-data and profile workflows
- OAuth architecture validation alongside `warcraftlogs`
- region and namespace-aware API patterns

See [blizzard-api/README.md](blizzard-api/README.md).

### Raider.IO

Continue:
- deeper sample-backed analytics
- clearer season-aware leaderboard workflows
- richer normalized run/profile snapshots where the source supports them

See [raiderio/README.md](raiderio/README.md).

### WowProgress

Continue:
- deeper sample-backed analytics
- easier guild snapshot/history/rank workflows
- reliability and normalization improvements around ranking/profile slices

See [wowprogress/README.md](wowprogress/README.md).

### Wowhead

Maintain the current boundary:
- continue only on straightforward structured extraction
- do not push `dressing-room` or `profiler` into reverse-engineering work without an explicit product decision

See [wowhead/README.md](wowhead/README.md).

### Raidbots

Workflow-oriented companion to `simc`, not a driver of shared auth or canonical gameplay truth.

There is no public API for sim submission. The viable path is report consumption and local bridging:
- fetch and parse completed reports (public, stable)
- extract SimC input from reports and hand off to local `simc` analysis
- generate ready-to-paste SimC input locally as the handoff to Raidbots cloud execution

Submission automation is deferred indefinitely unless Raidbots exposes a sanctioned API.

See [raidbots/README.md](raidbots/README.md).

## Later

Lower-priority provider candidates:
- `undermine-exchange`
- `raidplan`
- `curseforge`

Revisit only when the current wrapper, official API, and evidence-oriented analytics work is in a stronger place.

## Sequencing Rules

- Keep roadmap sequencing here.
- Keep open engineering backlog items in [Linear — Warcraft CLI](https://linear.app/aurokin/project/warcraft-cli-a9a133da0d88).
- Keep repo-wide rules out of this file unless they directly affect sequencing.
- Keep stable architecture and package-boundary reference in [architecture/README.md](architecture/README.md).
- Keep provider behavior, current boundaries, and command-specific detail in the provider CLI docs.
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
- roadmap sequencing stays here, while open backlog, stable rules, and provider behavior stay in their own systems or docs
