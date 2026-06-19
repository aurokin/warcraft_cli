# Repo Structure And Packaging

## Purpose

This document defines the structural rules for the Warcraft monorepo.

It exists to keep future changes aligned with the intended architecture:
- separate service CLIs
- shared libraries with clear boundaries
- a thin `warcraft` wrapper
- isolated service packages
- purpose-driven exceptions instead of accidental coupling

## Core Decisions

### Monorepo Shape

This should be a monorepo with multiple package-level projects.

Each service package should be buildable from:
- its own source
- shared source

It should not depend on other service packages just because they live in the same repo.

### Install Model

Support both:
- one umbrella install for normal users
- service-specific installs for focused users or development workflows

That means:
- `warcraft` should be the umbrella package
- `wowhead`, `method`, `icy-veins`, `raiderio`, `warcraft-wiki`, `wowprogress`, and `simc` are independently installable provider packages
- `warcraftlogs` currently has a provider source directory and command surface that ship through the root package; it should gain a package-local `pyproject.toml` before being documented as independently installable
- candidate providers enter this list when they gain a package and command surface

### Wrapper Model

`warcraft` is a routing and orchestration layer.

It should:
- proxy to service CLIs
- offer shared search and resolve
- offer shared environment diagnostics

It should not:
- own service parsers
- own API schemas
- own SimC execution logic
- become a second implementation layer for every service

### Backward Compatibility

The monorepo migration has shipped, so user-facing command contracts and documented output shapes are the compatibility boundary.

Internal package layout can continue to evolve when needed, but provider commands should preserve shipped behavior unless the change is documented and reflected in the changelog.

## Language Policy

### Default Language

Python is the default language for this monorepo.

Reasons:
- existing codebase is already Python
- Python is a strong fit for HTML extraction, HTTP clients, CLI work, local tooling orchestration, and file-backed data workflows
- the wrapper and shared layers benefit more from iteration speed and maintainability than from raw runtime performance

For now, the intended baseline is:
- shared libraries: Python
- wrapper: Python
- all services: Python

## Package Boundaries

### Shared Packages

These are the first shared package targets:

- `warcraft-core`
  - output shaping
  - field projection
  - structured errors
  - config loading
  - environment inspection helpers
- `warcraft-api`
  - HTTP client primitives
  - retries and backoff
  - throttling hooks
  - auth/config persistence helpers
- `warcraft-content`
  - bundle storage
  - index management
  - freshness tracking
  - local query scaffolding

These shared packages should stay narrow and infrastructure-focused.

### Service Packages

Each service package owns:
- parsing rules
- API contracts
- service-specific identifiers
- service-specific ranking behavior
- service-specific operational constraints

Examples:
- `wowhead` owns Wowhead entity/page parsing
- `method` owns Method guide parsing
- `raiderio` owns Raider.IO endpoint and profile logic
- `warcraft-wiki` owns MediaWiki-backed reference lookups
- `wowprogress` owns WowProgress ranking/profile lookups
- `simc` owns local repo/build/run orchestration
- `warcraftlogs` owns GraphQL query catalogs and auth scope handling

### Dependency Direction

Allowed:
- service package -> shared package
- umbrella package -> shared package
- umbrella package -> service CLI invocation

Not allowed:
- service package -> another service package
- shared package -> service package

## Auth Policy

Auth is provider-scoped. Some providers remain unauthenticated, while OAuth-backed providers such as Warcraft Logs use the shared auth primitives described in [Auth Architecture](AUTH_ARCHITECTURE.md).

Preferred order:
- OS keychain when available
- file-based secret storage with strict permissions as fallback
- env vars for CI and headless usage

Rules:
- store secrets per service
- keep shared config separate from secrets
- do not write secrets into bundles, manifests, or shared cache entries
- let each service own its own auth logic even if storage helpers are shared

## Search Policy

`warcraft search` should query all services available to the user.

That includes:
- unauthenticated services by default
- authenticated services when the user has configured auth for them

This means provider discovery must be auth-aware and capability-aware.

## Storage Policy

Use one common root with service-specific subdirectories and a separate shared directory.

High-level shape:
- `shared/`
- `wowhead/`
- `method/`
- `icy-veins/`
- `raiderio/`
- `warcraft-wiki/`
- `wowprogress/`
- `simc/`
- `warcraftlogs/`

Use the shared directory only for data that is actually shared. Do not use it as a dumping ground.

## Platform Policy

Keep Linux support primary.

Cross-platform support can come later, but the structure should avoid making Linux-first assumptions impossible to revisit.

`simc` is the biggest reason this needs to be explicit.

## Release Policy

Use one release pipeline for the monorepo for now.

That does not require one combined CLI. It only means release management stays centralized until there is a reason to split it.

## Current Source Layout

The current high-level layout includes:

- `packages/warcraft-core/`
- `packages/warcraft-api/`
- `packages/warcraft-content/`
- `packages/warcraft-cli/`
- `packages/wowhead-cli/`
- `packages/method-cli/`
- `packages/icy-veins-cli/`
- `packages/raiderio-cli/`
- `packages/warcraft-wiki-cli/`
- `packages/wowprogress-cli/`
- `packages/simc-cli/`
- `packages/warcraftlogs-cli/`
- `packages/raidbots-cli/`
- `packages/blizzard-api-cli/`
- `packages/curseforge-cli/`
- `packages/lorrgs-cli/`
- `skills/warcraft/`

Service-specific root skills can be added later if they prove useful. For now, the root `warcraft` skill should use progressive disclosure and route agents to the right service docs and CLI.

## Questions This Doc Resolves

- Should this be one big CLI? No.
- Should this be one repo? Yes.
- Should packages be isolated? Yes.
- Should users be able to install one umbrella package? Yes.
- Should users be able to install one service package? Yes.
- Should Python remain the default? Yes.
- Is Python the language for all services? Yes.

## Linked Docs

- [Roadmap](../ROADMAP.md)
- [Architecture index](README.md)
- [Package Layout](PACKAGE_LAYOUT.md)
- [Monorepo migration (completed)](history/MONOREPO_MIGRATION.md)
- [Wrapper Provider Contract](../foundation/WRAPPER_PROVIDER_CONTRACT.md)
- [Warcraft wrapper CLI doc](../warcraft/README.md)
- [Warcraft Logs CLI doc](../warcraftlogs/README.md)
