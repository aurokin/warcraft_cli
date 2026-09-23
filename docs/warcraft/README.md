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
| supported | lorrgs | `lorrgs` | Cooldown timeline rankings and cached report overviews; no auth. |
| experimental | raidbots | `raidbots` | Public report parsing and SimC input handoff; no search index. |
| experimental | blizzard-api | `blizzard` | Battle.net Game Data and Profile reads; unverified stub surfaces. |
| experimental | curseforge | `curseforge` | Addon metadata lookup; unverified stub surfaces. |

## Global flags

Global flags go before the subcommand and are forwarded to the provider CLI on passthrough:

```bash
warcraft --pretty wowhead search "defias"
warcraft --fields data.results,data.count search "mistweaver monk"
warcraft --expansion wotlk resolve "thunderfury"
```

- `--pretty`, `--compact`, `--compact-max-chars`, `--fields` (dot paths into objects; repeat or
  comma-separate), `--fields-strict`, `--profile`
- `--expansion <key>` filters fanout and pins expansion-aware providers (`wowhead --expansion`,
  `warcraftlogs --site`). Providers with no expansion axis (`simc`, `blizzard`, `curseforge`) pass
  through unchanged with an `expansion_advisory` note.

`warcraft search --brief` and `warcraft resolve --brief` shrink candidate rows and drop the
per-provider payloads; each brief row keeps the provider's `follow_up.command` as
`follow_up_command`. `--compact` is the global output flag only, and it truncates long strings in
any payload; the two no longer share a name. `--brief` never hides a provider failure:
`failed_providers`, `failed_provider_count`, and `answered_provider_count` stay in both shapes.

## Composite commands

Every command's flags are listed in [docs/reference/warcraft.md](../reference/warcraft.md) and in
`warcraft <command> --help`.

- `warcraft doctor` — wrapper and per-provider readiness: tier, auth, expansion support, runtime paths.
- `warcraft search` — fan out to every search-ready provider and rank the merged candidates. Each
  provider row carries `ok` and `error`, so a failed provider is distinguishable from an empty result.
  Each provider's scores are rescaled against that provider's own best row before the merge, so a
  provider with a larger local score scale cannot take every slot; the divisor has a floor, so a
  provider whose best row is weak does not get a full score for topping its own empty field.
  `count` is the merged candidate total and `truncated` says whether `--limit` cut it. The merged
  page interleaves the providers' own lists without ever reordering two rows from one provider,
  leads with Wowhead's top row when a bare query names it, applies a per-provider cap, ranks rows
  from a family the query did not ask for (a player profile for a bare item name) below the rest,
  and keeps one slot for a character or guild named exactly the query when no entity anchors the
  page; `merge_policy` reports the caps, the reserved slot and the rows they deferred or withheld,
  and each row carries the normalized `kind` the ranking used. A guide the provider flagged as
  superseded carries `wrapper_ranking.stale_guide: true`. See
  [WRAPPER_PROVIDER_CONTRACT.md](../foundation/WRAPPER_PROVIDER_CONTRACT.md) for the model.
- `warcraft resolve` — pick the single best match plus its follow-up command; never resolves to a
  provider that reported `resolved: false`. `selected_provider` is the match's provider or `null`;
  `provider` mirrors it when resolved and is `warcraft` when nothing matched. When nothing resolved,
  `fallback_search_command` and `best_unresolved_candidate` carry the next step instead of a dead end.
- `warcraft guild` / `guild-ranks` — one guild identity's Raider.IO snapshot, and its per-raid
  normal/heroic/mythic world, region, and realm ranks, with citations. `sources.raiderio.summary`
  reports every raid Raider.IO returned (`raids[]`, progression joined to its own ranks by
  `raid_slug`). There is no `active_raid`: Raider.IO orders those rows by slug and carries no raid
  start/end window, so naming one of them "active" would be a guess. Cross-reference
  `raiderio raids` when you need the currently running tier. Both keep the Raider.IO envelope
  itself: `guild` under `sources.raiderio.payload`, `guild-ranks` as `provider_payload`.
