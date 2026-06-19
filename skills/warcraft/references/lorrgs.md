# Lorrgs

## Best For

- inspecting top-parse cooldown timelines by spec and boss
- seeing composition rows for an encounter
- finding Lorrgs spec, boss, spell, and zone slugs
- complementing `warcraftlogs` report reads with Lorrgs' prebuilt visual aggregation

## Start With

- readiness: `warcraft lorrgs doctor`
- discovery: `warcraft lorrgs search "frost mage chimaerus"`
- conservative handoff: `warcraft lorrgs resolve "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"`
- report overview: `warcraft lorrgs report-overview <warcraftlogs-report-url-or-code>`
- player phase cooldown packet:
  `warcraft cooldown-packet <warcraftlogs-report-url> --actor-id <source-id> --phase 2`
- current season raids: `warcraft lorrgs current-season`
- spec slugs: `warcraft lorrgs specs`
- boss slugs: `warcraft lorrgs bosses`
- cooldown timelines: `warcraft lorrgs spec-ranking mage-frost chimaerus-the-undreamt-god`
- lightweight ranking metadata: `warcraft lorrgs spec-ranking-info mage-frost chimaerus-the-undreamt-god`
- encounter composition rows: `warcraft lorrgs comp-ranking chimaerus-the-undreamt-god --limit 10`

## Effective Use

- use `spec-ranking-info` first when you only need freshness, difficulty, metric, or dirty status
- use `spec-ranking` when you need the actual report/fight/player/boss cast timelines
- use `warcraft cooldown-packet` when the question is about a specific player's cooldowns in a
  report phase; Lorrgs supplies phase markers, spell metadata, boss casts, and top-parse samples,
  while Warcraft Logs supplies exact player cast events
- use `resolve` when you have a Lorrgs URL, Warcraft Logs report URL, report code, or likely
  spec/boss query and want the next command chosen conservatively
- use `report-overview` for report metadata from a Warcraft Logs URL without requesting Lorrgs'
  per-fight/player timeline generation
- use `user-report-fights <url> --type <report-type>` when the report URL carries a view type such
  as `damage-done`; the CLI also preserves that query parameter automatically from URLs
- use `spec-spells` and `boss-spells` to interpret spell ids in timeline rows
- use `comp-ranking` filters (`--role`, `--spec`, `--killtime-min`, `--killtime-max`) when you need
  a narrower comparison cohort
- every result preserves `provenance.source_url`; follow that exact URL when you need to verify the
  source payload

## Boundaries

- user-report commands read already-cached Lorrgs records only
- queued load/dirty endpoints are intentionally not exposed
- do not turn top-parse timelines into universal cooldown recommendations without checking fight
  duration, phase timing, strategy, composition, and externals
- registered as fixed retail data for wrapper expansion filtering
