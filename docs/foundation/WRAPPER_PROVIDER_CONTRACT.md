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
workflows, so the wrapper does. `guild` is an identity-normalizing wrapper over the single guild
provider (`raiderio`) and keeps the source/provenance shape so a second source can be added without
changing the contract. The line the wrapper must not cross is *parsing or
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
discovery surface — is declared `not_supported` in the registry, and its in-process `PROVIDER`
surface still answers with a structured stub instead of a crash; the provider binary does not need
the command (`raidbots` has no `search` or `resolve`). Either way the wrapper contract stays stable
and the registry, not a special case in wrapper code, says which it is.

## Capability Expectations

### `search`

Purpose:
- return service-specific candidate matches for a free-text query

Minimum behavior:
- accept a query string
- return a structured result list
- return a `coming_soon` (or `not_supported`) stub if not implemented yet: the same shape with an
  empty list, the flag set in `data`, and `message` / `suggested_command`

Shared shape (`warcraft_core.discovery` builds it, `tests/test_discovery_contract.py` holds every
provider to it): the envelope `kind` is `search_results`; `data` carries `search_query`, `results`,
`count` (the rows in `results`), `total_matches` (every match the provider knows of: the upstream's
own hit count where it reports one, `null` only on a stub) and `truncated` (more matches exist than
`results` holds). Every row carries `provider`, `kind`, `id`, `name`, `url` (`null` when the row has
no page), `ranking {score, match_reasons}` and `follow_up {command, surface}`, plus the provider's own
keys. A row nothing can open has `follow_up.command: null` and `follow_up.surface: "none"`.

### `resolve`

Purpose:
- return the best next service-specific command for a query when confidence is high

Minimum behavior:
- accept a query string
- return either a candidate resolution or a structured unresolved response
- return a `coming_soon` (or `not_supported`) stub if not implemented yet, with `confidence: "none"`

Shared shape: the envelope `kind` is `resolve_match`; `data` carries `search_query`, `resolved`,
`confidence` (`high`, `medium`, `low` or `none`), `match`, `next_command`, `fallback_search_command`,
`candidates`, `count`, `total_matches` and `truncated`, the last three meaning what they mean for
`search`. `resolved` is true exactly when `confidence` is `high`, and only then is `next_command` set
(to `match.follow_up.command`). `match` is the top candidate whenever there is one, resolved or not.
An unresolved answer with a `match` carries the provider's `fallback_search_command`; one with no
match carries `null`, since the same search would come back empty. Warcraft Logs also leaves it
`null` on an unresolved bare report code: it matches only explicit report references, so its search
adds nothing.

