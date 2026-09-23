# Repo Structure And Packaging

## Purpose

Structural rules for the Warcraft monorepo: what is a package, what may depend on what, how the
product is installed, and which tooling enforces it.

Companion documents: [Package Layout](PACKAGE_LAYOUT.md) for the concrete package table,
[Error and Envelope Contract](../foundation/ERROR_CONTRACT.md) for the output contract every
binary shares, and [docs/reference/](../reference/README.md) for the generated command surface.

## Core Decisions

### Monorepo Shape

One repository, 16 package-level projects: three shared libraries, the `warcraft` wrapper, and 12
provider CLIs. Each provider package builds from its own source plus the shared packages, and never
from another provider package.

### Install Model

The distribution unit is the root `warcraft` wheel. Three supported install paths:

| Path | Command | Who it is for |
| --- | --- | --- |
| Released wheel | `pipx install <wheel url>` / `uvx --from <wheel url> warcraft ...` | users |
| Editable checkout | `uv sync --all-extras` (`make install`); `pip install -e '.[dev]'` also works | developers |
| Single provider | `uv pip install packages/warcraft-core packages/warcraft-api packages/warcraft-content packages/<provider>-cli` | focused or embedded use |

Nothing is published to PyPI. `.github/workflows/release.yml` builds the wheel on a `v*` tag and
attaches it to the GitHub release; `.github/workflows/ci.yml` proves the single-provider path by
installing one provider at a time into a clean virtualenv and running its console script.

Every provider listed in [Package Layout](PACKAGE_LAYOUT.md) has a package-local `pyproject.toml`
whose dependency list is checked against its real imports by
`tests/test_warcraft_cli_packaging.py`.

### Wrapper Model

`warcraft` is a routing and orchestration layer.

It does:
- route `search`, `resolve`, `doctor`, and `--expansion` filtering across providers
- pass provider subcommands straight through (`warcraft wowhead entity ...`)
- compose cross-provider evidence commands that no single provider can answer: `guild`,
  `guide-compare`, `guide-compare-query`, `guide-builds-simc`, `talent-packet`, `talent-describe`,
  `cooldown-packet`

It does not:
- own service parsers or API schemas
- own SimC execution logic
- reimplement a provider surface

Composition is a legitimate wrapper responsibility; a second implementation of a provider is not.
The wrapper reaches providers through the in-process `PROVIDER` surfaces registered in
`warcraft_cli.providers`. It never spawns a provider binary and never drives one through a Typer
test runner.

### Backward Compatibility

