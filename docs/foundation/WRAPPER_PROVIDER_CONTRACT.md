# Wrapper Provider Contract

## Purpose

This document defines how the Python `warcraft` wrapper interacts with service providers.

It exists to keep the routing boundary thin and predictable, and to make every provider look the same to an agent.

## Wrapper Philosophy

`warcraft` is:
- a router (`warcraft <provider> ...` passthrough)
- a discovery layer (`search`, `resolve`, `doctor`)
- the owner of cross-provider composition

It is not:
- a second implementation of every service
- a parser owner
- an API schema owner

Composition is a real wrapper responsibility, not an accident: `actor-profile`,
`cooldown-packet`, `guide-compare`, `guide-compare-query`, `guide-builds-simc`, `talent-packet`,
and `talent-describe` all merge or hand off between two or more providers. No provider owns those
workflows, so the wrapper does. `guild` and `guild-ranks` are identity-normalizing wrappers over
the single guild provider (`raiderio`) and keep the same source/provenance shape so a second source
can be added without changing the contract. The line the wrapper must not cross is *parsing or
re-modelling a provider's source data*: composite commands consume provider payloads, preserve each
source's provenance, and add their own reconciliation layer explicitly.

## Required Provider Capabilities

Every service provider must expose these wrapper-facing capabilities:

- `search`
- `resolve`
- `doctor`
- direct passthrough execution for service-specific commands

A capability that is not implemented yet must still exist and return a structured `coming_soon`
stub. A capability the provider will never have — Raidbots publishes no report index, so it has no
discovery surface — is declared `not_supported` in the registry and still answers with a structured
stub instead of a crash. Either way the wrapper contract stays stable and the registry, not a
special case in wrapper code, says which it is.

## Capability Expectations

### `search`

Purpose:
- return service-specific candidate matches for a free-text query

Minimum behavior:
- accept a query string
- return a structured result list
- return `coming_soon` if not implemented yet

### `resolve`

Purpose:
- return the best next service-specific command for a query when confidence is high

Minimum behavior:
- accept a query string
- return either a candidate resolution or a structured unresolved response
- return `coming_soon` if not implemented yet

Important boundary:
- wrapper `search`, `resolve`, and follow-up guidance are routing aids
- they should help agents choose the right provider and next command
- they should not pretend to be final answer synthesis

### `doctor`

Purpose:
- report whether the service is ready to be used in the current environment

Examples of what it may check:
- package availability
- auth configuration
- local binary presence
- cache/storage roots
- local runtime dependencies such as the SimulationCraft checkout and binary

This should always exist, even for stubbed providers.

## Provider Surface

The wrapper-facing capabilities are a code-level interface, not prose. `warcraft_core.provider`
defines:

```python
class ProviderSurface(Protocol):
    name: str
    def search(self, query: str, *, limit: int = 10, **options) -> Envelope: ...
    def resolve(self, target: str, **options) -> Envelope: ...
    def doctor(self, **options) -> Envelope: ...
```

Rules:
- every provider package exports `PROVIDER` from `<pkg>/provider.py`, an object satisfying that protocol
- surface methods are pure: they never print and never raise `typer.Exit`; they return an `Envelope` or raise `ProviderError`
- the provider's Typer commands are thin wrappers that call the surface and emit its envelope
- the wrapper calls `PROVIDER` objects in-process for `search`, `resolve`, and `doctor` — never a test CLI runner and never a subprocess
- `warcraft <provider> ...` passthrough invokes that provider's Typer app in-process and forwards the global output flags

There is no shell integration boundary and none is planned. Every provider is a Python package in
this repo; a future non-Python service would be wrapped by a Python adapter package that exports
`PROVIDER` like any other provider.

## Envelope, Errors, And Exit Codes

Wrapper and provider payloads share one envelope (`ok`, `provider`, `command`, `kind`,
`schema_version`, `query`, `provenance`, `data`, and `error` on failure) and one exit-code
vocabulary (1 generic, 2 usage, 3 auth, 4 not found, 5 network/upstream). Both are defined in
[ERROR_CONTRACT.md](ERROR_CONTRACT.md), which is the normative document; this page does not restate
them. Provider rows inside `warcraft search` and `warcraft resolve` output carry `ok` and `error`
from the underlying call alongside the registry `status`.

## Provider Tiers

Every registration declares a tier. `warcraft doctor` reports `wrapper.tiers` and a `tier` per
provider row, and [ROADMAP.md](../ROADMAP.md) and `README.md` use the same membership.