One-word rule: when the provider's `search_query` (after its own hint, type and follow-up
stripping) is one plain alphabetic word, a `high` answer stands only if that word names the match,
by its whole name or its head before the first `,` or `:` (`warcraft_core.discovery.title_match`, which
the wrapper's `name_match` ranking also uses),
or the provider declares the row an identity match (a `single_word_identity` callable passed to
`resolve_data`; each provider keeps its own rules, there is no shared reason vocabulary). Otherwise
`resolve_data` lowers the answer to `medium` and adds `confidence_cap: {"rule": "single_word_query",
"from": "high"}`. A token with digits, `/`, `:`, `.` or `_` is never a plain word, so URLs, numeric ids
and dotted or namespaced identifiers are never capped. Letters-only tokens are plain words, including
report codes (`JVFTxcKCqrvpaAzD`), API names (`SetPoint`) and hyphenated slugs (`mage-frost`): they stay
`high` only through the provider's identity rule or because the provider never answers them at `high`.

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
    @property
    def name(self) -> str: ...  # read-only, so frozen dataclasses satisfy it
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
them. A wrapper envelope carries those keys and nothing else: its payload is under `data` on
success, and its failure context is under `error.details` (with `data: {}`). Provider rows inside
`warcraft search` and `warcraft resolve` output carry `ok` and `error` from the underlying call
alongside the registry `status`.

Fanout failure rules:
- `failed_providers`, `failed_provider_count`, and `answered_provider_count` are always present, in
  both the default and the `--brief` shape, so a dead fanout is never indistinguishable from an
  empty one; `provider_warnings` likewise lifts every `*_warning` in a provider's provenance out of
  the per-provider rows `--brief` drops
- a provider row's `answered` says whether the provider actually looked the query up. An
  explicit-report-only provider (Warcraft Logs) answers free text with a locally built hint and no
  rows, so it is `ok` but not `answered`, and `answered_provider_count` does not count it; a report
  reference it matched (search rows, or a resolve `match` even when unresolved) is an answer
- when no provider answered and at least one failed, the wrapper emits an error envelope whose
  `error.code` and exit code are the failed providers' shared ones, so a total outage exits 5
  instead of returning an ok:true empty page. When they disagree the code is `upstream_error`
  (exit 5) if every provider failed upstream, otherwise `providers_failed` (exit 1): a crash or a bad
  argument must not read as "retry later". The rows, each with the provider's `exit_code`, are under
  `error.details.failed_providers`
- when no provider answered and none failed, no included provider searched the query (an expansion
  filter left only explicit-report-only providers, or none), so the wrapper fails
  `no_searching_provider` (exit 2) with `requested_expansion`, `included_providers` and
  `excluded_providers` in `error.details` instead of an ok:true empty page

Composite failure rules:
- a composite command exits with the code the contract maps its failing source's error to. The
  talent routes re-emit the source's own `error.code`; `actor-profile` and `cooldown-packet` name
  the step that failed (`warcraftlogs_lookup_failed`, `lorrgs_spec_ranking_failed`, ...) and put the
  source's error under `error.details.source`
- every wrapper failure envelope has `kind: "error"`
- structured context belongs under `error.details`, never as a sibling of `code`/`message`. A
  failure envelope carries no `data` body, so a composite that declines (for example
  `guide-compare-query` with fewer than two exported bundles) puts the per-provider reasons in
  `error.details`

Reading a provider payload:
- every wrapper composite reads provider fields from the envelope's `data` body, through
  `warcraft_cli.providers.provider_payload_data`; `data` is the only place an envelope carries them
- test fakes for provider calls must emit the same shape (fields under `data`), or they keep a
  broken composite green

## Provider Tiers

Every registration declares a tier. `warcraft doctor` reports `wrapper.tiers` and a `tier` per
provider row, and [ROADMAP.md](../ROADMAP.md) and `README.md` use the same membership.

| Tier | Providers | Meaning |
|------|-----------|---------|
| core | `wowhead`, `warcraftlogs`, `simc` | deepest surface and contracts; the product |
| supported | `method`, `icy-veins`, `raiderio`, `warcraft-wiki`, `lorrgs` | real, narrower surfaces expected to work |
| experimental | `raidbots`, `blizzard-api`, `curseforge` | thin or unproven surfaces; `blizzard-api` payloads report `provenance.verified: true` for `us`/`eu`/`kr`/`tw` and `false` for `cn`, and `curseforge` addon payloads report `true` |

Tier is descriptive, not a permission: it tells an agent how much to trust the surface before
building a workflow on it.

## Provider Registration

The wrapper should know for each provider:
- provider name
- command name
- support tier
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
- `warcraft-wiki` -> `retail`
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
- the wrapper may apply a thin cross-provider ranking layer on top of provider-local scores
- that wrapper layer should be query-aware and use signals like provider family, result kind, and structured query hints
- that wrapper layer may also use provider-specific boosts for certain intents, such as preferring `raiderio` for character-profile and guild-profile queries
- wrapper ranking must stay inspectable in output, not hidden behind opaque ordering
- provider scores are not comparable across providers, so each candidate's score is rescaled against
  its own provider's best row for the query before the merge; `wrapper_ranking` keeps the raw
  `provider_score` and the `provider_max_score` divisor so the rescale is auditable
- the rescale divisor has a floor, so a provider whose best row is weak is scaled down rather than
  promoted to a full score for topping its own empty field. The floor is one shared, documented
  constant — `MINIMUM_PROVIDER_SCORE_SCALE` in `warcraft_cli.provider_contract`, `40` today —
  calibrated so a genuine hit clears it on every provider's own scale: a Wowhead exact name scores
  30 before prefix, term and popularity credit, a Raider.IO exact structured match adds 45 to its
  base of 12, and the Warcraft Wiki adds 50 for an exact title. A provider whose whole answer is a
  two-term text match scoring 3 therefore normalizes to `round(100 * 3 / 40) = 8`, not to 100
- a row whose provider says it does not cover the query is not rescaled up at all: its divisor floor
  is `UNCOVERED_ROW_SCORE_SCALE` (`100`), and `wrapper_ranking.covers_query` is `false` with an
  `uncovered_query:scale_floor:100` reason. Two providers say so: a Lorrgs row with non-empty
  `ranking.unmatched_terms`, and a Warcraft Wiki row whose `match_reasons` hold none of the wiki's
  query-coverage reasons and no `query_contains_title` (a snippet, upstream-position or family match
  only). A live corpus (2026-10-03) moved Icy Veins' `PvP DPS Tier List` above the wiki's `Events`
  for `pvp tier list`, and the wiki's `Mimiron's Head` and `Captain Fareeya` above Lorrgs comp
  rankings that matched one word; exact and title matches (`un'goro crater`, `class hall`,
  `C_Spell.GetSpellInfo`, `thunderfury`, `druid`) kept their order