User-facing command contracts and documented output shapes are the compatibility boundary.
Internal layout can keep evolving. Every envelope carries only the envelope keys, with the payload
under `data`; see [ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

## Language Policy

Python 3.12 for everything: shared libraries, wrapper, and all providers. The codebase is already
Python, and the work (HTML extraction, HTTP clients, CLI surfaces, local tooling orchestration,
file-backed data workflows) rewards iteration speed over raw runtime performance.

## Package Boundaries

### Shared Packages

`warcraft-core` (`warcraft_core`) — no provider imports, no subprocess execution of provider
binaries:

| Module | Responsibility |
| --- | --- |
| `output` | JSON shaping: pretty/compact, `--fields` projection and `fields_missing`, output profiles |
| `cli` | Typer scaffolding: `RuntimeConfig`, `cfg`, `emit`, `fail`, common global options, `guarded_run` |
| `envelope` | The `Envelope` TypedDict plus `success_envelope` / `error_envelope` |
| `exit_codes` | Error-code to exit-code mapping (1 generic, 2 usage, 3 auth, 4 not found, 5 network) |
| `provider` | `ProviderSurface` Protocol and `ProviderError` |
| `auth` | Credential discovery order and provider auth-state persistence |
| `env` | `.env.local` / provider env-file discovery and key reads |
| `paths` | XDG config/data/cache/state roots and per-provider subpaths |
| `identity` | Shared character/guild/realm identity semantics |
| `citations` | Source citation shaping |
| `analytics` | Sampling and comparison primitives shared by analytics surfaces |
| `wow_normalization` | Game-vocabulary normalization (classes, specs, slots) |
| `expansions` | Expansion keys, aliases, and per-site mappings (Wowhead prefixes, Warcraft Logs sites) |
| `talent_transport` | Pure talent-packet parsing and validation with an injectable round-trip executor |

`warcraft-api` (`warcraft_api`):

| Module | Responsibility |
| --- | --- |
| `http` | HTTP client construction, retries, backoff, shared per-host throttling |
| `cache` | On-disk and Redis-backed response caching |

Auth lives in `warcraft_core.auth`, not here. See [Auth Architecture](AUTH_ARCHITECTURE.md).

`warcraft-content` (`warcraft_content`):

| Module | Responsibility |
| --- | --- |
| `article_bundle` | Bundle storage, manifests, freshness tracking |
| `article_discovery` | Sitemap and index discovery for article providers |
| `article_provider_cli` | Shared Typer helpers for article-shaped providers |
| `search` | Local bundle indexing and query |
| `guide_analysis` | Cross-provider guide comparison primitives |

### Provider Packages

Each provider package owns its parsing rules, API contracts, identifiers, ranking behavior, and
operational constraints, and exports `PROVIDER` from `<pkg>/provider.py`. Its Typer commands are
thin wrappers over that surface.

- `wowhead` — Wowhead entity/page parsing, comments, guides, expansion routing
- `warcraftlogs` — GraphQL query catalogs, OAuth scope handling, sampled report analytics
- `simc` — local SimulationCraft repo/build/run orchestration and APL analysis
- `raiderio` — Raider.IO endpoints, profiles, sampled Mythic+ analytics
- `warcraft-wiki` — MediaWiki-backed reference lookups
- `icy-veins` — Icy Veins guide families
- `method` — Method.gg article-shaped guides
- `lorrgs` — top-parse cooldown timelines and composition rankings
- `raidbots` — shared report parsing and SimC-input handoff
- `blizzard` — Battle.net Game Data and Profile reads (endpoints unverified)
- `curseforge` — addon metadata, files, changelog (endpoints unverified)

### Dependency Direction

Enforced by `.importlinter`. Allowed: provider -> shared, wrapper -> shared, wrapper -> provider.
Not allowed: provider -> provider, shared -> provider. Full rules in
[Package Layout](PACKAGE_LAYOUT.md).

## Auth Policy

Auth is provider-scoped. Some providers are unauthenticated; OAuth-backed providers use the shared
primitives described in [Auth Architecture](AUTH_ARCHITECTURE.md).

Preferred credential order: OS keychain when available, then file-based secret storage with strict
permissions, then env vars for CI and headless usage.

Rules:
- store secrets per provider
- keep shared config separate from secrets
- never write secrets into bundles, manifests, or shared cache entries
- each provider owns its own auth logic even when storage helpers are shared

## Search Policy

`warcraft search` queries every provider available to the user: unauthenticated providers by
default, authenticated providers when credentials are configured. Provider discovery is therefore
auth-aware and capability-aware, and `warcraft doctor` reports which providers were included.

## Storage Policy

One root per XDG category, `shared/` for genuinely shared data, then one directory per provider:
`wowhead/`, `warcraftlogs/`, `simc/`, `raiderio/`, `warcraft-wiki/`, `icy-veins/`,
`method/`, `lorrgs/`, `raidbots/`, `blizzard/`, `curseforge/`. Do not use `shared/` as a dumping
ground.

## Platform Policy

Linux support stays primary; `simc` is the main reason this needs to be explicit. macOS is the
common development platform. Avoid assumptions that would make cross-platform support impossible to
revisit.

## Tooling

- **Package manager:** `uv`. `uv.lock` is committed; CI installs with `uv sync --frozen`. Makefile
  targets run through `.venv`. `pip install -e '.[dev]'` still works.
- **`make check`** = `lint typecheck lint-boundaries complexity-gate deadcode coverage`. Details in
  [LINTING_AND_COMPLEXITY.md](LINTING_AND_COMPLEXITY.md).
- **CI** (`.github/workflows/ci.yml`): `lint-and-typecheck`, `unit-tests`, `isolated-install`,
  `wheel`, `gitleaks`.
- **Live contracts** (`.github/workflows/live-contracts.yml`): weekly schedule plus manual dispatch;
  the Wowhead parser canary and the keyless end-to-end journeys, with no secrets.
- **Release** (`.github/workflows/release.yml`): a `v*` tag builds the wheel and attaches it.
- **Generated docs:** `make reference` regenerates `docs/reference/<cli>.md` from the Typer apps and
  `make skills` regenerates the provider subskills. Both have staleness tests; never hand-edit the
  output.

## Release Policy

One release pipeline for the monorepo. That does not require one combined CLI; it means release
management stays centralized. Steps live in [docs/RELEASE.md](../RELEASE.md); shipped changes live
in [CHANGELOG.md](../../CHANGELOG.md).

## Questions This Doc Resolves

- Should this be one big CLI? No — one wrapper plus 11 provider binaries.
- Should this be one repo? Yes.
- Should packages be isolated? Yes, with enforced dependency direction.
- Is there one install for normal users? Yes — the root wheel.
- Can a user install one provider? Yes, and CI proves it.
- Should Python remain the default for every package? Yes.

## Linked Docs

- [Architecture index](README.md)
- [Package Layout](PACKAGE_LAYOUT.md)
- [Error and Envelope Contract](../foundation/ERROR_CONTRACT.md)
- [Wrapper Provider Contract](../foundation/WRAPPER_PROVIDER_CONTRACT.md)
- [Generated command reference](../reference/README.md)
- [Roadmap](../ROADMAP.md)
