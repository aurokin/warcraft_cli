# Architecture Docs

Progressive index for repo structure, shared design, and completed work. Sequencing and near-term priorities live in [ROADMAP.md](../ROADMAP.md). Open engineering work is tracked in [Linear — Warcraft CLI](https://linear.app/aurokin/project/warcraft-cli-a9a133da0d88).

## Start Here

- [REPO_STRUCTURE_AND_PACKAGING.md](REPO_STRUCTURE_AND_PACKAGING.md) — monorepo shape, install model, shared package responsibilities, tooling
- [PACKAGE_LAYOUT.md](PACKAGE_LAYOUT.md) — the 16 packages, console scripts, tiers, dependency direction
- [../foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md) — the envelope, exit codes, and global flags every binary shares
- [../foundation/WRAPPER_PROVIDER_CONTRACT.md](../foundation/WRAPPER_PROVIDER_CONTRACT.md) — required provider surfaces for the wrapper
- [../reference/README.md](../reference/README.md) — generated per-CLI command reference

## Active Reference

- [AUTH_ARCHITECTURE.md](AUTH_ARCHITECTURE.md) — shared auth classes, rollout status, provider posture
- [EXPANSION_FILTERING.md](EXPANSION_FILTERING.md) — wrapper `--expansion` behavior and provider modes
- [LINTING_AND_COMPLEXITY.md](LINTING_AND_COMPLEXITY.md) — static quality tooling and the gates in `make check`
- [CONTRACT_TEST_CATALOG.md](CONTRACT_TEST_CATALOG.md) — pinned parser/matrix/schema inputs and cross-provider contract tests
- [FIXTURE_MAINTENANCE.md](FIXTURE_MAINTENANCE.md) — how to refresh synthetic and captured fixtures

## Completed Work (History)

Shipped milestones and design records that should not be mistaken for open plans:
[history/README.md](history/README.md).

## Provider Candidates

Providers that were evaluated and deferred are listed under "Deferred candidates" in
[ROADMAP.md](../ROADMAP.md), with the conditions that would re-open each one.
