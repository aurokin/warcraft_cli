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
| experimental | blizzard-api | `blizzard` | Battle.net Game Data and Profile reads (verified live); no search/resolve. |
| experimental | curseforge | `curseforge` | Addon metadata lookup (verified live); no search/resolve. |

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
`follow_up_command`. `--compact` is the global output flag only, and it truncates long prose strings in
any payload; the two no longer share a name. `--brief` never hides a provider failure:
`failed_providers`, `failed_provider_count`, and `answered_provider_count` stay in both shapes, and
so does `provider_warnings` (`[{provider, key, warning}]`, every `*_warning` a provider put in its
provenance, such as Icy Veins' stale-sitemap warning).

## Composite commands

Every command's flags are listed in [docs/reference/warcraft.md](../reference/warcraft.md) and in
`warcraft <command> --help`.

- `warcraft doctor` — wrapper and per-provider readiness: tier, auth, expansion support, runtime paths.
  A provider with a cache failure carries it in `cache_error` as `{code, message}` (`null`
  otherwise): `cache_unavailable` for an unreachable Redis, `invalid_cache_config` for an invalid
  `<PROVIDER>_CACHE_BACKEND`. The row's `status` is `degraded`, or `error` when the provider's own
  doctor failed because of it.
- `warcraft search` — fan out to every search-ready provider and rank the merged candidates. Each
  provider row carries `ok` and `error`, so a failed provider is distinguishable from an empty result.
  Each provider's scores are rescaled against that provider's own best row before the merge, so a
  provider with a larger local score scale cannot take every slot; the divisor has a floor, so a
  provider whose best row is weak does not get a full score for topping its own empty field. A row
  its provider says does not cover the query (a Lorrgs row with `unmatched_terms`, a wiki row that
  matched only on its snippet) is not rescaled up at all (`wrapper_ranking.covers_query: false`), so
  it cannot outrank another provider's title match.
  `count` is the rows on the page and `truncated` says whether `--limit` cut the merged candidates;
  `merge_policy.provider_total_matches` keeps each provider's own `total_matches` (`null` for a stub or
  a provider in `failed_providers`). The merged
  page interleaves the providers' own lists without ever reordering two rows from one provider,
  leads with Wowhead's top row when a bare query names it, applies a per-provider cap, ranks rows
  from a family the query did not ask for (a player profile for a bare item name) below the rest,
  and keeps one slot for a character or guild named exactly the query when no entity anchors the
  page; `merge_policy` reports the caps, the reserved slot and the rows they deferred or withheld,
  and each row is the provider's own row (its `provider`, `kind`, `url`, `ranking` and `follow_up`)
  plus `wrapper_ranking`. A guide the provider flagged as
  superseded carries `wrapper_ranking.stale_guide: true`. See
  [WRAPPER_PROVIDER_CONTRACT.md](../foundation/WRAPPER_PROVIDER_CONTRACT.md) for the model.
- `warcraft resolve` — the single best match plus its follow-up command. Each provider's match is
  ranked exactly as `warcraft search` ranks that provider's top row, matches their own provider rated
  `low` are skipped, and the top-ranked remaining one is the answer only when its own provider
  resolved it (at `high` confidence) and the query's intent does not rank that
  provider's family down (a guide query is never answered by Lorrgs spec metadata, a guild query
  never by a wiki article); a match whose title is exactly the query (the item `Guild Tabard`) is
  exempt. Otherwise `resolved` is `false`, the top-ranked candidate is `data.best_unresolved_candidate`
  (a `low` match keeps its rank here: it often means two right pages tied, so an off-intent `medium`
  match does not lead the hints), and the candidate itself carries
  `unresolved_reason` (`data.best_unresolved_candidate.unresolved_reason`:
  `provider_did_not_resolve`, `provider_family_ranked_down_by_query_intent`, or
  `single_word_query_not_named_exactly` when its provider capped a one-word query's answer at `medium`
  because the word does not name it, as its `confidence_cap` says). Any lower match its provider
  resolved is in `data.provider_resolved_candidates` with its `next_command`, so an answer a
  better-ranked `medium` match blocked stays one command away; `--ranking-debug` lists every match
  with its `resolved` flag. `data` also lists the `fallback_search_command`s of the providers that
  returned a candidate, in ranking order (none when no provider found anything). `search` and
  `resolve` ask the providers concurrently and list them in registry order.
  `--limit` only sizes `--ranking-debug`: providers are never asked for fewer candidates, because
  their confidence is judged against the rivals a small limit would hide. The envelope's `provider`
  is `warcraft`; `data.selected_provider` is the match's provider or `null`.
