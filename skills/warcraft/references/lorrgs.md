# Lorrgs

**Tier: supported.** Lorrgs is a narrow provider. Prefer `warcraftlogs` for anything that must be
authoritative, and use Lorrgs for its prebuilt aggregation.

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
  (Lorrgs only serves reports it has already cached; for any other report add
  `--spec-slug <lorrgs-spec-slug>`). For a report Lorrgs has not cached, also pass
  `--boss-slug <slug>` (see `warcraft lorrgs bosses`) or the top-parse comparison is skipped with
  `comparison.reason: no_boss_slug`.
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
- `cooldown-packet` needs Lorrgs to have cached the report, which most guild and private reports
  are not. Pass `--actor-id` and `--spec-slug` and it still returns the Warcraft Logs half with
  `data.lorrgs.status: "unavailable"`, `data.phase.status: "unavailable"`, a null
  `data.phase.selected`, and the player's casts intact. `data.lorrgs.message` names the reason and
  only says "no cached copy" for a `not_found`; a timeout or transport failure says so instead.
  Without both flags there is nothing left to build, so the command fails and names them
- use `resolve` when you have a Lorrgs URL, Warcraft Logs report URL, report code, or likely
  spec/boss query and want the next command chosen conservatively
- when `resolve` answers `resolved: false` with `confidence: "none"`, read `results`: either two
  candidates tied, so re-ask with a spec slug or boss slug (`frost` matches Mage and Death Knight;
  `salhadaar` matches two encounters), or the top candidate left a recognised word in
  `ranking.unmatched_terms` and would have answered a narrower question than you asked
- a report handoff resolves at `confidence: "medium"` with a `caveat`: nothing checked that Lorrgs
  can serve that report, and it refuses reports Warcraft Logs keeps private
- use `report-overview` for report metadata from any public Warcraft Logs URL, including one Lorrgs
  has not cached, without requesting Lorrgs' per-fight/player timeline generation
- an empty `comp-ranking` or `spec-ranking` (`reports: []`) carries `data.notes`: Lorrgs has no
  ranked rows for that boss (and spec or filters) yet, which is not a ranking and not evidence the
  spec is unplayed there
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