| Tier | Providers | Meaning |
|------|-----------|---------|
| core | `wowhead`, `warcraftlogs`, `simc` | deepest surface and contracts; the product |
| supported | `method`, `icy-veins`, `raiderio`, `warcraft-wiki` | real, narrower surfaces expected to work |
| experimental | `raidbots`, `blizzard-api`, `curseforge`, `lorrgs` | thin or unproven; `blizzard-api` and `curseforge` are additionally unverified against live endpoints (`provenance.verified: false`) |

Tier is descriptive, not a permission: it tells an agent how much to trust the surface before
building a workflow on it.

## Provider Registration

The wrapper should know for each provider:
- provider name
- command name
- support tier
- implementation language
- whether it is installed
- whether auth is configured
- whether auth is required for the provider at all
- expansion support mode
- supported expansions when known
- expansion review status
- a short policy note explaining the current expansion classification
- whether `search`, `resolve`, and `doctor` are supported or stubbed
- whether each wrapper surface is actually ready to participate in wrapper fanout

This registry should drive wrapper behavior instead of hardcoded special cases spread throughout the codebase.

## Expansion Support Contract

The wrapper must treat expansion filtering as a trust-sensitive contract.

When a caller explicitly asks for an expansion, the wrapper must not silently mix in providers whose game-version semantics are ambiguous.

Every provider should declare one expansion mode:

- `profiled`
- `fixed`
- `none`

### `profiled`

Meaning:
- the provider can actively switch behavior based on requested expansion

Expected fields:
- `expansion_mode = "profiled"`
- provider-defined supported expansion list or profile map

Current examples:
- `wowhead` (expansion profiles)
- `warcraftlogs` (`retail` / `classic` / `fresh` site profiles)

### `fixed`

Meaning:
- the provider has a known fixed content scope and does not switch dynamically

Expected fields:
- `expansion_mode = "fixed"`
- explicit `supported_expansions`

Current examples:
- `method` -> `retail`
- `icy-veins` -> `retail`
- `raiderio` -> `retail`
- `lorrgs` -> `retail`
- `raidbots` -> `retail`

### `none`

Meaning:
- the provider cannot yet reliably claim expansion semantics

Expected behavior:
- excluded from wrapper expansion-filtered search and resolve
- surfaced in excluded-provider metadata

## Expansion Fanout Rules

When `warcraft search` or `warcraft resolve` is called with `--expansion <x>`:

- include `profiled` providers that support `<x>`
- include `fixed` providers only when `<x>` is in their declared supported expansion set
- exclude `none` providers
- report excluded providers and exclusion reasons

The wrapper must not silently widen scope.

`retail` is a real explicit filter, not equivalent to “no expansion filter”.

Provider profile note:
- a `profiled` provider does not have to share Wowhead's profile model; the shared key and alias vocabulary lives in `warcraft_core.expansions`, and each provider maps those keys onto its own model
- `warcraftlogs` is the second example: the wrapper maps `retail` to `--site retail`, the classic-family keys (`classic`, `tbc`, `wotlk`, `cata`, `mop-classic`) to `--site classic`, and `fresh` to `--site fresh`; `ptr`, `beta`, and `classic-ptr` are rejected rather than coerced

## Expansion Output Rules

When expansion filtering is active, wrapper output should preserve:
- requested expansion
- included providers
- excluded providers
- exclusion reasons

This metadata is required so agents can trust why a result was or was not considered.

## Search Fanout Rules

`warcraft search` should query:
- all installed unauthenticated providers
- all installed authenticated providers when auth is configured

Providers that are not ready should still report through `doctor`.
Providers whose wrapper `search` surface is stubbed or otherwise not ready should be excluded from wrapper fanout and surfaced in exclusion metadata instead of being treated like live routing candidates.

Search result ordering rules:
- provider-local ranking stays provider-specific
- the wrapper may apply a thin, tunable cross-provider ranking layer on top of provider-local scores
- that wrapper layer should be query-aware and use signals like provider family, result kind, and structured query hints
- that wrapper layer may also use provider-specific boosts for certain intents, such as preferring `raiderio` for character-profile and guild-profile queries
- wrapper ranking must stay inspectable in output, not hidden behind opaque ordering
- the wrapper should not invent a fake universal content model beyond that thin ranking/orchestration layer

Ranking policy location:
- default policy lives in shared code
- optional local override file: `~/.config/warcraft/wrapper_ranking.json`
- override files should only tune weights and mappings, not redefine provider contracts