- changing the floor or the per-provider scales is a contract change: record the calibration
  evidence here, because a floor set above a provider's real ceiling would silently demote that
  provider in every merged list
- the wrapper should not invent a fake universal content model beyond that thin ranking/orchestration layer
- `count` is the rows on the merged page, `truncated` reports whether `--limit` cut the merged
  candidates (`merge_policy.candidate_row_count`), and `merge_policy.provider_total_matches` keeps
  each provider's own `total_matches` (also under `--brief`); a provider in `failed_providers` is
  `null` there, like a stub
- merged rows are the providers' own rows, `provider` and `kind` included, plus `wrapper_ranking`

### The merged page: intent, order, diversity, quality

Score arithmetic alone cannot order a merged page: a provider that returns twenty equally scored
rows normalizes all twenty to 100 and owns every slot. Four structural rules decide the page, and
each one is visible in the payload.

**Intent — what kind of thing was asked for.** `query_intents()` reads the query for the keywords and
shapes in the ranking policy. A structured profile query is either `<region> <realm...> <name>` (at
least three words, the first a Raider.IO region — `us`, `eu`, `kr`, `tw`, `cn` — so multi-word realms
such as `eu tarren mill Cotti` and `us area 52 Roguecane` count) or a query carrying a
`guild`/`character` token. `world` is a leaderboard scope, not a region, so `world boss sha of anger`
is free text. A bare name carrying none of these and no keyword is *not* a profile query:
- a profile-family row (Raider.IO) answering a query with no profile intent is marked
  `wrapper_ranking.off_intent` and sorts below every on-intent row, whatever its local score. It
  takes a page slot only when the on-intent rows cannot fill the page, with one exception: when the
  page is not anchored and the first off-intent row's name is exactly the query, one slot is kept for
  it (`merge_policy.reserved_exact_profile_slot_count`), so `warcraft search <character name>` still
  shows the character beside the wiki's fuzzy matches.
- the entity provider's own top row anchors the page (`wrapper_ranking.anchor`) when its title is the
  bare query or starts with it (`Thunderfury, Blessed Blade of the Windseeker` for `thunderfury`):
  the entity a user named is the primary answer, and another provider's article *about* that entity
  is supporting reference, however large its local scale. Only a provider's top row can anchor, so a
  same-named row Wowhead ranked lower (the `Thunderfury` proc spells) never jumps ahead of it. An
  anchored page keeps no profile slot.
  When the query carries intent words, only a title that is exactly the whole query anchors (the item
  `Guild Tabard` for `guild tabard`: the words are its name), so `character us malganis Aurow` still
  resolves to the character and not to a spell of the same name.
- structured profile queries (`guild us illidan Liquid`, `character us malganis Aurow`) keep their
  profile intent boosts and put Raider.IO first.

**Order — a provider's own order is its ranking.** The wrapper never reorders two rows from the same
provider. It interleaves the providers' lists (`interleave_provider_rows`): at every step the best of
the providers' next rows, by the anchor tier, the off-intent tier and then the normalized wrapper
score (`search_result_sort_key`, ties then by provider name, never by raw provider score), takes
the next place. Boosts therefore decide only *between*
providers; within one provider, a row the provider ranked lower stays lower.

**Diversity — no provider fills the page.** After interleaving, the page is built with a per-provider cap
of half the page rounded up. An on-intent row over the cap is *deferred*, not dropped: it fills the
slots the other on-intent providers leave, so a page is never short while on-intent candidates
exist. Off-intent rows then fill what is still empty, up to a strict minority of the page
(`limit // 2`, at least one); the page comes back short rather than repeating twenty near-identical
profiles. The chosen rows keep their interleaved order, so a promoted row sits where interleaving put
it and `data.results` never reorders a provider's rows. `data.merge_policy` reports the caps, the
reserved slot, the candidate total, and how many rows were deferred or withheld
(`docs/foundation/SAFE_ANALYTICS_RULES.md`: a page that dropped rows says so).

**Quality — the row's own title.** A row whose title *is* the query (`name_match: "exact"`) or whose
title starts with it (`"title_prefix"`, as in `Thunderfury, Blessed Blade of the Windseeker`) scores
above one that merely mentions it somewhere, so between providers an exact item/spell/quest beats a
partial match and a news post about it. The boosts live in the same ranking policy as every other
weight. A guide row the provider itself flagged as superseded (Wowhead's `stale_guide` ranking
reason) carries `wrapper_ranking.stale_guide: true`, in the `--brief` rows too; the wrapper passes the
provider's flag on and never computes one of its own.