- When no included provider searches the query (`--expansion fresh` leaves only Warcraft Logs, which
  matches only explicit report references), `search` and `resolve` fail `no_searching_provider`
  (exit 2) with the included and excluded providers in `error.details`.
- `warcraft guild` — one guild identity's Raider.IO snapshot, with citations. An Oceanic region
  alias (`oce`, `oceanic`) is looked up as `us`, the region Oceanic realms belong to.
  `sources.raiderio.summary.raids[]` reports every raid Raider.IO returned, progression joined to its
  own normal/heroic/mythic world, region, and realm ranks by `raid_slug` (`0` means unranked). There
  is no `active_raid`: the raiderio CLI sorts those rows by raid slug; Raider.IO's payload carries no
  raid start/end window, so naming one of them "active" would be a guess. Cross-reference `raiderio raids` when you need the
  currently running tier. The Raider.IO call's provenance is `sources.raiderio.provenance`; the raw
  envelope is not repeated (`warcraft raiderio guild` returns it).
- `warcraft actor-profile` — cross-walk a Warcraft Logs report actor to a Raider.IO profile. The report
  is a report URL or a bare code, read by the `warcraftlogs` rule: a URL's `fight=<id>` scopes the
  lookup when `--fight-id` is absent, and a blank or non-alphanumeric code or a URL on a host other
  than warcraftlogs.com fails `invalid_query` (exit 2) before any request. Warcraft
  Logs only answers a fight-scoped roster query, so without `--fight-id` the wrapper reads the
  report's fight list first and scopes the lookup to a bounded set of fights, kills first.
  `query.scoped_fight_ids` names the fights that were actually read and `query.fight_scope` reports
  the rule, the report's fight count, and whether the scope was truncated. A report with no fights
  fails `report_has_no_fights` (exit 4), and a name missing from the fights read fails
  `actor_not_found` (exit 4) with `error.details.fight_scope`, so a miss inside a truncated scope
  says the other fights were not searched. Warcraft Logs names a server by its space-stripped name;
  for a localized one (`Ревущийфьорд`) the Raider.IO lookup uses the realm slug from the report's own
  realm rows (`howling-fjord`). Raider.IO's spec is the character's current active spec
  (`sources.raiderio.spec_source: "active_spec"`), so a log played in another spec reports
  `reconciliation.reasons: ["spec_mismatch"]` as expected; only `class_mismatch` casts doubt on the join.
