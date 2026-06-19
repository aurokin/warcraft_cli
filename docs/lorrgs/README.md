# Lorrgs CLI

## Decision

**GO.**

Lorrgs has a public FastAPI JSON surface at `https://api2.lorrgs.io/api/*`, an OpenAPI document,
and an open-source implementation. It complements `warcraftlogs`: Warcraft Logs gives scoped report
and ranking primitives, while Lorrgs gives a ready cooldown-timeline view over top parses by
spec/boss and a composition-ranking view for encounters.

## Implemented

The CLI ships:

- `lorrgs doctor` — reports no-auth posture, endpoint metadata, and capability state.
- `lorrgs specs`, `lorrgs bosses`, `lorrgs roles`, `lorrgs classes`, `lorrgs zones` — static
  metadata from the Lorrgs public API.
- `lorrgs current-season` and `lorrgs season <season-slug>` — season-to-raid partition metadata
  used by the Lorrgs UI.
- `lorrgs spec <spec-slug>`, `lorrgs boss <boss-slug>`, `lorrgs zone <zone-id>` — single metadata
  lookups.
- `lorrgs spec-spells <spec-slug>`, `lorrgs boss-spells <boss-slug>`, `lorrgs spell <spell-id>`,
  `lorrgs trinkets`, `lorrgs zone-bosses <zone-id>` — spell and encounter metadata surfaces.
- `lorrgs spec-ranking <spec-slug> <boss-slug> [--difficulty mythic] [--metric dps]` — top-parse
  cooldown timelines. The payload preserves reports, fights, players, boss casts, phases, and cast
  timestamps from Lorrgs.
- `lorrgs spec-ranking-info <spec-slug> <boss-slug>` — ranking metadata without the large report
  list.
- `lorrgs comp-ranking <boss-slug> [--role ...] [--spec ...] [--killtime-min N] [--killtime-max N]`
  — top composition rows for an encounter.
- `lorrgs search <query>` and `lorrgs resolve <query>` — discovery for Lorrgs ranking URLs,
  Warcraft Logs report URLs, bare report codes, and free text spec/boss queries.
- `lorrgs report-overview <report-id-or-url>` — fetches Lorrgs report overview metadata without
  queueing fight/player timeline work.
- `lorrgs user-report <report-id-or-url>` and `lorrgs user-report-fights <report-id-or-url> --fight <ids>
  [--player <ids>] [--type <report-type>]` — read already-cached Lorrgs user-report data only.
- `warcraft cooldown-packet <report-url> --actor-id <source-id> --phase <n>` — wrapper workflow
  that joins cached Lorrgs phase/spell/top-parse context with exact Warcraft Logs actor cast events
  for player-specific phase cooldown questions.

Each command emits `{ok, provider, command, kind, query, provenance, data}` on success and
`{ok:false, ..., error:{code, message}}` with a nonzero exit on failure. Provenance carries the exact
API `source_url`, `api_host`, site URL, and upstream source posture (`warcraftlogs` data, Wowhead
tooltips).

`search` and `resolve` return ranked candidates with `follow_up.command` values. Use `resolve` when
you want a conservative one-command handoff.

## Expected User Workflow

```bash
warcraft lorrgs doctor
warcraft lorrgs search "frost mage chimaerus"
warcraft lorrgs resolve "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
warcraft lorrgs report-overview "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
warcraft cooldown-packet "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done" --actor-id 89 --phase 2
warcraft lorrgs current-season
warcraft lorrgs specs
warcraft lorrgs bosses
warcraft lorrgs spec-ranking mage-frost chimaerus-the-undreamt-god
warcraft lorrgs spec-ranking-info mage-frost chimaerus-the-undreamt-god
warcraft lorrgs comp-ranking chimaerus-the-undreamt-god --limit 10
```

Use `spec-ranking-info` first when you only need freshness/status metadata. Use `spec-ranking` when
you need the actual cast timeline rows.

## Wrapper Contract

The provider registers with:

- `doctor` — **ready**
- `search` / `resolve` — **ready**
- `season` / `current-season` — **ready**
- direct passthrough execution for Lorrgs-specific commands
- `expansion_mode = "fixed"` with `supported_expansions = ["retail"]`

Lorrgs is treated as retail-only because it uses current Warcraft Logs-derived raid ranking data and
does not expose a classic/fresh selector. It participates in retail wrapper search/resolve fanout and
is excluded when another fixed expansion such as WOTLK is requested.

## Boundaries

- No queued load or dirty endpoints are exposed.
- `user-report` commands read already-cached Lorrgs records only; they do not trigger the Lorrgs
  queued load flow.
- `user-report-fights` preserves the report `type` query parameter from Warcraft Logs/Lorrgs URLs
  and also accepts it explicitly with `--type`.
- `report-overview` calls Lorrgs' overview endpoint only; it does not request per-fight/player
  timeline generation.
- The CLI does not synthesize cooldown plans, "best timings", or normalized strategy answers. It
  returns Lorrgs' raw timeline data with provenance so agents can analyze it explicitly.
- Lorrgs data is an aggregation over top parses, not a universal recommendation. Fight duration,
  composition, externals, phase timing, and kill strategy can make top-parse timings unsuitable for a
  specific group.

## Live Tests

```bash
LORRGS_LIVE_TESTS=1 pytest -q -m live tests/test_lorrgs_live.py
```

## Source Links

- `https://lorrgs.io/`
- `https://api2.lorrgs.io/api/openapi.json`
- `https://github.com/gitarrg/lorrgs`
