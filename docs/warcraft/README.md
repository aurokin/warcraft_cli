# Warcraft CLI

## Purpose

`warcraft` should be the root orchestration CLI for this repo.

It is not the place where service-specific logic should live. Its job is to help an agent:
- decide which backing service is most relevant
- route to the right service CLI when the source is already known
- expose shared discovery and environment checks when those concepts become cross-service

## What The Wrapper Should Do

- `warcraft search`: fan out to service-specific search providers where that is practical. Each registered provider advertises a `search`/`resolve` capability that matches its own `doctor` (a parity test enforces this). Providers whose surface is not `ready` are reported under `excluded_providers` rather than queried. `warcraftlogs` is `ready_explicit_report_only`: it stays in the fanout but returns a structured discovery hint (`count: 0`, `message`, `supported_inputs`, `suggested_commands`) for any non-report query instead of a fabricated match.
- `warcraft resolve`: conservatively pick the best service and next command; it never resolves to a provider that reported `resolved: false` (e.g. a non-report `warcraftlogs` query).
- `warcraft <service> ...`: proxy through to any registered provider — `wowhead`, `method`, `icy-veins`, `raiderio`, `warcraft-wiki`, `wowprogress`, `simc`, `raidbots`, `warcraftlogs`, `blizzard`, `curseforge`, or `lorrgs`.
- `warcraft doctor`: surface accurate per-provider capability, expansion mode/review status, and auth posture (the registry metadata is kept in lockstep with each CLI's own `doctor`).
- `warcraft cooldown-packet`: compose Lorrgs phase/cooldown context with Warcraft Logs actor cast events for user-specific phase cooldown analysis.
- expose shared inspection commands only when the concept is truly shared across services
- when it exposes a merged shared workflow, keep provider-native payloads accessible and layer wrapper comparisons on top instead of replacing the sources

## What The Wrapper Should Not Do

- it should not hide source provenance
- it should not impose one universal data model across article sites, APIs, and local tools
- it should not become the place where parsers, API schemas, or SimC execution logic live
- it should not route through stubbed wrapper surfaces as if they were production-ready search or resolve providers

## Recommended First Contract

Start narrow:

- `warcraft wowhead ...`
- `warcraft search ...`
- `warcraft resolve ...`
- `warcraft doctor`

That is enough to validate:
- command routing
- service discovery
- agent-facing ergonomics
- shared environment inspection

The packaging and language boundaries for the wrapper are defined in [REPO_STRUCTURE_AND_PACKAGING.md](../architecture/REPO_STRUCTURE_AND_PACKAGING.md).

## Shared Code It Should Reuse

- command routing helpers
- shared output shaping and field projection
- shared cache/environment inspection
- shared search and resolve interfaces
- provider-facing orchestration helpers when a workflow genuinely spans providers

## Shared Code It Should Not Own

The wrapper should consume shared libraries, not become one:

- no service parsers
- no API schema logic
- no SimC execution logic
- no service-specific ranking logic

## Risks

- putting too much real business logic in the wrapper
- hiding which source answered the question
- forcing all services into one command grammar too early

## Source Links

- [Roadmap](../ROADMAP.md)
