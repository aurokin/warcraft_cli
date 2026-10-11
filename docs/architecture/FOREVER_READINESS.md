# World of Warcraft: Forever readiness

Evidence checked 2026-10-10/11. This records verified integration scope, not a promise that
every Forever workflow works. [Blizzard lists launch on November 4, 2026 and beta ending
October 21](https://worldofwarcraft.blizzard.com/en-us/forever/).

## Current boundaries

| Surface | Scope |
| --- | --- |
| Wowhead | Dedicated `/forever/` profile, data environment 16; source URLs and caches stay separate from Classic/retail |
| Wrapper discovery | `--expansion forever` queries Wowhead only; other providers disclose exclusions |
| Talent calculators | Forever's versioned `v2` talent data can be decoded; this is separate from retail loadout transport |
| Character/profile handoff | Unsupported: current join depends on region + realm + name, and Raider.IO is retail-only |
| Warcraft Logs | No verified Forever host/schema/identity/auth contract; no Classic/Fresh alias |
| Blizzard API | No verified Forever namespace/realmless character lookup; no guessed namespace |
| Simulation | Official engine branch exists but is experimental; retail build transport and guide handoffs remain unsupported |
| CurseForge | Standalone metadata reads remain available; no automatic selection of Forever-compatible files |

The shared registry retains the existing public `--expansion` flag for game profiles. It does
not introduce a parallel registry or infer capabilities from the fact that Forever resembles
Classic. Providers with `expansion_mode=none` normally allow advisory-only passthrough, but
explicitly unverified Forever requests fail before they run. Doctor reports the reason.

## Realmless identity

Blizzard confirms [two-part character names unique within a
region](https://news.blizzard.com/en-us/article/24304161/create-a-name-of-your-own-in-wow-forever).
Players can hide the second name in display settings. A displayed first name cannot establish
character identity. Preserve full source names and provider IDs; never synthesize a realm or
collapse a full name into its first component to satisfy the existing profile join.

Enable profile handoffs only after verifying stable upstream character identifiers, region and
ruleset fields, full-name handling, and lookup behavior. Game rulesets describe play context;
do not assume they are part of a character's identity without the provider contract.

## Simulation engine evidence

The official [SimulationCraft Forever
branch](https://github.com/simulationcraft/simc/tree/forever) was built in an isolated scratch
checkout at `fcfe72f62bba554dcb7b5b6c7804a6cd799ec71f`. Its banner identifies client
`1.60.1.70334` and hotfix `2026-10-10/70334`, while the engine version is still `1210-01`.
Use client data version for game identity; the engine version alone can look like retail.
`simc version` and `doctor` disclose `wow_version` and `game_variant`; a positively identified
Forever binary disables retail build/analysis workflows. Raw `run`/`sim` remain experimental.

Verified checks at that revision:

- `spell_query=spell.id=133` succeeds and returns Forever Fireball data and talent modifiers.
- A synthetic level-10 shaman with explicit `actions=earth_shock`, no optimal raid buffs,
  ten iterations and ten seconds runs and emits the existing JSON report shape.
- Default unequipped shaman simulation and an explicit warlock Shadow Bolt probe abort
  upstream assertions. Several class modules are scaffolds, and no stock simulation profiles
  are present. These checks do not establish combat accuracy or live-character import support.

Keep a Forever checkout separate from the configured retail checkout. Source inspection and
an engine execution check do not make the current retail talent/hero-tree analysis compatible.
The experimental [addon
release](https://github.com/simulationcraft/simc-addon/releases/tag/12.1.0-alpha-06) also does not
establish engine coverage.

## Alternative engine evaluation

[ElliotWood/Forever](https://github.com/ElliotWood/Forever) exposes a headless WoWSims-based
CLI. `wowsimcli sim --infile` reads `RaidSimRequest` protojson and emits `RaidSimResult`.
The UI's CLI exporter produces that request. Addon exports, settings JSON, and decoded share
links have different shapes and cannot be passed through as requests.

The CLI discards unknown fields, its input schema is moving, and old documentation mentions
a `ruleset` field absent from the checked schema. A future adapter must pin engine/schema,
accept validated CLI exports, disclose defaults and unsupported fields, and verify a real
exported character fixture. A new provider is deferred while the existing official engine
has a plausible path to coverage.

## Verification before promotion

- Recheck captured source data against the launch client, preserving versioned source URLs.
- Prove same-ID items/spells and cached results do not cross game profiles.
- Verify all nine talent calculators against rendered ranks, names, spell IDs, and tree totals.
- Test full names sharing a first component and names containing non-English characters.
- Verify an upstream character/profile contract before enabling joins.
- Verify a complete exported character → matching engine → report journey before advertising
  simulation readiness; separate execution correctness from combat-model accuracy.