- `warcraft cooldown-packet` — compose Lorrgs phase windows with Warcraft Logs cast events for
  phase-scoped cooldown analysis. Lorrgs only serves reports it has already cached; for any other
  report — or when Lorrgs itself is unreachable — the packet still returns the Warcraft Logs cast
  timeline with `lorrgs.status: "unavailable"`. The actor (`--actor-id` or `--actor-name`) is found in
  the Warcraft Logs roster of the fight, which also supplies the spec, so `--spec-slug` is needed only
  when the roster names none (`spec_slug_missing`, exit 2); without either actor flag the command fails
  `missing_actor` (exit 2) with the roster in `error.details.available_players`. While Lorrgs is down,
  also pass `--spell-id` for each cooldown (they are then named `spell:<id>`).
  `lorrgs.message` names the real reason (only a `not_found` is reported as "not cached"). The phase
  windows then come from the Warcraft Logs fight's phase transitions (`phase.source:
  "warcraftlogs"`; `"lorrgs"` when Lorrgs served the fight): windows are numbered P1, P2, ... in
  order, one per transition, and each window carries the encounter phase's `phase_id` and `name` (a
  boss that returns to phase 1 has P1 and P3 both with `phase_id` 1). Lorrgs places its own markers
  (on some bosses only the intermission ends), so its P2 can be a different stretch of the fight.
  The top parses are then segmented by their own Warcraft Logs phase transitions
  (`sources.warcraftlogs_top_parse_phase_transitions`) the same way. A sample whose window of that
  number is another encounter phase gets `phase_unavailable_reason: "phase_not_in_top_parse"`, and one
  whose transitions could not be read gets `"top_parse_phase_lookup_failed"`. A fight with no
  phase transitions, or a failed lookup (`sources.warcraftlogs_phase_transitions.error`), leaves
  `phase.status: "unavailable"`, `phase.selected` null, and `phase.requested` echoing the `--phase`
  that could not be applied.
  A fight Lorrgs cached without its players degrades the same way (`lorrgs.reason:
  "lorrgs_fight_has_no_players"`). Without Lorrgs the player's name and class come from the Warcraft
  Logs roster of the selected fight, so an `--actor-id` (or `--actor-name`) that fight lacks fails
  `actor_id_not_found` (or `actor_name_not_found`, exit 4) even when the player is elsewhere in the report, and `player.deaths` is `null` (deaths
  come only from the Lorrgs timeline). `--spec-slug` takes any provider's spelling
  (`frost-death-knight`, `BeastMastery`, `balance-druid`) and becomes the Lorrgs slug in `query.spec_slug`;
  a bare spec two classes share (`frost`) takes the player's class. A `--spec-slug` of another
  class than the player's fails `invalid_query` (exit 2). Casts are counted only for the actor:
  `cooldowns.player_casts.other_source_cast_count` counts rows from anyone else. When Lorrgs omits a
  fight's duration, the last phase window has `end_ms: null` (open-ended).
  The top-parse comparison uses `--difficulty` when passed, otherwise the Warcraft Logs fight's own
  difficulty (heroic or mythic, echoed as `query.difficulty`). It needs a Lorrgs boss slug: a
  Lorrgs-cached report names it, otherwise the Lorrgs boss whose id is the Warcraft Logs fight's
  encounter id (`sources.lorrgs_bosses`); `--boss-slug` overrides both. Lorrgs' `other-externals`
  (Power Infusion, Bloodlust, Ironbark and the like) are recorded on top parses as buffs received, so
  the ones the player did not cast in the fight are left out of `tracked_spells` and the comparison
  and listed in `cooldowns.received_auras`; one the player cast (a priest's Power Infusion) and any
  `--spell-id` stay compared. An external the player owns but never pressed is therefore not reported
  as missed. When the analyzed
  fight is itself a top parse it is skipped (`comparison.excluded_analyzed_fight: true`), so the
  player is never compared with themselves. When the comparison
  does not run, `comparison.reason` says why (`no_boss_slug`, `unranked_difficulty`,
  `lorrgs_spec_ranking_failed`, `disabled_by_sample_limit`) and a note names the flag that fixes it.
  Lorrgs ranking fights often carry no phase markers. When the encounter has several phases (the
  player's fight or any top-parse fight shows a phase transition) such a sample gets
  `phase_available: false` with `phase_unavailable_reason: "top_parse_has_no_phase_markers"` instead
  of its whole-fight casts; a sample whose markers stop before the phase gets
  `"phase_not_in_top_parse"`. `sample_fraction` counts only the `phase_sample_count` samples that have
  the phase, and when none has it `comparison.status` is `no_phase_data` with `reason` set to the
  samples' shared reason (`no_sample_has_phase` when they differ). `phase_sample_count` is 0 when
  the comparison did not run. When neither the player's fight nor any top parse has phase markers,
  each top parse's whole fight stands for P1.
  `notes` only describe what the packet actually holds, and say when Warcraft Logs truncated the
  cast events or Lorrgs' boss spell names were unavailable. A fight id the Warcraft Logs report does
  not have fails `fight_not_found` (exit 4) with `error.details.available_fight_ids`; an actor
  missing from the Lorrgs or Warcraft Logs roster fails `actor_id_not_found` / `actor_name_not_found`
  (exit 4).
- `warcraft guide-compare` — compare two or more already-exported guide bundles.
  `comparison_evidence.subjects` names the class and spec each guide's title names (spec `null` for a
  class hub) and `subject_agreement` is `agree`, `mixed` (different specs, or a class hub next to a
  spec guide, so shared sections compare unlike guides) or `unknown`.
  `comparison_evidence.bundles_without_build_references` lists the bundles that hold none. A build
  is `shared` when every bundle that can hold builds has it: a Wowhead export never holds any and is
  left out, and at least two bundles must hold builds for any to be shared.
  `freshness` is export recency; `bundles[].content_updated_at` (also on each
  `comparison_evidence.bundle_freshness` row) is when the site last changed the guide (Wowhead's
  `dateModified`, Method's and Icy Veins' last-updated date), `null` when the export did not record it.
- `warcraft guide-compare-query` — resolve a guide query across wowhead, method, and icy-veins,
  export the bundles, and compare them. Each provider is asked for guides only, so Wowhead answers
  `frost mage` with its guide rather than the spell. A provider's guide is its resolved guide, else its search top
  hit when that hit is a guide with a strong score and a clear margin; each provider row names why
  it declined (`reason` for the search step, `resolve_reason` for the resolve step; a failed call
  makes the row `status: error`, `reason: provider_failed` with its `error`). It writes
  `manifest.json` only when at least two bundles were exported. Without `--out-root` it writes under
  `<data root>/guide_compare/<query-slug>` (the `paths.data_root` that `warcraft doctor` reports: the
  checkout's `.warcraft/runtime/data` for a checkout install, `<XDG data dir>/warcraft` for a wheel),
  never into the current directory. `manifest.json` stores each `bundle_path` relative to the root, so
  a copied or moved root reads its own bundles. Flags that
  leave fewer than two providers (a single `--provider`, or a non-retail `--expansion`, since method
  and icy-veins are retail-only) fail `invalid_argument` (exit 2) before any provider call, naming
  which of the two it was. Fewer than
  two bundles fails `insufficient_guides` (exit 1), except when every provider that contributed
  nothing failed outright (resolve/search or `guide-export` returned an error): then the run fails
  with those providers' shared code and exit code (a network outage is `network_error`, exit 5),
  and each `provider_results` row carries its `error`. With `--simc-build-handoff`, a handoff whose
  status is `all_handoffs_failed` fails `simc_handoff_failed` (exit 1) exactly as
  `guide-builds-simc` does, with the comparison under `error.details`. An exported or reused
  `provider_results` row carries the provider's `redirect` (non-null when it served another guide
  than the resolved candidate, such as a retired page); `manifest.json` saves it for reuse.
- `warcraft talent-packet` / `talent-describe` — build a validated talent transport packet, optionally
  with simc `describe-build` output. Both report the file they wrote as `written_packet_path`.
  With `--actor-id`, a bare word is read as a Warcraft Logs report code by the providers' own rule
  (16 mixed-case letters and digits, or 8 to 32 with a digit), so a name such as `HavocDemonHunter`
  fails `unsupported_talent_source` (exit 2) instead of reaching Warcraft Logs. A Warcraft Logs report
  ref without `--actor-id` fails `missing_actor_id` (exit 2), naming `report-player-details` to find
  it. `talent-describe` with an `--apl-path` of another spec than the build's fails `invalid_query`
  (exit 2), as `simc describe-build` does.
  `producer_result` and `upgrade_result` are `{provider, exit_code, payload}`, the provider's parsed
  envelope only.
  simc reads the packet in memory, so its output cites a packet file only when one holds that
  packet: `describe_result`'s `build_spec.transport_packet.path` (and its `build packet:` source
  note) is the `--packet-out` file or an unchanged packet-file source, and is absent otherwise
  (`--packet-out` is written after describe succeeds, so a `transport_packet_write_failed` error's
  `provider_result` names the path that could not be written);
  `upgrade_result.payload.data.input.build_packet` is the packet file the source was read from, or
  `null` for a routed packet.
- `warcraft guide-builds-simc` — turn explicit build references in exported bundles into a simc packet.
  Each reference is handed to simc in the form its type requires: a `wow_talent_export` string goes
  as `--build-text`, a Wowhead talent-calc URL as a validated transport packet held in memory (its
  simc payloads cite no packet file). A reference that can
  go neither way is an `excluded_builds` row naming the reason, not a silently shorter list.
  `--limit` takes one build per provider in turn (bundle order), so it never drops a whole provider;
  the builds past it are `summary.truncated_build_count`, apart from `summary.excluded_build_count`.
  For an orchestration root, `freshness.sampled_at` is the oldest bundle's export time (`reason:
  "oldest_bundle_exported_at"`); `freshness.manifest_updated_at` is when the root's manifest was last
  rewritten, which every run does.
  `summary.simc_handoff_status` is `ok`, `partial`, `failed`, `no_build_references`,
  `all_references_excluded` (the bundle had build references but every one is in `excluded_builds`),
  or `all_handoffs_failed`. The requested legs are `identify` plus `decode` (on by default) and
  `describe` (with `--apl-path`). `all_handoffs_failed` means every requested leg produced nothing:
  it is a `simc_handoff_failed` error envelope (exit 1, `kind: "error"`) whose `provenance` is the
  packet's and whose `error.details` carry the rest of the packet, per-build `failures` included. With
  `--no-decode`, a bundle in which no build is identified fails this way; its message names
  `build_not_identified` and points at the guide hashes, not at `simc doctor`. `failed` means some requested leg produced nothing
  while another produced output (`summary.empty_requested_legs` names the empty ones); `partial`
  means a leg worked for some builds and not others
  (`summary.partial_requested_legs`). Every build carries its own `failures` with the simc error
  code per leg, and `summary.failed_page_count` / `bundle_health` report pages the guide export
  never fetched, so a handoff built from a partial bundle says so.

## Errors and exit codes

Wrapper failures use the shared envelope and exit codes in
[docs/foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md). Every failure envelope has
`kind: "error"`. `invalid_argument` (for example `guide-compare` with one bundle, or
`guide-compare-query --provider` naming an unsupported provider, or selecting fewer than two
providers for the expansion) and Typer usage errors exit 2. `guide-compare` reports a bundle path the way the shared bundle loader
does: `not_found` (exit 4) when it is missing, `invalid_argument` (exit 2) when it is a file, and
`invalid_bundle` (exit 1) when it is not a readable bundle. `unsupported_provider_expansion` and
`duplicate_expansion_argument` are argument mismatches and exit 2, as do `invalid_report_ref`,
`missing_fight` and `missing_actor` from `cooldown-packet`, `unsupported_talent_source` from
`talent-packet`/`talent-describe`, and `no_searching_provider` from `search`/`resolve`. A
`talent-packet`/`talent-describe` packet path that does not exist fails `not_found` (exit 4). `insufficient_guides`, `simc_handoff_failed` and `providers_failed` exit 1.

Composite commands do not flatten a source failure into exit 1: they exit with the code the contract
maps the source's error to (`not_found` -> 4, `auth_failed` -> 3, `network_error` -> 5).
`talent-packet` and `talent-describe` re-emit the source's own `error.code`; `actor-profile` and
`cooldown-packet` name the step that failed (for example `warcraftlogs_lookup_failed`) and carry the
source's error under `error.details.source`. When every provider in a `search`/`resolve` fanout
fails, the result is an error envelope carrying the providers' shared code and exit code; when they
disagree it is `upstream_error` (exit 5) if every one failed upstream, otherwise
`providers_failed` (exit 1). The per-provider rows, each with its `exit_code`, are under
`error.details.failed_providers` — never `ok: true` with an empty result list. A wrapper envelope carries the envelope keys and nothing else: the payload is under
`data` on success, and all structured failure context is under `error.details`, never as a
sibling of `code`/`message`.

## What the wrapper does not do

- It does not hide source provenance: provider payloads stay reachable under `providers[].payload`.
- It does not impose one universal data model across article sites, APIs, and local tools.
- It does not host parsers, API schemas, SimC execution, or provider-specific ranking logic.
- It does not route through stubbed provider surfaces as if they were production search or resolve.
- It does not run provider binaries: `search`, `resolve` and `doctor` call each provider's in-process
  `PROVIDER` surface, the simc build steps call `simc_cli` functions, and the other composites and
  passthrough run the provider's Typer app in-process (composites capture its JSON output).

## Source links

- [REPO_STRUCTURE_AND_PACKAGING.md](../architecture/REPO_STRUCTURE_AND_PACKAGING.md)
- [Roadmap](../ROADMAP.md)
