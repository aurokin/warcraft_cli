# Identity Contract

This document defines the shared identity contract for cross-provider normalization.

The goal is not to create one fake universal WoW entity graph.
The goal is to give agents a small, honest shared layer they can trust across CLIs.

## Principles

- Preserve provider-native ids.
- Normalize formatting separately from canonical identity.
- Treat normalization as an additive analysis layer, not a replacement for the source payload.
- Mark inference explicitly.
- Treat ambiguity and unknown state as first-class outcomes.
- Do not collapse multiple candidates into one identity without source-backed evidence.

## Additive Layer Rule

Normalization should add reusable structure for agents without reducing access to source detail.

Required behavior:
- keep raw source content, source ids, and source citations available
- add normalized fields alongside raw payloads instead of overwriting them
- let agents choose raw, normalized, or both depending on the workflow
- preserve section, page, report, fight, and window provenance from normalized rows back to the source content

Disallowed behavior:
- replacing guide, article, or log content with only normalized summaries
- treating normalized fields as a complete substitute for the source
- dropping source detail just because a normalized view exists

## Status Meanings

- `unknown`: no shared identity could be established safely.
- `normalized`: formatting or token cleanup only; not a claim of cross-provider equivalence.
- `canonical`: backed by a stable id inside the domain.
- `inferred`: a best supported mapping, but still not canonical across all providers.
- `ambiguous`: multiple plausible candidates remain.

## Confidence Meanings

- `none`: no useful identity confidence.
- `low`: weak inference or multiple plausible matches.
- `medium`: useful but not fully stable.
- `high`: strong source-backed match inside the declared domain.

Confidence is not a substitute for status.
A payload can have `high` confidence and still be only `inferred`.

## Domain Rules

### Class / Spec

Safe shared contract:
- normalized `actor_class`
- normalized `spec`

`canonical` is only valid when the source gives an explicit class/spec pair in a stable domain.
For many workflows, especially build identification, class/spec should remain `inferred` instead of `canonical`.

The retail class/spec table lives in
[packages/warcraft-core/src/warcraft_core/wow_specs.py](../../packages/warcraft-core/src/warcraft_core/wow_specs.py):
`WOW_SPECS` (class key, spec key, Blizzard spec id) and `WOW_CLASS_NAMES`. Keys are the normalized
values identity payloads emit (`deathknight`, `beast_mastery`). Each spec renders the slug a provider
takes (`lorrgs_slug` `hunter-beastmastery`, `raiderio_slug` `hunter-beast-mastery`,
`warcraftlogs_spec_slug` `BeastMastery`), and `lookup_spec(text, class_hint=None)` reads any of those
spellings back, plus guide-site order (`beast-mastery-hunter`), display names (`Beast Mastery Hunter`)
and the shorthand in `CLASS_SPEC_ALIASES` (`bm hunter`, `bdk`). A bare spec several classes share
(`frost`, `holy`, `protection`, `restoration`) names no spec unless `class_hint` picks one; no two specs
share any other spelling. Provider flags that take a spec (Lorrgs spec routes, Raider.IO
`--contains-spec`/`--contains-class`, Warcraft Logs `--class-name`/`--spec-name` and its sampled spec
filter, SimC `--actor-class`/`--spec` and `find-action`/`trace-action --class`,
`warcraft cooldown-packet --spec-slug`) translate through this lookup and leave text it does
not recognize as typed. The table is retail only; classic sites keep their
own permissive handling.

### Encounter

Safe shared contract:
- `encounter_id` when a provider exposes one
- `journal_id` when a provider exposes one
- normalized encounter name

`canonical` is valid when the provider exposes a stable encounter id or journal id.
Name-only encounter identity is only `normalized`.

### Ability

Safe shared contract:
- `spell_id` or equivalent stable game id when explicitly present
- normalized ability name

Name-only ability identity is only `normalized`.
Do not infer a spell id from text alone in the shared layer.

### Report Actor

Safe shared contract:
- actor identity scoped to one report and one fight
- actor id plus report/fight scope
- optional normalized class/spec/name

Report actor identity is only canonical inside its declared local scope.
Do not treat report actors as globally canonical across multiple reports by default.

### Build

There is no repo-wide canonical build id.

Safe shared contract:
- inferred class/spec target
- candidate class/spec rows
- source kind
- source notes
- explicit build-reference packets when a provider page embeds a concrete Wowhead talent-calc URL
  whose path names a class and one of that class's specs (a classic-era
  `/classic/talent-calc/<class>/<code>` path names no spec and is not read as one)
- talent transport packets that preserve raw build evidence plus any exact or validated transport forms

