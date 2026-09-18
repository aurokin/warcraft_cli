# Documentation Map

Use this directory as a map, not a dumping ground.

Documentation ownership:
- Repo-wide product philosophy and representative workflows belong in [foundation/PRODUCT_PRINCIPLES.md](foundation/PRODUCT_PRINCIPLES.md).
- Repo-wide analytics and comparison safety rules belong in [foundation/SAFE_ANALYTICS_RULES.md](foundation/SAFE_ANALYTICS_RULES.md).
- Repo-wide operational boundaries (rate limits, logging, failure response) belong in [foundation/OPERATIONAL_BOUNDARIES.md](foundation/OPERATIONAL_BOUNDARIES.md).
- Shared cross-provider contracts belong in focused contract docs such as [foundation/ERROR_CONTRACT.md](foundation/ERROR_CONTRACT.md), [foundation/IDENTITY_CONTRACT.md](foundation/IDENTITY_CONTRACT.md), and [foundation/WRAPPER_PROVIDER_CONTRACT.md](foundation/WRAPPER_PROVIDER_CONTRACT.md).
- [ROADMAP.md](ROADMAP.md) tracks provider tiers, what is next, and deferred candidates; open work is in [Linear — Warcraft CLI](https://linear.app/aurokin/project/warcraft-cli-a9a133da0d88); shipped work is in [../CHANGELOG.md](../CHANGELOG.md).
- [USAGE.md](USAGE.md) carries workflows and cross-provider conventions; per-command flags live in the generated [reference/](reference/README.md).
- Each CLI has its own folder under `docs/` with a `README.md` entrypoint.

## Folder Layout

- `foundation/`: repo-wide principles and shared contracts
- `architecture/`: packaging, auth, expansion filtering, linting, and completed-work history
- `reference/`: generated per-command flag reference, one file per binary (`make reference`; never hand-edited)
- `warcraft/`, `wowhead/`, `method/`, `icy-veins/`, `raiderio/`, `warcraft-wiki/`, `warcraftlogs/`, `simc/`, `blizzard-api/`, `raidbots/`, `curseforge/`, `lorrgs/`: CLI-specific docs
- root docs:
  - [ROADMAP.md](ROADMAP.md)
  - [USAGE.md](USAGE.md)
  - [RELEASE.md](RELEASE.md)
  - this file

## Start Here

- [USAGE.md](USAGE.md)
- [reference/README.md](reference/README.md)
- [foundation/ERROR_CONTRACT.md](foundation/ERROR_CONTRACT.md)
- [foundation/PRODUCT_PRINCIPLES.md](foundation/PRODUCT_PRINCIPLES.md)
- [foundation/SAFE_ANALYTICS_RULES.md](foundation/SAFE_ANALYTICS_RULES.md)
- [ROADMAP.md](ROADMAP.md)

## Command Reference

Generated from the Typer apps by `make reference`; a test fails when it drifts from the code.

- [reference/README.md](reference/README.md) — index of every binary, by tier

## Foundations

- [foundation/PRODUCT_PRINCIPLES.md](foundation/PRODUCT_PRINCIPLES.md)
- [foundation/ERROR_CONTRACT.md](foundation/ERROR_CONTRACT.md)
- [foundation/SAFE_ANALYTICS_RULES.md](foundation/SAFE_ANALYTICS_RULES.md)
- [foundation/OPERATIONAL_BOUNDARIES.md](foundation/OPERATIONAL_BOUNDARIES.md)
- [foundation/IDENTITY_CONTRACT.md](foundation/IDENTITY_CONTRACT.md)
- [foundation/WRAPPER_PROVIDER_CONTRACT.md](foundation/WRAPPER_PROVIDER_CONTRACT.md)

## Architecture And Shared References

- [ROADMAP.md](ROADMAP.md) — tiers, next work, and deferred candidates (open work in Linear)
- [architecture/README.md](architecture/README.md) — progressive index
- [architecture/REPO_STRUCTURE_AND_PACKAGING.md](architecture/REPO_STRUCTURE_AND_PACKAGING.md)
- [architecture/PACKAGE_LAYOUT.md](architecture/PACKAGE_LAYOUT.md)
- [architecture/AUTH_ARCHITECTURE.md](architecture/AUTH_ARCHITECTURE.md)
- [architecture/EXPANSION_FILTERING.md](architecture/EXPANSION_FILTERING.md)
- [architecture/LINTING_AND_COMPLEXITY.md](architecture/LINTING_AND_COMPLEXITY.md)
- [architecture/CONTRACT_TEST_CATALOG.md](architecture/CONTRACT_TEST_CATALOG.md)
- [architecture/FIXTURE_MAINTENANCE.md](architecture/FIXTURE_MAINTENANCE.md)
- [architecture/history/README.md](architecture/history/README.md) — completed milestones

Engineering backlog: [Linear — Warcraft CLI](https://linear.app/aurokin/project/warcraft-cli-a9a133da0d88).

## Provider CLI Docs

- [warcraft/README.md](warcraft/README.md)
- [wowhead/README.md](wowhead/README.md)
- [method/README.md](method/README.md)
- [icy-veins/README.md](icy-veins/README.md)
- [raiderio/README.md](raiderio/README.md)
- [warcraft-wiki/README.md](warcraft-wiki/README.md)
- [warcraftlogs/README.md](warcraftlogs/README.md)
- [warcraftlogs/SCOPING.md](warcraftlogs/SCOPING.md)
- [warcraftlogs/CACHING.md](warcraftlogs/CACHING.md)
- [simc/README.md](simc/README.md)
- [raidbots/README.md](raidbots/README.md)
- [blizzard-api/README.md](blizzard-api/README.md)
- [curseforge/README.md](curseforge/README.md)
- [lorrgs/README.md](lorrgs/README.md)

Deferred provider candidates (RaidPlan, Undermine Exchange) live in [ROADMAP.md](ROADMAP.md#deferred-candidates).

## Usage And Research

- [USAGE.md](USAGE.md)
- [wowhead/ACCESS_METHODS.md](wowhead/ACCESS_METHODS.md)
- [wowhead/EXPANSION_RESEARCH.md](wowhead/EXPANSION_RESEARCH.md)

## Releases

- [../CHANGELOG.md](../CHANGELOG.md)
- [RELEASE.md](RELEASE.md)
