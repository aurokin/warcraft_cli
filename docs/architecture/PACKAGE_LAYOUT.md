# Package Layout

## Purpose

This document describes the package layout of the Warcraft monorepo as it actually is.

It is the concrete companion to:
- [Repo Structure And Packaging](REPO_STRUCTURE_AND_PACKAGING.md)
- [Roadmap](../ROADMAP.md)

## Distribution Unit

The distribution unit is the root `warcraft` wheel built from the root `pyproject.toml`
(`make build` / `uv build --wheel`). It carries every package's source and declares all 13 console
scripts, so one install gives an agent the whole surface.

The 16 package-local `pyproject.toml` files are kept because they encode the dependency graph, not
because each one is published:

- `tests/test_warcraft_cli_packaging.py` asserts every package declares the runtime distributions
  its `src/` actually imports, so the per-package metadata cannot silently rot.
- The CI `isolated-install` job installs one provider package plus the shared packages into a clean
  virtualenv and runs its console script, so "install one provider on its own" stays real.
- Nothing is published to PyPI. Releases attach the wheel to a GitHub release
  (`.github/workflows/release.yml`).

## Packages

| Directory | Distribution | Import package | Console script | Role |
| --- | --- | --- | --- | --- |
| `packages/warcraft-core/` | `warcraft-core-cli` | `warcraft_core` | — | shared |
| `packages/warcraft-api/` | `warcraft-api-cli` | `warcraft_api` | — | shared |
| `packages/warcraft-content/` | `warcraft-content-cli` | `warcraft_content` | — | shared |
| `packages/warcraft-cli/` | `warcraft-cli` | `warcraft_cli` | `warcraft` | wrapper |
| `packages/wowhead-cli/` | `wowhead-cli` | `wowhead_cli` | `wowhead` | core |
| `packages/warcraftlogs-cli/` | `warcraftlogs-cli` | `warcraftlogs_cli` | `warcraftlogs` | core |
| `packages/simc-cli/` | `simc-cli` | `simc_cli` | `simc` | core |
| `packages/raiderio-cli/` | `raiderio-cli` | `raiderio_cli` | `raiderio` | supported |
| `packages/warcraft-wiki-cli/` | `warcraft-wiki-cli` | `warcraft_wiki_cli` | `warcraft-wiki` | supported |
| `packages/icy-veins-cli/` | `icy-veins-cli` | `icy_veins_cli` | `icy-veins` | supported |
| `packages/method-cli/` | `method-cli` | `method_cli` | `method` | supported |
| `packages/lorrgs-cli/` | `lorrgs-cli` | `lorrgs_cli` | `lorrgs` | supported |
| `packages/raidbots-cli/` | `raidbots-cli` | `raidbots_cli` | `raidbots` | experimental |
| `packages/blizzard-api-cli/` | `blizzard-api-cli` | `blizzard_api_cli` | `blizzard` | experimental |
| `packages/curseforge-cli/` | `curseforge-cli` | `curseforge_cli` | `curseforge` | experimental |

The three shared distributions carry a historical `-cli` suffix even though they ship no command.
Renaming them would break every existing per-package dependency pin for no user-visible gain.

Tiers are the support level agents should expect; they are declared on each `ProviderRegistration`
in `packages/warcraft-cli/src/warcraft_cli/providers.py` and surfaced by `warcraft doctor`.
`blizzard` and `curseforge` are experimental with endpoints that have not been confirmed live.

## Per-Package Structure

- `pyproject.toml`
- `src/<import_package>/`
- `README.md` only when the package needs docs beyond `docs/<cli>/README.md`

Packages have no local `tests/` directory. Every test lives in the root `tests/` so one pytest run
covers cross-package contracts (envelope conformance, docs parity, help parity, packaging).

Shared packages expose no CLI entrypoints.

## Naming

- provider distributions end in `-cli`; the command they install is the short service name
- `blizzard-api-cli` is the only package whose command (`blizzard`) differs from its directory stem
- new provider packages keep the same package-to-command alignment

## Dependency Direction

Enforced by `.importlinter` (`make lint-boundaries`):

```
warcraft_cli
  -> provider packages (independent of each other)
    -> warcraft_api | warcraft_content
      -> warcraft_core
```

Allowed:
- provider package -> `warcraft-core`, `warcraft-api`, `warcraft-content`
- `warcraft-cli` -> shared packages and provider packages

Not allowed:
- provider package -> another provider package
- shared package -> provider package

Additional rules the linter cannot express:

- Shared packages never import a provider package and never execute a provider binary.
- `warcraft_core.talent_transport` holds pure parsing and validation only. It takes an injectable
  round-trip executor (`RoundTripExecutor`); `simc_cli.talent_transport` supplies the
  SimulationCraft-backed one, and `warcraftlogs_cli` uses the pure validation with an optional
  backend that the wrapper may inject.
- The wrapper reaches providers in-process through the `PROVIDER` surfaces registered in
  `warcraft_cli.providers`. It does not spawn provider binaries, and no other `warcraft_cli` module
  imports a provider package.

## Root-Level Responsibilities

The repo root owns docs, `scripts/`, `tests/`, shared tooling config, and the release/CI
configuration. It is not an implicit package that other packages import from.

## Storage Layout

XDG-style roots resolved by `warcraft_core.paths`:

- config: `XDG_CONFIG_HOME/warcraft/` (default `~/.config/warcraft/`)
- data: `XDG_DATA_HOME/warcraft/` (default `~/.local/share/warcraft/`)
- cache: `XDG_CACHE_HOME/warcraft/` (default `~/.cache/warcraft/`)
- state: `XDG_STATE_HOME/warcraft/` (default `~/.local/state/warcraft/`), including
  `providers/<provider>.json` auth state

Within each root: `shared/` for genuinely shared data, then one directory per provider.

## Rules

- every package must stay independently buildable and truthfully declare its dependencies
- shared code moves into a shared package only when a second consumer proves the need
- package boundaries and dependency direction stay documented here and enforced in `.importlinter`
- update this document whenever the package set or dependency rules change

Open engineering work lives in Linear.
