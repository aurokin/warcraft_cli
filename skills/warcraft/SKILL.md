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
- Global flags (every binary, always before the subcommand):
  - `--pretty` pretty-print JSON; default output is compact JSON
  - `--compact` truncate long strings, with `--compact-max-chars <n>` to set the cut
  - `--fields <a.b,c>` keep only these dot paths; repeatable
  - `--fields-strict` fail instead of silently dropping a missing `--fields` path
  - `--profile agent|human|debug` presets (`agent` is the default compact JSON)
  - `warcraft --expansion <profile>` additionally restricts routing to expansion-aware providers
  - example: `warcraft --pretty --fields data.results search "<query>"`
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

## Output Contract

Every binary emits one JSON object. On success it carries `ok: true`, `provider`, `command`,
`kind`, `schema_version`, `query`, `provenance`, and `data`. Read the payload from `data`: every
binary populates it. Providers also copy the same keys to the top level for older agents; those
copies are deprecated and identical to `data`.

On failure the object goes to stderr with `ok: false` and
`error: {"code": ..., "message": ..., "details"?: ...}`, and the process exit code tells you what
to do next:

| Exit | Meaning | Reaction |
| --- | --- | --- |
| `0` | success | continue |
| `1` | generic failure | read `error.code`; do not retry blindly |
| `2` | usage error (bad flag or argument) | fix the command |
| `3` | auth required or rejected | run the provider's `doctor` / `auth status` |
| `4` | target not found | try a different id or `resolve` again |
| `5` | network or upstream failure | back off and retry once |

A traceback is always a bug — report it rather than parsing it.

## Provider Synopsis

Tiers are the support level to expect: **core** is deeply covered, **supported** is stable but
narrower, **experimental** is thin and may change.

| Provider | Tier | Best for | First commands |
| --- | --- | --- | --- |
| `wowhead` | core | entities, guides, comments, timelines, tool-state refs | `warcraft wowhead search ...`, `warcraft wowhead entity ...`, `warcraft wowhead guide ...` |
| `method` | supported | supported article/guide families with simple article structure | `warcraft method search ...`, `warcraft method guide ...` |
| `icy-veins` | supported | spec guides, hubs, and guide subpages | `warcraft icy-veins search ...`, `warcraft icy-veins guide ...` |
| `raiderio` | supported | character/guild profiles, Mythic+, sampled run analytics | `warcraft raiderio character ...`, `warcraft raiderio sample ...` |
| `warcraft-wiki` | supported | API docs, events, systems, lore, reference pages | `warcraft warcraft-wiki api ...`, `warcraft warcraft-wiki article ...` |
| `wowprogress` | supported | progression, rankings, guild/profile analytics | `warcraft guild ...`, `warcraft wowprogress guild ...`, `warcraft wowprogress sample ...` |
| `warcraftlogs` | core | official raid-log API, world metadata, guild/character/report lookups | `warcraftlogs doctor`, `warcraftlogs guild ...`, `warcraftlogs report-fights ...` |
| `simc` | core | local SimulationCraft inspection, exact-build priority analysis, APL comparison, and runs | `warcraft simc doctor`, `warcraft simc priority ...`, `warcraft simc compare-apls ...` |
| `raidbots` | experimental | reading shared Raidbots reports and bridging their SimC input to local `simc` | `warcraft raidbots inspect-report <url-or-id>`, `warcraft raidbots input <url-or-id>`, `warcraft raidbots explain-input` |
| `blizzard` | experimental; verified live for us/eu/kr/tw | official Battle.net Game Data (realm, item) and Profile (character) reads | `warcraft blizzard doctor`, `warcraft blizzard realm ...`, `warcraft blizzard character ...` |
| `curseforge` | experimental; verified live | World of Warcraft addon lookup: metadata, latest files, changelog | `warcraft curseforge doctor`, `warcraft curseforge addon <slug-or-id>` |
| `lorrgs` | experimental | top-parse cooldown timelines, composition rankings, report overview handoffs, and static spec/boss/spell metadata | `warcraft lorrgs resolve ...`, `warcraft lorrgs spec-ranking ...` |

## Routing Rules

- Prefer `resolve` when you want one conservative next command.
- Prefer `search` when you want to inspect candidates across providers.
- Prefer `warcraft guild ...` when the user wants a guild snapshot and you want normalized input plus explicit source disagreement reporting.
- Preserve provider provenance. `warcraft` is a router, not a source.
- Use `warcraft guide-compare` when you already have exported guide bundles and want additive cross-provider evidence instead of a synthesized summary.
- Use `warcraft guide-compare-query` when you want the wrapper to resolve, export, and compare guide candidates conservatively across supported guide providers.
- `guide-compare-query` may use a provider search fallback only when the top guide result is clearly decisive; it should not guess across weak or ambiguous guide candidates.
- `guide-compare-query` should reuse prior orchestrated bundles only through explicit freshness rules like `--max-age-hours` and `--force-refresh`, not through invisible cache-like behavior.
- Steer `guide-compare-query` orchestration with:
  - `--provider <name>` repeatable, to restrict the run to `wowhead`, `method`, or `icy-veins`
  - `--out-root <dir>` to choose where the orchestrated bundles are written
  - `--limit <n>` (1-20, default 5) provider-local resolve candidates considered before one guide is selected
  - `--max-age-hours <n>` (1-720, default 24) and `--force-refresh` for bundle reuse
  - `--simc-build-handoff` plus `--simc-apl-path <apl>`, `--simc-decode` / `--no-simc-decode`, and `--simc-build-limit <n>` (1-200, default 20) for the SimC handoff
  - example: `warcraft guide-compare-query "<guide query>" --provider wowhead --provider icy-veins --limit 3 --out-root ./tmp/guide-compare`
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
