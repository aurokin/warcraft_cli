# Architecture Docs

Progressive index for repo structure, shared design, and completed work. Sequencing and near-term priorities live in [ROADMAP.md](../ROADMAP.md). Open engineering work is tracked in [Linear — Warcraft CLI](https://linear.app/aurokin/project/warcraft-cli-a9a133da0d88).

## Start Here

- [REPO_STRUCTURE_AND_PACKAGING.md](REPO_STRUCTURE_AND_PACKAGING.md) — monorepo shape, wrapper boundaries, language policy
- [PACKAGE_LAYOUT.md](PACKAGE_LAYOUT.md) — package naming, directory layout, dependency direction
- [../foundation/WRAPPER_PROVIDER_CONTRACT.md](../foundation/WRAPPER_PROVIDER_CONTRACT.md) — required provider surfaces for the wrapper

## Active Reference

- [AUTH_ARCHITECTURE.md](AUTH_ARCHITECTURE.md) — shared auth classes, rollout status, provider posture
- [EXPANSION_FILTERING.md](EXPANSION_FILTERING.md) — wrapper `--expansion` behavior and provider modes
- [LINTING_AND_COMPLEXITY.md](LINTING_AND_COMPLEXITY.md) — static quality tooling and refactor signals

## Completed Work (History)

Shipped milestones and design decisions that should not be mistaken for open plans:

- [history/README.md](history/README.md) — index
- [history/MONOREPO_MIGRATION.md](history/MONOREPO_MIGRATION.md) — initial monorepo extraction and Method rollout
- [history/ENCOUNTER_RANKINGS.md](history/ENCOUNTER_RANKINGS.md) — Warcraft Logs `encounter-rankings` vs sampled boss analytics

## Future Providers (Docs Only)

These folders document intent only; no CLI package exists yet:

- [../blizzard-api/README.md](../blizzard-api/README.md)
- [../raidbots/README.md](../raidbots/README.md)
- [../undermine-exchange/README.md](../undermine-exchange/README.md)
- [../raidplan/README.md](../raidplan/README.md)
- [../curseforge/README.md](../curseforge/README.md)
