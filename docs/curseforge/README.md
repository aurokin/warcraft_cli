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

The GO scaffold shipped under
[AUR-499 — CurseForge provider scaffold (doctor + addon-lookup workflow)](https://linear.app/aurokin/issue/AUR-499).

## Implemented

The scaffold slice (AUR-499) ships the package, wrapper registration, `doctor`, and the first
addon-lookup workflow:

- `curseforge doctor` — reports install state, API-key auth posture (`api_key` flow,
  `CURSEFORGE_API_KEY`, credential source + lookup order), and capability metadata (`doctor` and
  `addon` are `ready`; `search`/`resolve` stay `coming_soon`).
- `curseforge addon <slug-or-id>` — resolves the addon (numeric → mod id, validated to be a WoW
  project via `gameId`; otherwise a `gameId=1` slug search whose result is matched to the exact slug
  client-side so an ignored/renamed filter can never bind the wrong mod), then returns
  `data.metadata` (the raw CurseForge mod record), `data.latest_files` (the mod's `latestFiles`), and
  `data.changelog` for the newest file. Changelog is best-effort: `null` when the addon has no files,
  or an explicit `{file_id, error}` marker if that one request fails — neither fails the whole lookup,
  and a failed fetch is never silently reported as "no changelog".
- Each command returns `{ok, provider, command, kind, query, provenance, data}` on success and
  `{ok:false, ..., error:{code, message}}` with a nonzero exit on failure (never a traceback).
  Error codes: `missing_api_key`, `addon_not_found`, `http_error`, `network_error`,
  `invalid_response`. Provenance carries `game_id`, `mod_id`, `slug`, `resolved_by`, and
  `source_urls`.
- Auth is a static API key (`x-api-key` header), discovered in this order (matching `blizzard-api`):
  1. repo `.env.local`
  2. `~/.config/warcraft/providers/curseforge.env`
  3. process environment

  Set `CURSEFORGE_API_KEY`. `doctor` never prints the key.
- The wrapper registers `curseforge` with `expansion_mode=none` (addon game-version compatibility
  lives inside file records, not the wrapper's expansion axis), so it stays out of expansion fanout.

> **Pending one-time live confirmation.** The host, endpoints, and response shapes follow the
> documented public CurseForge Core API but have **not** been confirmed against live endpoints in
> this repo. Every command payload carries `provenance.verified: false`. Run
> `CURSEFORGE_LIVE_TESTS=1 pytest -q -m live tests/test_curseforge_live.py` with a real
> `CURSEFORGE_API_KEY` to confirm them.

## Expected User Workflow

The committed first slice is addon lookup by slug or id, returning metadata, latest files, and
changelog in one provider:

```
warcraft curseforge doctor
warcraft curseforge addon <slug-or-id>   # metadata + latest files + changelog
```

`warcraft curseforge ...` routes through the wrapper to the `curseforge` provider; each command
returns the standard `{ok, provider, command, kind, query, provenance, data}` envelope on success
and `{ok:false, ..., error:{code, message}}` with a nonzero exit on failure (never a traceback).

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
