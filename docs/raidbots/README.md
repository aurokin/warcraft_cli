# Raidbots CLI

## Commands (shipped)

Report consumption (Tier 1) and local SimC bridging (Tier 2) are implemented. Submission (Tier 3) is deferred.
Run via `raidbots …` directly or `warcraft raidbots …` through the wrapper. `<url-or-id>` accepts a bare report
ID or any URL containing `/report/{ID}`.

| Command | Purpose |
|---|---|
| `raidbots doctor` | Status, capabilities, cache config, and the live URL templates. |
| `raidbots inspect-report <url-or-id>` | Fetch + parse a report's `data.json` into a kind-aware summary (quick-sim actor + metrics, or ranked profilesets for Top Gear/Droptimizer) with freshness/citations/scope. Pass `--no-raw` to omit the raw payload (recommended for large multi-profile reports). |
| `raidbots input <url-or-id>` | Fetch the report's SimC input and emit it plus a handoff: classification and suggested local `simc` commands. |
| `raidbots explain-input` | Classify SimC addon/profile text locally (`--text`, `--file`, or stdin) and explain what Raidbots would do with it. No network. |

Errors map to a shared envelope: `invalid_report`, `not_found`, `rate_limited`, `invalid_query`, `upstream_error`.

Report URLs are env-overridable so a live URL change needs no code change: `RAIDBOTS_BASE_URL`,
`RAIDBOTS_REPORT_PATH_TEMPLATE`, `RAIDBOTS_DATA_PATH_TEMPLATE`, `RAIDBOTS_INPUT_PATH_TEMPLATE` (each `{id}`-templated).
Cache TTL: `RAIDBOTS_REPORT_CACHE_TTL_SECONDS` (default 24h; completed reports are immutable).

**Handoff posture:** raidbots never imports `simc` and never runs SimC. To analyze locally, run the suggested
`simc sim -` / `simc decode-build` / `simc describe-build` commands; to run on the Raidbots cloud, paste the
emitted input into raidbots.com.

## Why Raidbots Should Be Staged Carefully

`raidbots` is popular and useful, but it should not be planned like a normal public data API.

Raidbots is a cloud frontend for SimulationCraft. Its core value is running SimC on powerful cloud hardware so users don't need a local build. The strongest confirmed workflow is built around SimulationCraft input and simulation results, so this CLI should be introduced after `simc`.

## Research Summary

Observed from official support content:
- Raidbots recommends the SimulationCraft addon and `/simc` workflow
- the official support docs state the Blizzard Armory API is often out of date
- official support explicitly says Raidbots uses SimulationCraft under the hood
- spec support is constrained by SimulationCraft volunteer maintenance, not by Raidbots itself
- healing and tanking specs are unsupported or unreliable because SimC focuses on damage dealing

Observed from the Raidbots architecture (Seriallos blog posts):
- the frontend is a React SPA that generates SimC input from user selections
- input is submitted to a web API server (`btserverweb.raidbots.com`)
- jobs go into a queue, workers pick them up and run SimC
- on completion, HTML, JSON (`data.json`), and SimC stdout/stderr go to Google Cloud Storage
- large sims are split into chunks by Flightmaster (a ~900 LoC NodeJS orchestrator) and merged on completion
- reports are served at `raidbots.com/simbot/report/{ID}`

Observed from the developer and community surface:
- there is no documented public API for submitting simulations
- the `/developers` page exposes static game data and "hooks" but is JS-rendered and not fully indexed
- the GitHub issues repo (`seriallos/raidbots-issues`) was archived March 2025
- the only third-party API wrapper (`logiek/raidbots-api`, PHP) is discontinued and never supported submission
- the Discord bot accepts sim commands with flags (fight style, fight length, enemy count, scaling, talent comparison) but is Raidbots' own internal integration
- the Terms of Use page is JS-rendered and could not be read externally

## Simulation Types

| Tool | Purpose |
|---|---|
| Quick Sim | Single-profile DPS estimate with detailed stats |
| Top Gear | Compare equipped/bag gear combinations to find the best setup |
| Droptimizer | Simulate potential drops from raids/dungeons to find the most valuable content to run |
| Stat Weights | Calculate relative stat values |
| Advanced Sim | Run arbitrary raw SimC input |

All of these generate SimC input under the hood. The website is a UI for building that input.

## Report Structure

