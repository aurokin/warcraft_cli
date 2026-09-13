# Raidbots: research and staging record

Provenance for why the Raidbots provider is a report-consumption surface and why submission is not
implemented. The shipped behavior is documented in [docs/raidbots/README.md](../../raidbots/README.md);
this page is the record of the research that produced it, not a backlog.

## Why Raidbots was staged after `simc`

Raidbots is a cloud frontend for SimulationCraft. Its value is running SimC on cloud hardware so
users need no local build, and its strongest confirmed workflow is built around SimC input and
simulation results. That made it a consumer of the local `simc` primitives rather than a peer of
them, so it landed after `simc`.

## Research summary

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
- the Discord bot accepts sim commands with flags (fight style, fight length, enemy count, scaling,
  talent comparison) but is Raidbots' own internal integration
- the Terms of Use page is JS-rendered and could not be read externally

## Simulation types

| Tool | Purpose |
|---|---|
| Quick Sim | Single-profile DPS estimate with detailed stats |
| Top Gear | Compare equipped/bag gear combinations to find the best setup |
| Droptimizer | Simulate potential drops from raids/dungeons to find the most valuable content to run |
| Stat Weights | Calculate relative stat values |
| Advanced Sim | Run arbitrary raw SimC input |

All of these generate SimC input under the hood; the website is a UI for building that input.

## Report structure

Reports are public and stable:

- report page: `raidbots.com/simbot/report/{ID}`
- raw SimC input: `.../report/{ID}/simc`
- JSON output: `data.json`, stored in Google Cloud Storage

The JSON is standard SimC `json2` output, which is why the parser splits on report kind:

- `sim.players[]` for single-actor sims (Quick Sim with `report_details=1`)
- `sim.profilesets` for multi-profile sims (Top Gear, Droptimizer)
- detailed damage breakdown and buff uptime exist only for Quick Sim; other sim types strip that data

## Access model

Report reading is stable and public: fetch a completed report by URL or ID, extract the SimC input
that was used, and parse the JSON results (standard SimC json2). Static reference data is only
partially accessible, and our `simc` CLI already provides most of it from the local source tree.

Sim submission has no sanctioned programmatic path: no documented public API, SPA-internal
endpoints, and a Discord bot that proves an internal machine path exists without exposing one.
Reverse-engineering those endpoints would be fragile, likely against ToS, and could break at any
time, so submission stayed out of scope. The correct cloud path remains: generate SimC input
locally, then the user pastes it into raidbots.com.

Shared auth direction is defined in [AUTH_ARCHITECTURE.md](../AUTH_ARCHITECTURE.md). Raidbots is a
likely future session/workflow auth consumer, not a driver of the shared OAuth architecture.

## Known risks

- report URL/storage structure could change without notice (mitigated by the `RAIDBOTS_*` URL template overrides)
- the JSON format depends on SimC json2, which evolves across SimC versions
- deeper automation would depend on unstable or undocumented internal flows
- this CLI is not a substitute for `simc`

## Source links

- `https://support.raidbots.com/article/54-installing-and-using-the-simulationcraft-addon`
- `https://support.raidbots.com/article/69-why-isnt-my-spec-supported`
- `https://medium.com/raidbots/raidbots-technical-architecture-303349d82784`
- `https://medium.com/raidbots/how-simbot-works-1e9d24e6093b`
- `https://www.raidbots.com/developers`
- `https://github.com/logiek/raidbots-api` (archived, discontinued)
- `https://github.com/seriallos/raidbots-issues` (archived March 2025)