Changing any of these rules is a contract change: the model is covered by a table of realistic
queries with realistic per-provider score scales in `tests/test_provider_contract.py`, and that
table — not a single number — is what a change has to keep true.

The policy is `RANKING_POLICY` in `warcraft_cli/provider_contract.py`. There is no local override
file, so the same query always ranks the same way.

## Resolve Rules

`warcraft resolve` should:
- ask ready providers for candidate resolutions
- rank them conservatively
- preserve source provenance
- avoid pretending certainty when providers are stubbed or unavailable
- not upgrade synthetic direct-route hints into verified resolved matches unless the provider actually returned a resolved payload

Providers whose wrapper `resolve` surface is stubbed or otherwise not ready should be excluded from wrapper fanout and surfaced in exclusion metadata instead of being queried like live routing candidates.

Resolve selection rules:
- `warcraft resolve` and `warcraft search` agree: each provider's match is ranked as search ranks that
  provider's top row (normalized against the provider's own candidates, anchor and off-intent tiers,
  intent boosts), and the top-ranked match is the only candidate for the answer, except that a match
  its own provider rated `low` is skipped: a tie a provider could not break never blocks another
  provider's answer, while a `medium` match (something found but not confirmed) still does
- provider-reported `resolved` and confidence never lift a match over a better-ranked one; they only
  break an exact tie on the wrapper score, ahead of the provider name; the raw provider score, which
  is not comparable across providers, never breaks a tie
- the top-ranked match is the answer only when its own provider resolved it (every provider resolves
  only at `high` confidence; otherwise `unresolved_reason: "provider_did_not_resolve"`, or
  `"single_word_query_not_named_exactly"` when the provider's `confidence_cap.rule` is
  `single_word_query`; the wrapper reads that rule and never recomputes it. A capped row still ranks
where its score puts it, so it can sit above another provider's resolved answer and leave the wrapper
unresolved) and the query's intents
  do not rank that provider's family down (`wrapper_ranking.intent_family_fit` is not negative):
  a guide query is never answered by Lorrgs spec metadata, a guild query never by a wiki article.
  A match whose title is exactly the query is exempt, because the intent word is part of its name
  (the item `Guild Tabard`)
- the wrapper never passes its own `--limit` to a provider's resolve: providers judge confidence
  against their rivals, and a small limit would hide them
- preserve the chosen provider's `match`, `next_command`, and confidence instead of flattening them
- when there is no answer, surface the top-ranked match as `best_unresolved_candidate` (flagged
  `resolved: false`, with `unresolved_reason`), together with the own `fallback_search_command`s of
  the providers that returned a candidate, in ranking order. A `low` match keeps its rank in these
  hints, though it can never be the answer: a provider's `low` often means two right pages tied, and
  moving it behind an off-intent `medium` match made the hint point away from the query. A provider
  that found nothing hands over no search, so when no provider found
  anything `fallback_search_command` is `null`. Every lower match its provider resolved is listed in
  `provider_resolved_candidates` with its `next_command`: a resolved answer has no fallback search,
  so without that list an answer a better-ranked `medium` match blocked would be unreachable

Debuggability rules:
- `warcraft search --ranking-debug` should expose compact ranking summaries for the top wrapper candidates
- `warcraft resolve --ranking-debug` should expose the first `--limit` providers' matches in ranking order, each with its provider's `resolved` flag
- `warcraft search --brief` and `warcraft resolve --brief` should omit bulky provider payloads while keeping the wrapper decision surface intact (`--compact` is the global string-truncation flag and nothing else)

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
- `curseforge` is a scaffold for the public CurseForge addon API (`x-api-key` auth, `CURSEFORGE_API_KEY`): `doctor` and `addon` are ready (`curseforge addon <slug|id>` returns the addon metadata, latest files, and the newest file's changelog), while `search`/`resolve` stay `coming_soon`. Registered with `expansion_mode=none` (addon game-version compatibility lives inside file records, not the wrapper's expansion axis). Host/endpoints/response shapes follow the documented public CurseForge Core API and are confirmed against live traffic (`provenance.verified=true`)
- `lorrgs` is ready for no-auth public Lorrgs reads: static spec/boss/spell metadata, top-parse spec rankings, composition rankings, report overview handoffs, and conservative `search`/`resolve` for Lorrgs URLs, Warcraft Logs report URLs, bare report codes, and spec/boss text. Registered with `expansion_mode=fixed` / `supported_expansions=["retail"]`

## Documentation Rule

Whenever provider capabilities, provider readiness rules, or wrapper/provider boundaries change:
- update this document
- update [Roadmap](../ROADMAP.md) if sequencing changes
- update [Repo Structure And Packaging](../architecture/REPO_STRUCTURE_AND_PACKAGING.md) if package or language rules change
