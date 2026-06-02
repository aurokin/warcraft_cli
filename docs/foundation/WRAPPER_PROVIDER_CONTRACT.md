# Wrapper Provider Contract

## Purpose

This document defines how the Python `warcraft` wrapper interacts with service providers.

It exists to keep the wrapper thin, predictable, and language-agnostic at the provider boundary.

## Wrapper Philosophy

`warcraft` is:
- a router
- a discovery layer
- a thin orchestration layer

It is not:
- a second implementation of every service
- a parser owner
- an API schema owner

## Required Provider Capabilities

Every service provider must expose these wrapper-facing capabilities:

- `search`
- `resolve`
- `doctor`
- direct passthrough execution for service-specific commands

If a capability is not implemented yet, it must still exist and return a structured stub such as `coming_soon`.

That keeps the wrapper contract stable even while services are being built.

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
- required runtime such as Node for a non-Python package

This should always exist, even for stubbed providers.

## Invocation Strategy

Preferred order:
- Python-first for Python services
- shell/CLI boundary second

Practical meaning:
- Python services may be invoked through shared entrypoints or direct package hooks
- non-Python services should be invoked through a CLI boundary
- the wrapper should preserve a stable provider contract either way

## Mixed-Language Rule

If a service is implemented in another language:
- the wrapper still stays Python
- the provider is invoked as a CLI process
- stdout/stderr and exit code become the integration boundary
- the provider must still satisfy the same `search`, `resolve`, and `doctor` contract

## Provider Registration

The wrapper should know for each provider:
- provider name
- command name
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

Current example:
- `wowhead`

### `fixed`

Meaning:
- the provider has a known fixed content scope and does not switch dynamically

Expected fields:
- `expansion_mode = "fixed"`
- explicit `supported_expansions`

Current likely examples:
- `method` -> `retail`
- `icy-veins` -> `retail`
- `raiderio` -> `retail`
- `warcraftlogs` -> `retail`
- `wowprogress` -> `retail`

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

Future provider note:
- some providers may require profile-based routing that is not a clean copy of the current wowhead-centric expansion vocabulary
- `warcraftlogs` is the current example: the wrapper now supports the retail provider surface, but classic/fresh site-profile mapping is still intentionally deferred

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
- that wrapper layer may also use provider-specific boosts for certain intents, such as preferring `raiderio` for character-profile queries and `wowprogress` for guild-profile queries
- the wrapper may add synthetic search candidates for narrow query families when a provider has a strong direct command surface but not a native search API for that family
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

## Milestone Behavior

Milestone 1:
- `warcraft` proxies `wowhead`
- `method` exists as a registered provider with stubbed `search`, `resolve`, and `doctor` if needed
- wrapper commands exist even if some providers are not yet real

Milestone 2:
- `method` becomes a real provider behind the same contract

Current state:
- `wowhead` is ready
- `method` is ready
- `icy-veins` is ready
- `raiderio` is ready for direct phase-1 retrieval plus provider-local search and conservative resolve
- `warcraft-wiki` is ready
- `wowprogress` is ready for structured search, conservative resolve, and direct phase-1 retrieval
- `simc` is ready for direct local repo workflows plus readonly APL inspection, conservative reasoning, comparison, analysis packets, and runtime timing helpers, with `search` and `resolve` intentionally returning structured `coming_soon` payloads
- `warcraftlogs` is ready for explicit report-scoped wrapper routing with retail-only OAuth client-credentials auth, typed world metadata, guild, character, and report commands. Wrapper `search`/`resolve` are intentionally limited to explicit report references (URL or a bare report code) and advertised as `ready_explicit_report_only`: a non-report query keeps `warcraftlogs` in the fanout but returns a structured discovery hint (`count: 0`, `resolved: false`, `message`, `supported_inputs`, `suggested_commands`) rather than a fabricated match
- `raidbots` is ready for report consumption (`inspect-report`, `input`, `explain-input`) and local SimC handoff; `search`/`resolve` are `not_supported` (report-driven provider, no discovery surface)
- `blizzard-api` is ready for official Game Data and Profile reads over OAuth client-credentials auth: `doctor` reports install state, auth posture, and the region/routing block; `game_data` and `profile` are ready (`realm`/`item`/`character` read commands), while `search`/`resolve` stay `coming_soon` until a discovery surface lands. Registered with `expansion_mode=none` (Blizzard's region/namespace model is not the wrapper's expansion axis)

## Documentation Rule

Whenever provider capabilities, provider readiness rules, or wrapper/provider boundaries change:
- update this document
- update [Roadmap](../ROADMAP.md) if sequencing changes
- update [Repo Structure And Packaging](../architecture/REPO_STRUCTURE_AND_PACKAGING.md) if package or language rules change
