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
- talent transport packets that preserve raw build evidence plus any exact or validated transport forms

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
| wowprogress | ✓ (`character`) | ✓ (guild encounter rows) | – | – | – |
| warcraft-wiki | – | – | – | – | – |
| blizzard-api | – | – | – | – | – |

Notes:
- `raiderio` class/spec identity is always `normalized` (never `canonical`); `confidence` is `high`
  only when both class and spec resolve, else `none` (search rows are class-only).
- `raidbots` class/spec comes from the sim report's explicit `class`/`specialization` fields →
  `high` confidence when both present.
- `wowprogress` class/spec is `normalized` with `confidence: none` — class is parsed from free header
  text and the spec (when present) is sourced from the SimDPS table; encounter identity is name-only
  (`encounter_id: null`).
- `method`/`icy-veins` ability identity is `canonical` (the Wowhead spell id is already in the link);
  it is emitted only on `spell` linked-entity rows, leaving other entity types unchanged.
- `blizzard-api` emits no identity yet. The Game Data/Profile endpoints shipped (AUR-455), but
  wiring Blizzard-sourced class/spec, encounter (journal), and ability (spell-id) identity onto them
  is deferred follow-up work, not yet scheduled.

## Demonstrated Handoffs

End-to-end cross-provider handoffs that ship today, where one provider's output feeds another's
input or lookup:

- **Guide → SimC build:** `warcraft guide-builds-simc` (and `guide-compare --simc-build-handoff`)
  extracts build references from content-provider guides and produces talent transport packets that
  `simc` consumes.
- **Log actor → profile:** `warcraft actor-profile <report-code> <actor-name>` resolves a Warcraft
  Logs report actor (`report_player_details`) and cross-walks it to a Raider.IO `character` profile,
  emitting both `class_spec_identity` blocks side by side with an agree/conflict reconciliation. The
  join is a soft match on region + realm + character name — explicitly **not** a canonical
  cross-provider actor id (see Report Actor and Current Scope).

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
