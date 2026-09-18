# Warcraft CLI

`warcraft` is the root router for this repo. It decides which backing provider answers a question,
proxies to that provider's own CLI when the source is already known, and composes a small number of
cross-provider workflows. Provider-specific logic lives in the provider packages, not here.

## Provider tiers

Tiers describe how much depth an agent should expect. `warcraft doctor` reports them under
`wrapper.tiers` and per provider as `tier`.

| Tier | Provider | `warcraft <command>` | One line |
| --- | --- | --- | --- |
| core | wowhead | `wowhead` | Entity/guide search, resolve, retrieval, and expansion profiles. |
| core | warcraftlogs | `warcraftlogs` | Report analytics; discovery is limited to explicit report references. |
| core | simc | `simc` | Local SimulationCraft repo inspection, build decoding, and runs. |
| supported | raiderio | `raiderio` | Character/guild profiles and Mythic+ leaderboards. |
| supported | warcraft-wiki | `warcraft-wiki` | MediaWiki-backed reference article search and export. |
| supported | icy-veins | `icy-veins` | Guide search, resolve, and guide bundle export. |
| supported | method | `method` | Guide search, resolve, and guide bundle export. |
| experimental | lorrgs | `lorrgs` | Cooldown timeline rankings and cached report overviews. |
| experimental | raidbots | `raidbots` | Public report parsing and SimC input handoff; no search index. |
| experimental | blizzard-api | `blizzard` | Battle.net Game Data and Profile reads; unverified stub surfaces. |
| experimental | curseforge | `curseforge` | Addon metadata lookup; unverified stub surfaces. |

## Global flags

Global flags go before the subcommand and are forwarded to the provider CLI on passthrough:

```bash
warcraft --pretty wowhead search "defias"
warcraft --fields results,count search "mistweaver monk"
warcraft --expansion wotlk resolve "thunderfury"
```

- `--pretty`, `--compact`, `--compact-max-chars`, `--fields` (dot paths into objects; repeat or
  comma-separate), `--fields-strict`, `--profile`
- `--expansion <key>` filters fanout and pins expansion-aware providers (`wowhead --expansion`,
  `warcraftlogs --site`). Providers with no expansion axis (`simc`, `blizzard`, `curseforge`) pass
  through unchanged with an `expansion_advisory` note.

`warcraft search --compact` and `warcraft resolve --compact` are separate command-level flags that
shrink candidate rows; the global `--compact` truncates long strings in any payload.

## Composite commands

Every command's flags are listed in [docs/reference/warcraft.md](../reference/warcraft.md) and in
`warcraft <command> --help`.

- `warcraft doctor` — wrapper and per-provider readiness: tier, auth, expansion support, runtime paths.
- `warcraft search` — fan out to every search-ready provider and rank the merged candidates. Each
  provider row carries `ok` and `error`, so a failed provider is distinguishable from an empty result.
- `warcraft resolve` — pick the single best match plus its follow-up command; never resolves to a
  provider that reported `resolved: false`. `selected_provider` is the match's provider or `null`;
  `provider` mirrors it when resolved and is `warcraft` when nothing matched.
- `warcraft guild` / `guild-ranks` — one guild identity's Raider.IO snapshot, and its per-raid
  normal/heroic/mythic world, region, and realm ranks, with citations.
- `warcraft actor-profile` — cross-walk a Warcraft Logs report actor to a Raider.IO profile.
- `warcraft cooldown-packet` — compose Lorrgs phase windows with Warcraft Logs cast events for
  phase-scoped cooldown analysis.
- `warcraft guide-compare` — compare two or more already-exported guide bundles.
- `warcraft guide-compare-query` — resolve a guide query across wowhead, method, and icy-veins,
  export the bundles, and compare them. Flags: `--provider` (repeatable), `--out-root`, `--limit`,
  `--max-age-hours`, `--force-refresh/--no-force-refresh`,
  `--simc-build-handoff/--no-simc-build-handoff`, `--simc-apl-path`, `--simc-decode/--no-simc-decode`,
  `--simc-build-limit`. It writes `manifest.json` only when at least two bundles were exported.
- `warcraft talent-packet` / `talent-describe` — build a validated talent transport packet, optionally
  with simc `describe-build` output.
- `warcraft guide-builds-simc` — turn explicit build references in exported bundles into a simc packet.

## Errors and exit codes

Wrapper failures use the shared envelope and exit codes in
[docs/foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md). Wrapper validation errors
(`unsupported_provider_expansion`, `duplicate_expansion_argument`, `invalid_argument`,
`invalid_bundle`, `insufficient_guides`) exit 1; Typer usage errors exit 2.

## What the wrapper does not do

- It does not hide source provenance: provider payloads stay reachable under `providers[].payload`.
- It does not impose one universal data model across article sites, APIs, and local tools.
- It does not host parsers, API schemas, SimC execution, or provider-specific ranking logic.
- It does not route through stubbed provider surfaces as if they were production search or resolve.
- It does not run provider binaries: providers are called in-process through their `PROVIDER` surface,
  and passthrough invokes the provider's Typer app directly.

## Source links

- [REPO_STRUCTURE_AND_PACKAGING.md](../architecture/REPO_STRUCTURE_AND_PACKAGING.md)
- [Roadmap](../ROADMAP.md)
