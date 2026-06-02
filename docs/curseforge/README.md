# CurseForge CLI

## Decision

**GO.**

The [AUR-395](https://linear.app/aurokin/issue/AUR-395) gate — *revisit only when wrapper,
official API (Blizzard), and evidence-oriented analytics work are in a stronger place* — is met:
wrapper routing/doctor/registration shipped (AUR-384), the official Blizzard Game Data/Profile
slice shipped (AUR-455), and the evidence/trust analytics passes shipped (AUR-386/387/388).
CurseForge clears go/no-go because it has a documented public API, a read/metadata-first first
slice that needs no write or user-auth flows, and it complements `warcraft-wiki`: the wiki gives
programming/usage context while CurseForge gives the real packaged addon surface users install.

Scaffold work is tracked in the follow-up issue
[AUR-499 — CurseForge provider scaffold (doctor + addon-lookup workflow)](https://linear.app/aurokin/issue/AUR-499).

## Expected User Workflow

> **Status: approved, not yet implemented.** No `curseforge` CLI package exists yet — these
> commands will not run until the [AUR-499](https://linear.app/aurokin/issue/AUR-499) scaffold
> lands. The sequence below is the committed *target* for that first slice, not a live surface.

The committed first slice is addon lookup by slug or id, returning metadata, latest files, and
changelog in one provider:

```
warcraft curseforge doctor
warcraft curseforge addon <slug-or-id>   # metadata + latest files + changelog
```

`warcraft curseforge ...` will route through the wrapper to the `curseforge` provider; each command
should return the standard `{ok, provider, command, kind, query, provenance, data}` envelope on
success and `{ok:false, ..., error:{code, message}}` with a nonzero exit on failure (never a
traceback).

## Wrapper Contract

Per [WRAPPER_PROVIDER_CONTRACT.md](../foundation/WRAPPER_PROVIDER_CONTRACT.md), the provider
registers with:

- `doctor` — **ready** (install state + auth posture + capability metadata)
- `search` / `resolve` — `coming_soon` for the scaffold slice; the surfaces exist and return
  structured stubs so the wrapper contract stays stable
- direct passthrough execution for `addon` (and later addon-specific commands)
- `expansion_mode = "none"` — CurseForge addon metadata is not the wrapper's expansion axis, so it
  stays out of expansion fanout (mirrors `blizzard-api` and `simc`)

## Analytics And Provenance Posture

Following [SAFE_ANALYTICS_RULES.md](../foundation/SAFE_ANALYTICS_RULES.md):

- preserve raw source identifiers and URLs (addon id, slug, project/file URLs) alongside any
  normalized output
- treat any future normalization (compatibility, version, dependency mapping) as an additive layer,
  not a replacement for the raw metadata
- do not synthesize "best addon" answers; surface sample-backed, citable metadata agents can compose

## Shared vs Provider-Specific

Shared:
- output/error shaping
- cache and HTTP primitives
- wrapper search/resolve/doctor contract

Provider-specific:
- CurseForge API client, search/ranking behavior
- addon/file/changelog parsing
- compatibility/version normalization

## Deferred

- dependency graph, release/channel filtering, game-version compatibility, file-download workflows
- `search` / `resolve` beyond the `coming_soon` stub
- any write or user-auth flows

## Source Links

- `https://www.curseforge.com/wow/addons`
- [Roadmap](../ROADMAP.md)
