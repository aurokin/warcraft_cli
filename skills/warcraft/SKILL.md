---
name: warcraft
description: Use the local `warcraft` CLI as the root Warcraft data entrypoint when the source is unclear or the user may need more than one provider. Best for wrapper `search`, `resolve`, `doctor`, expansion-aware routing, and handing off to the right provider CLI.
---

# Warcraft

Use `warcraft` first when the caller does not already know which provider they need.

## Start Here

- Source unclear:
  - `warcraft resolve "<query>"`
  - if unresolved: `warcraft search "<query>"`
- Source known:
  - `warcraft <provider> ...`
- Version-specific request:
  - `warcraft --expansion <profile> ...`
- Global flags:
  - put them before the subcommand, e.g. `warcraft --pretty search "<query>"`
- Trust check:
  - `warcraft doctor`
- Cross-provider guide evidence:
  - `warcraft talent-packet <source>`
  - `warcraft talent-describe <source> --apl-path <apl>`
  - `warcraft cooldown-packet <warcraftlogs-report-url> --actor-id <source-id> --phase <n>`
  - `warcraft guide-compare <bundle-a> <bundle-b>`
  - `warcraft guide-compare-query "<guide query>"`
  - `warcraft guide-compare-query "<guide query>" --simc-build-handoff --simc-apl-path <apl>`
  - `warcraft guide-builds-simc <bundle-or-orchestration-root>`
  - `warcraft guide-builds-simc <bundle-or-orchestration-root> --apl-path <apl>`

## Provider Synopsis

| Provider | Best for | First commands |
| --- | --- | --- |
| `wowhead` | entities, guides, comments, timelines, tool-state refs | `warcraft wowhead search ...`, `warcraft wowhead entity ...`, `warcraft wowhead guide ...` |
| `method` | supported article/guide families with simple article structure | `warcraft method search ...`, `warcraft method guide ...` |
| `icy-veins` | spec guides, hubs, and guide subpages | `warcraft icy-veins search ...`, `warcraft icy-veins guide ...` |
| `raiderio` | character/guild profiles, Mythic+, sampled run analytics | `warcraft raiderio character ...`, `warcraft raiderio sample ...` |
| `warcraft-wiki` | API docs, events, systems, lore, reference pages | `warcraft warcraft-wiki api ...`, `warcraft warcraft-wiki article ...` |
| `wowprogress` | progression, rankings, guild/profile analytics | `warcraft guild ...`, `warcraft wowprogress guild ...`, `warcraft wowprogress sample ...` |
| `warcraftlogs` | official raid-log API, world metadata, guild/character/report lookups | `warcraftlogs doctor`, `warcraftlogs guild ...`, `warcraftlogs report-fights ...` |
| `simc` | local SimulationCraft inspection, exact-build priority analysis, APL comparison, and runs | `warcraft simc doctor`, `warcraft simc priority ...`, `warcraft simc compare-apls ...` |
| `raidbots` | reading shared Raidbots reports and bridging their SimC input to local `simc` | `warcraft raidbots inspect-report <url-or-id>`, `warcraft raidbots input <url-or-id>`, `warcraft raidbots explain-input` |
| `blizzard` | official Battle.net Game Data (realm, item) and Profile (character) reads | `warcraft blizzard doctor`, `warcraft blizzard realm ...`, `warcraft blizzard character ...` |
| `curseforge` | World of Warcraft addon lookup: metadata, latest files, changelog | `warcraft curseforge doctor`, `warcraft curseforge addon <slug-or-id>` |
| `lorrgs` | top-parse cooldown timelines, composition rankings, report overview handoffs, and static spec/boss/spell metadata | `warcraft lorrgs resolve ...`, `warcraft lorrgs spec-ranking ...` |

## Routing Rules

- Prefer `resolve` when you want one conservative next command.
- Prefer `search` when you want to inspect candidates across providers.
- Prefer `warcraft guild ...` when the user wants a guild snapshot and you want normalized input plus explicit source disagreement reporting.
- Preserve provider provenance. `warcraft` is a router, not a source.
- Use `warcraft guide-compare` when you already have exported guide bundles and want additive cross-provider evidence instead of a synthesized summary.
- Use `warcraft guide-compare-query` when you want the wrapper to resolve, export, and compare guide candidates conservatively across supported guide providers.
- `guide-compare-query` may use a provider search fallback only when the top guide result is clearly decisive; it should not guess across weak or ambiguous guide candidates.
- `guide-compare-query` should reuse prior orchestrated bundles only through explicit freshness rules like `--max-age-hours` and `--force-refresh`, not through invisible cache-like behavior.
- Use `warcraft talent-packet` when the source is already an explicit build ref, scoped log actor, or packet file and you want the wrapper to route it into the shared transport contract.
- Use `warcraft talent-describe` when you want that same routed packet handed directly into `simc describe-build` without manually chaining commands.
- Use `warcraft cooldown-packet` for player-specific log questions like "how can I improve my
  cooldowns in P2"; it joins Lorrgs phase/spell/top-parse context with exact Warcraft Logs cast
  events for the selected actor and keeps both sources visible.
- Typical packet flow:
  - `warcraftlogs report-player-talents <report> --fight-id <id> --actor-id <id> --out ./tmp/actor-packet.json`
  - `simc validate-talent-transport --build-packet ./tmp/actor-packet.json --out ./tmp/actor-packet-validated.json`
  - `warcraft talent-describe ./tmp/actor-packet-validated.json --apl-path <apl>`
- Failure contract:
  - producer commands fail with `invalid_transport_packet` if they would otherwise emit malformed packet JSON
  - malformed Wowhead-like talent refs, including exact packet refs without a build code, fail with `invalid_tool_ref`
  - `simc ... --build-packet <path>` fails with `invalid_build_packet` when the packet file is malformed
  - wrapper routing preserves provider `invalid_transport_packet` failures instead of replacing them with a generic wrapper error
- Add `--simc-build-handoff` when you want the orchestration packet to include explicit guide build refs handed into `simc`; add `--simc-apl-path` when you also want exact-build `describe-build` output.
- Use `warcraft guide-builds-simc` when you want explicit guide build refs handed into `simc` without inferring claims from guide prose; the handoff packet now includes provenance, citations, and source freshness so agents can tell how trustworthy the build inputs are.
- Add `--apl-path` when you want the wrapper to include exact-build `simc describe-build` output for those same explicit guide build refs.
- When `--expansion` matters, trust only the providers the wrapper says are included.
- Once the provider is known, switch to the provider CLI or the provider reference below.

## Read Next

- `wowhead`: see `references/wowhead.md`
- `method`: see `references/method.md`
- `icy-veins`: see `references/icy-veins.md`
- `raiderio`: see `references/raiderio.md`
- `warcraft-wiki`: see `references/warcraft-wiki.md`
- `wowprogress`: see `references/wowprogress.md`
- `warcraftlogs`: see `references/warcraftlogs.md`
- `simc`: see `references/simc.md`
- `raidbots`: see `references/raidbots.md`
- `blizzard`: see `references/blizzard-api.md`
- `curseforge`: see `references/curseforge.md`
- `lorrgs`: see `references/lorrgs.md`

## Notes

- This skill is the umbrella consumer skill.
- Keep provider details in the reference files so they can become standalone skills later without rewriting the root skill.
- Keep reference links portable: use relative paths like `references/simc.md`, not machine-specific absolute paths.