- `warcraft actor-profile` — cross-walk a Warcraft Logs report actor to a Raider.IO profile. Warcraft
  Logs only answers a fight-scoped roster query, so without `--fight-id` the wrapper reads the
  report's fight list first and scopes the lookup to a bounded set of fights, kills first.
  `query.scoped_fight_ids` names the fights that were actually read and `query.fight_scope` reports
  the rule, the report's fight count, and whether the scope was truncated. A report with no fights
  fails `report_has_no_fights` (exit 4), and a name missing from the fights read fails
  `actor_not_found` (exit 4) with `error.details.fight_scope`, so a miss inside a truncated scope
  says the other fights were not searched.
- `warcraft cooldown-packet` — compose Lorrgs phase windows with Warcraft Logs cast events for
  phase-scoped cooldown analysis. Lorrgs only serves reports it has already cached; for any other
  report — or when Lorrgs itself is unreachable — pass `--actor-id` and `--spec-slug` and the packet
  still returns the Warcraft Logs cast timeline with `lorrgs.status: "unavailable"` and
  `phase.status: "unavailable"`. `lorrgs.message` names the real reason (only a `not_found` is
  reported as "not cached") and `phase.requested` echoes the `--phase` that could not be applied.
  The top-parse comparison uses `--difficulty` when passed, otherwise the Warcraft Logs fight's own
  difficulty (heroic or mythic, echoed as `query.difficulty`); at any other difficulty it is skipped
  and `notes` says why.
- `warcraft guide-compare` — compare two or more already-exported guide bundles.
- `warcraft guide-compare-query` — resolve a guide query across wowhead, method, and icy-veins,
  export the bundles, and compare them. Flags: `--provider` (repeatable), `--out-root`, `--limit`,
  `--max-age-hours`, `--force-refresh/--no-force-refresh`,
  `--simc-build-handoff/--no-simc-build-handoff`, `--simc-apl-path`, `--simc-decode/--no-simc-decode`,
  `--simc-build-limit`. It writes `manifest.json` only when at least two bundles were exported.
  Without `--out-root` it writes under `<XDG data dir>/warcraft/guide_compare/<query-slug>`, never
  into the current directory.
- `warcraft talent-packet` / `talent-describe` — build a validated talent transport packet, optionally
  with simc `describe-build` output. Both report the file they wrote as `written_packet_path`.
- `warcraft guide-builds-simc` — turn explicit build references in exported bundles into a simc packet.
  Each reference is handed to simc in the form its type requires: a `wow_talent_export` string goes
  as `--build-text`, a Wowhead talent-calc URL as a validated transport packet. A reference that can
  go neither way is an `excluded_builds` row naming the reason, not a silently shorter list.
  `summary.simc_handoff_status` is `ok`, `partial`, `failed`, `no_build_references`, or
  `all_handoffs_failed`. The requested legs are `identify` plus `decode` (on by default) and
  `describe` (with `--apl-path`). `all_handoffs_failed` means every requested leg produced nothing:
  it is a `simc_handoff_failed` error envelope (exit 1, `kind: "error"`) whose `provenance` is the
  packet's and whose `error.details` carry the rest of the packet, per-build `failures` included. `failed` means some requested leg produced nothing
  while another produced output (`summary.empty_requested_legs` names the empty ones); `partial`
  means a leg worked for some builds and not others
  (`summary.partial_requested_legs`). Every build carries its own `failures` with the simc error
  code per leg, and `summary.failed_page_count` / `bundle_health` report pages the guide export
  never fetched, so a handoff built from a partial bundle says so.

## Errors and exit codes

Wrapper failures use the shared envelope and exit codes in
[docs/foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md). Wrapper validation errors
(`unsupported_provider_expansion`, `duplicate_expansion_argument`, `invalid_argument`,
`invalid_bundle`, `insufficient_guides`, `simc_handoff_failed`) exit 1; Typer usage errors exit 2.

Composite commands do not flatten a source failure: they re-emit the failing provider's own
`error.code` and exit with the code the contract maps it to (`not_found` -> 4, `auth_required` -> 3,
`network_error` -> 5). When every provider in a `search`/`resolve` fanout fails, the result is an
error envelope carrying the providers' shared code (or `upstream_error` when they disagree), with
the per-provider rows under `error.details.failed_providers` — never `ok: true` with an empty
result list. A wrapper envelope carries the envelope keys and nothing else: the payload is under
`data` on success, and all structured failure context is under `error.details`, never as a
sibling of `code`/`message`.

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
