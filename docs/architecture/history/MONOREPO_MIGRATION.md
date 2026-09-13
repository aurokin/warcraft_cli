# Monorepo Migration (Completed)

Historical record of the migration from a Wowhead-only CLI to the current monorepo shape. Completed milestones; do not use as an execution checklist.

## Outcome

- Shared packages: `warcraft-core`, `warcraft-api`, `warcraft-content`
- Root wrapper: `warcraft` with `search`, `resolve`, `doctor`, and provider passthrough
- Provider CLIs remain independently runnable
- Pre-migration stable tag: `v0.1.0`

## Milestone 1 (Completed)

- Created package directories for shared libs, wrapper, `wowhead-cli`, and stubbed `method-cli`
- Moved shared infrastructure (output, cache, HTTP, bundles) without changing Wowhead user-visible behavior
- Added `skills/warcraft/SKILL.md` with progressive disclosure
- Verification: unit tests, live tests when enabled, smoke workflows for Wowhead feature areas

## Milestone 2 (Completed)

- Replaced Method stubs with sitemap-backed `search` / `resolve`, guide fetch, `guide-full`, bundle export/query
- Validated article abstractions before moving them into `warcraft-content`

## Milestone 3 (Completed)

Warcraft Logs was the last provider whose source directory shipped only through the root package.
It now has its own `packages/warcraftlogs-cli/pyproject.toml` with a `warcraftlogs` console script,
which closed the last gap between "documented as independently installable" and "actually
installable". `tests/test_warcraft_cli_packaging.py` asserts the package metadata, and the CI
`isolated-install` job installs it on its own and runs its `--help`.

Every provider that landed afterwards (raidbots, blizzard-api, curseforge, lorrgs) shipped with a
package-local `pyproject.toml` from the start.

## Extraction Rules (Still Valid)

**Moved early:** output shaping, errors, cache, config, HTTP transport, bundle/index/query scaffolding.

**Stayed provider-local:** Wowhead parsing and routing quirks, service-specific ranking behavior, abstractions used by only one provider until a second consumer proved sharing.

## Wrapper Guardrails

- `warcraft` routes and orchestrates; it does not own parsers or API schemas
- Provider registration status must stay honest (`coming_soon` where surfaces are stubbed)

## Smoke Areas (Regression Reference)

search/resolve, entity/entity-page/comments, guide/guide-full, guide-export and bundle workflows, cache inspect/clear/repair.

## Related Docs

- [REPO_STRUCTURE_AND_PACKAGING.md](../REPO_STRUCTURE_AND_PACKAGING.md)
- [PACKAGE_LAYOUT.md](../PACKAGE_LAYOUT.md)
- [../../foundation/WRAPPER_PROVIDER_CONTRACT.md](../../foundation/WRAPPER_PROVIDER_CONTRACT.md)