Reports are public and stable:
- report page: `raidbots.com/simbot/report/{ID}`
- raw SimC input: `…/report/{ID}/simc`
- JSON output: stored as `data.json` in Google Cloud Storage

The JSON is standard SimC `json2` output:
- `sim.players[]` for single-actor sims (Quick Sim with `report_details=1`)
- `sim.profilesets` for multi-profile sims (Top Gear, Droptimizer)
- detailed damage breakdown and buff uptime are only available for Quick Sim; other sim types strip that data

## Access Model

### What is accessible

Report reading is stable and public:
- fetch a completed report by URL or ID
- extract the SimC input that was used
- parse the JSON results (standard SimC json2 format)

Static reference data is partially accessible:
- the `/developers` page exposes game data and hooks
- the archived third-party wrapper hit endpoints for instances and talents
- our `simc` CLI already provides most of this from the local source tree

### What is not accessible

Sim submission has no sanctioned programmatic path:
- no documented public API for creating simulations
- the SPA communicates with internal endpoints rendered by JavaScript
- the Discord bot proves a machine-accessible submission path exists internally, but it is not exposed for external use
- reverse-engineering internal endpoints would be fragile, likely against ToS, and could break at any time

Shared auth direction is defined in [AUTH_ARCHITECTURE.md](../architecture/AUTH_ARCHITECTURE.md). `raidbots` should be treated as a likely future session/workflow auth consumer, not as a primary driver of the shared OAuth architecture.

## CLI Shape

### Tier 1: Report Consumption — shipped

Public, stable, no auth required. See [Commands](#commands-shipped) above.

- `raidbots inspect-report <url-or-id>` — fetch and parse a completed report's JSON, present structured results
- `raidbots input <url-or-id>` — extract the SimC input that was used, hand off to local `simc`

### Tier 2: Local Bridging — shipped

Builds on existing `simc` primitives.

- `raidbots explain-input` — take SimC addon text and explain what Raidbots would do with it (entirely local; suggests `simc decode-build` / `simc describe-build`)
- crosswalk between `simc` local analysis and Raidbots report results — deferred; the no-import handoff (emitted input + suggested `simc` commands) covers the common case, and a future wrapper command can do a full crosswalk

### Tier 3: Submission (Deferred)

Low feasibility without a sanctioned API. Do not implement.

- if Raidbots ever exposes a public submission API or webhook system, revisit then
- until that point, the correct cloud sim path is: generate SimC input locally with our `simc` tooling, then the user pastes it into Raidbots manually
- an agent can prepare ready-to-paste SimC input blocks as the handoff

## What Can Reuse Shared Code

- output shaping
- cache and local report storage
- wrapper routing
- SimC json2 parsing (shared with `simc` CLI)

## What Should Depend On Earlier Work

This service should come after:
- shared output and cache layers
- the `simc` local-tool path (already implemented through phase 3)

It should not drive the first round of shared abstractions.

## What Should Stay Raidbots-Specific

- report fetching and URL resolution
- result presentation and comparison across report types
- any future submission workflow
- any session/cookie/browser constraints

## First Useful Slice — shipped

1. fetch and parse a known Raidbots report by URL or ID — `raidbots inspect-report`
2. extract and display the SimC input from that report — `raidbots input`
3. bridge the report back into local `simc` analysis — the handoff emits the input plus suggested `simc decode-build` / `describe-build` / `sim` commands (a full local-vs-report crosswalk is deferred)

This gives agents immediate value: a user shares a Raidbots link, the agent can pull it apart, explain what was simulated, and continue the conversation with local `simc` tools.

## Risks

- report URL/storage structure could change without notice
- the JSON format depends on SimC json2 which evolves across SimC versions
- deeper automation would depend on unstable or undocumented internal flows
- workflow constraints may require browser automation or authenticated sessions for anything beyond report reading
- this CLI should not become a substitute for `simc`

## Source Links

- `https://support.raidbots.com/article/54-installing-and-using-the-simulationcraft-addon`
- `https://support.raidbots.com/article/69-why-isnt-my-spec-supported`
- `https://medium.com/raidbots/raidbots-technical-architecture-303349d82784`
- `https://medium.com/raidbots/how-simbot-works-1e9d24e6093b`
- `https://www.raidbots.com/developers`
- `https://github.com/logiek/raidbots-api` (archived, discontinued)
- `https://github.com/seriallos/raidbots-issues` (archived March 2025)
- [Roadmap](../ROADMAP.md)