Wowhead talent calculator refs have one parser, `parse_wowhead_talent_calc` in
`warcraft_core.identity`. It reads URLs, `/talent-calc/...` paths and `<class>/<spec>/<code>`
shorthand for every calculator (retail, PTR and beta; `classic`, `classic-ptr`, `tbc`, `wotlk`,
`cata`, `mop-classic`; and WoW Forever's `forever`) and returns either the calculator, class, spec
(null on a classic `<class>/<code>` path), build code and trailing segment (a classic selection
order or MoP Classic glyphs), or the reason the ref is not one, plus whether the ref still aims at a
calculator so a router can hand it to Wowhead for that message. Wowhead's `talent-calc` adds only
its build-code checks on top, and the wrapper's talent routing uses it as is. Build references and
SimC read the narrower `parse_wowhead_talent_calc_ref`: a path that names `/talent-calc`, a class
and one of that class's retail specs, on retail or an expansion site, with no trailing segment.
It preserves normalized spec aliases accepted by build references, such as `Balance`,
`BeastMastery` and `beast_mastery`; the Wowhead tool parser keeps its lowercase slug grammar.

Build identity should usually be `inferred`, `ambiguous`, or `unknown`.
Do not emit a canonical build id unless a future source contract proves one exists.
Do not create build references from guide slugs, page titles, or other indirect hints alone.
Do not mark a reconstructed transport form as reliable unless the consumer contract validated it explicitly.

## Shared Code Ownership

Shared identity helpers live in [packages/warcraft-core/src/warcraft_core/identity.py](../../packages/warcraft-core/src/warcraft_core/identity.py).

Provider rules:
- provider CLIs may adapt their payloads into this contract
- provider CLIs must not invent separate status meanings
- provider-specific ids and raw fields should remain in the provider payload alongside shared identity fields
- provider CLIs should expose normalized outputs as additive analysis fields rather than replacing raw source structures

## Maintenance Rules

- Add new shared identity behavior in shared code first, not directly in a provider CLI.
- If semantics change, update this document and tests in the same change.
- If a provider cannot satisfy the shared contract honestly, return `unknown` or `ambiguous`.
- New provider adapters should prove their mapping through contract tests, not only fixture snapshots.

## Testing Requirements

At minimum:
- unit tests for shared normalization helpers
- unit tests for status selection and ambiguity handling
- provider tests for any payload that embeds the shared identity contract

## Provider Coverage

Which providers emit which shared identity payloads today (✓ = emitted; – = not emitted). This is
the auditable record of "documented contracts per identity type"; keep it current when a provider
starts or stops emitting a contract.

| Provider | Class/Spec | Encounter | Ability | Report-Actor | Build |
| --- | --- | --- | --- | --- | --- |
| warcraftlogs | ✓ | ✓ | ✓ | ✓ | – |
| raiderio | ✓ (`character`, rankings, search, guild roster) | – | – | – | – |
| wowhead | – | – | – | – | ✓ (talent-calc) |
| simc | – | – | – | – | ✓ |
| method | – | – | ✓ (embedded spell refs) | – | ✓ (embedded talent-calc refs) |
| icy-veins | – | – | ✓ (embedded spell refs) | – | ✓ (embedded talent-calc refs) |
| raidbots | ✓ (`inspect-report` actors) | – | – | – | – |
| warcraft-wiki | – | – | – | – | – |
| blizzard-api | – | – | – | – | – |

Notes:
- `raiderio` class/spec identity is always `normalized` (never `canonical`); `confidence` is `high`
  only when both class and spec resolve, else `none` (search rows are class-only).
- `raidbots` class/spec comes from the sim report's explicit `class`/`specialization` fields →
  `high` confidence when both present.
- `method`/`icy-veins` ability identity is `canonical` (the Wowhead spell id is already in the link);
  it is emitted only on `spell` linked-entity rows, leaving other entity types unchanged.
- `blizzard-api` emits no identity yet. The Game Data/Profile endpoints shipped (AUR-455), but
  wiring Blizzard-sourced class/spec, encounter (journal), and ability (spell-id) identity onto them
  is deferred follow-up work, not yet scheduled.

## Demonstrated Handoffs

End-to-end cross-provider handoffs that ship today, where one provider's output feeds another's
input or lookup:

- **Guide → SimC build:** `warcraft guide-builds-simc` (and `guide-compare-query --simc-build-handoff`)
  extracts build references from content-provider guides and produces talent transport packets that
  `simc` consumes.
- **Log actor → profile:** `warcraft actor-profile <report-code> <actor-name>` resolves a Warcraft
  Logs report actor (`report_player_details`) and cross-walks it to a Raider.IO `character` profile,
  emitting both `class_spec_identity` blocks side by side with an agree/conflict reconciliation. The
  join is a soft match on region + realm + character name — explicitly **not** a canonical
  cross-provider actor id (see Report Actor and Current Scope). Raider.IO's spec is the character's
  current active spec, not the one played in the log, so a `spec_mismatch` alone is expected for an
  off-spec log; only a `class_mismatch` casts doubt on the join.

## Current Scope

The current shared module intentionally covers only:
- class/spec identity
- encounter identity
- ability identity
- report-actor identity
- build identity packets
- explicit Wowhead talent-calc build-reference parsing
- talent transport packet shaping

It does not yet attempt:
- universal cross-provider build equivalence
- universal actor identity across logs, guides, profiles, and local tools
- automatic spell-id inference from free text