## Resolve Rules

`warcraft resolve` should:
- ask ready providers for candidate resolutions
- rank them conservatively
- preserve source provenance
- avoid pretending certainty when providers are stubbed or unavailable
- not upgrade synthetic direct-route hints into verified resolved matches unless the provider actually returned a resolved payload

Providers whose wrapper `resolve` surface is stubbed or otherwise not ready should be excluded from wrapper fanout and surfaced in exclusion metadata instead of being queried like live routing candidates.

Resolve selection rules:
- do not pick the first provider that reports `resolved`
- prefer higher provider-reported confidence first
- use the tunable wrapper ranking layer, then the provider-reported match score, as tie-breakers
- preserve the chosen provider's `match`, `next_command`, and confidence instead of flattening them

Debuggability rules:
- `warcraft search --ranking-debug` should expose compact ranking summaries for the top wrapper candidates
- `warcraft resolve --ranking-debug` should expose the ranked resolved candidates the wrapper considered
- `warcraft search --compact` and `warcraft resolve --compact` should omit bulky provider payloads while keeping the wrapper decision surface intact

## Doctor Rules

`warcraft doctor` should report:
- wrapper health
- installed providers
- provider readiness
- auth availability where relevant
- wrapper-surface readiness for `doctor`, `search`, and `resolve`
- runtime availability for non-Python services
- storage/config root status

## Output Rules

The wrapper should preserve:
- service provenance
- suggested next command
- clear readiness/error state

It should not flatten all provider outputs into one fake universal model.
It should not turn routing guidance into unsupported "smart answers."

## Current State

- `wowhead` is ready
- `method` is ready
- `icy-veins` is ready
- `raiderio` is ready for direct phase-1 retrieval plus provider-local search and conservative resolve
- `warcraft-wiki` is ready
- `simc` is ready for direct local repo workflows plus readonly APL inspection, conservative reasoning, comparison, analysis packets, and runtime timing helpers, with `search` and `resolve` intentionally returning structured `coming_soon` payloads
- `warcraftlogs` is ready for explicit report-scoped wrapper routing with OAuth client-credentials auth across the `retail`, `classic`, and `fresh` site profiles, typed world metadata, guild, character, and report commands. Wrapper `search`/`resolve` are intentionally limited to explicit report references (URL or a bare report code) and advertised as `ready_explicit_report_only`: a non-report query keeps `warcraftlogs` in the fanout but returns a structured discovery hint (`count: 0`, `resolved: false`, `message`, `supported_inputs`, `suggested_commands`) rather than a fabricated match
- `raidbots` is ready for report consumption (`inspect-report`, `input`, `explain-input`) and local SimC handoff; `search`/`resolve` are `not_supported` (report-driven provider, no discovery surface)
- `blizzard-api` is ready for official Game Data and Profile reads over OAuth client-credentials auth: `doctor` reports install state, auth posture, and the region/routing block; `game_data` and `profile` are ready (`realm`/`item`/`character` read commands), while `search`/`resolve` stay `coming_soon` until a discovery surface lands. Registered with `expansion_mode=none` (Blizzard's region/namespace model is not the wrapper's expansion axis)
- `curseforge` is a scaffold for the public CurseForge addon API (`x-api-key` auth, `CURSEFORGE_API_KEY`): `doctor` and `addon` are ready (`curseforge addon <slug|id>` returns the addon metadata, latest files, and the newest file's changelog), while `search`/`resolve` stay `coming_soon`. Registered with `expansion_mode=none` (addon game-version compatibility lives inside file records, not the wrapper's expansion axis). Host/endpoints/response shapes follow the documented public CurseForge Core API and are pending one-time live confirmation (`provenance.verified=false`; run `CURSEFORGE_LIVE_TESTS=1`)
- `lorrgs` is ready for no-auth public Lorrgs reads: static spec/boss/spell metadata, top-parse spec rankings, composition rankings, report overview handoffs, and conservative `search`/`resolve` for Lorrgs URLs, Warcraft Logs report URLs, bare report codes, and spec/boss text. Registered with `expansion_mode=fixed` / `supported_expansions=["retail"]`

## Documentation Rule

Whenever provider capabilities, provider readiness rules, or wrapper/provider boundaries change:
- update this document
- update [Roadmap](../ROADMAP.md) if sequencing changes
- update [Repo Structure And Packaging](../architecture/REPO_STRUCTURE_AND_PACKAGING.md) if package or language rules change
