# Lorrgs CLI

**Tier: supported.** Lorrgs is a thin, no-auth public-API provider with a narrow surface — check
`lorrgs doctor` before building a workflow on it.

`lorrgs` reads the public Lorrgs API (`https://api2.lorrgs.io/api/*`) and returns raw Lorrgs JSON with
provenance. Lorrgs renders Warcraft Logs-derived cooldown timelines for top parses by spec and boss plus
composition rankings per encounter. No auth is required.

## Global flags

Global flags go before the subcommand and are the same on every binary:
`--pretty`, `--compact`, `--compact-max-chars <n>`, `--fields <dot.path>`, `--fields-strict`,
`--profile agent|human`.

```bash
lorrgs --pretty specs
lorrgs --fields data.specs specs
```

## Commands

| Command | What it returns |
|---------|-----------------|
| `doctor` | Auth posture (none required), endpoints, per-surface capability state, and the cache configuration. |
| `search <query> [--limit N]` | Ranked Lorrgs candidates with `follow_up.command` values. `--limit` defaults to 5, max 50. |
| `resolve <query> [--limit N]` | Conservative single-command handoff: `resolved`, `confidence`, `match`, `next_command`, `fallback_search_command`, `candidates`. |
| `roles`, `classes`, `specs`, `zones`, `bosses`, `trinkets` | Static Lorrgs metadata collections. |
| `spec <spec-slug>` | Metadata for one spec. |
| `spec-spells <spec-slug>` | Tracked cooldown spells for one spec. |
| `zone <zone-id>`, `zone-bosses <zone-id>` | Raid zone metadata and its bosses. |
| `boss <boss-slug>`, `boss-spells <boss-slug>` | Encounter metadata and tracked boss abilities. |
| `spell <spell-id>` | Lorrgs metadata for one spell id. |
| `season <season-slug>`, `current-season` | Season-to-raid partition metadata. `season` defaults to `current`. |
| `spec-ranking <spec-slug> <boss-slug> [--difficulty mythic\|heroic] [--metric dps]` | Top-parse cooldown timelines: reports, fights, players, boss casts, phases, cast timestamps. When Lorrgs returns `reports: []`, `data.notes` says the upstream ranking is empty, which is not evidence the spec is unplayed on that boss. |
| `spec-ranking-info <spec-slug> <boss-slug> [--difficulty mythic\|heroic] [--metric dps]` | Ranking metadata without the large report list. Lorrgs ranks Mythic and Heroic only, so any other `--difficulty` is a usage error (exit 2). |
| `comp-ranking <boss-slug> [--limit N] [--role EXPR]... [--spec EXPR]... [--killtime-min S] [--killtime-max S]` | Top composition rows for an encounter. `--role`/`--spec` are repeatable count filters `<name>.<op>.<n>` with `op` one of `eq`, `gt`, `gte`, `lt`, `lte`: `--role heal.gte.4`, `--spec mage-frost.gte.1`. `--role` names a role code, `tank`, `heal`, `mdps` or `rdps` (not the display name `Healer`); `--spec` names a spec slug from `lorrgs specs` or any other spelling of one (see `<spec-slug>` below). Any other spelling (`heal>=4`) or role is a usage error (exit 2) before the request, because Lorrgs answers the first with HTTP 500 and silently matches nothing for the second. When Lorrgs returns `reports: []`, `data.notes` says the upstream ranking is empty for that boss and those filters. |
| `report-overview <report-ref> [--refresh/--no-refresh]` | Lorrgs report overview metadata for any public Warcraft Logs report; Lorrgs loads one it has not seen on demand, and `--refresh` asks it to reload one it has. Does not queue per-fight timeline work. |
| `user-report <report-ref>` | Already-cached Lorrgs user report overview. |
| `user-report-fights <report-ref> [--fight IDS] [--player IDS] [--type TYPE]` | Selected cached fights. `--fight` and `--type` default to the values parsed from a report URL. |

`<report-ref>` is a Warcraft Logs report URL, a Lorrgs `user_report(s)` URL, or a bare 16-character report code (letters and digits, mixed case; it need not contain a digit).
`<spec-slug>` and the `--spec` filter name take any provider's spelling of a spec and send Lorrgs its
own slug: `balance-druid`, `Balance Druid`, `BeastMastery`, `death-knight-frost` and `bm hunter` become
`druid-balance`, `hunter-beastmastery` and `deathknight-frost`, and `query.spec_slug` echoes the slug sent.
A bare spec several classes share (`frost`) and anything else go to Lorrgs as typed (its `other-*`
pseudo-specs work this way). When Lorrgs answers not_found for such a value on a spec route,
`error.details.suggestions` lists the closest Lorrgs spec slugs (and is absent when none is close).
Lorrgs does not reject an unknown `--spec` filter name: `comp-ranking` returns `reports: []` with exit 0.
`--fight` and `--player` take dot-separated id lists (`2.4.15`).

The wrapper adds `warcraft cooldown-packet <report-url> --actor-id <source-id> --phase <n>`, which joins
cached Lorrgs phase/spell/top-parse context with Warcraft Logs actor cast events. Lorrgs only serves
reports it has already cached; for any other report add `--spec-slug <spec>` (any provider's spelling) and the command
degrades to the Warcraft Logs half with `data.lorrgs.status: "unavailable"` and no phase windows. Without
both flags it fails and names them.

## Output contract

Every command emits one JSON envelope: `ok`, `provider`, `command`, `kind`, `schema_version`, `query`,
`provenance`, `data`, and `error` when `ok` is false, and no other top-level key. The payload is in `data`.

`provenance` carries the exact API `source_url`, `api_host`, the site URL, upstream source posture
(`warcraftlogs` data, Wowhead tooltips), and the shared `cache` block (whether the answer was replayed
from the cache, and how old the replay is; see [USAGE.md](../USAGE.md#reading-cache-state-provenancecache)).

## Caching

Responses are cached on disk under the XDG cache root (`lorrgs/http`). Static metadata (roles,
classes, specs, spells, zones, bosses, seasons, trinkets) keeps 12 hours, `spec-ranking`,
`spec-ranking-info` and `comp-ranking` 30 minutes, and `user-report-fights` 6 hours, but only for a
fight Lorrgs has loaded (one with players). `report-overview` and `user-report` are never cached.
Override with `LORRGS_STATIC_CACHE_TTL_SECONDS`, `LORRGS_RANKING_CACHE_TTL_SECONDS`,
`LORRGS_REPORT_CACHE_TTL_SECONDS`, `LORRGS_CACHE_DIR`, or `LORRGS_CACHE_BACKEND=file|redis|none`
(Redis takes `LORRGS_REDIS_URL` and `LORRGS_REDIS_PREFIX`).

Failures write the envelope to stderr and exit with the shared codes from
[docs/foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md): 1 generic, 2 usage (bad
flags, an unparseable report reference, a missing `--fight`, a `--difficulty` Lorrgs does not rank,
a malformed `--role`/`--spec` filter or unknown `--role` name, or a Lorrgs 422), 4 not found
(Lorrgs 404, which `user-report` also returns for a report Lorrgs has not loaded yet, and Lorrgs 401/403
— it takes no credentials, so a refusal means Warcraft Logs keeps the report private, never an auth
problem), 5 network, timeout, rate limit, or other upstream failure. No Lorrgs command exits 3.

## How search and resolve rank

`search` ranks spec/boss candidates. `resolve` promotes the top one when three things hold: it accounts
for every query word except filler (`the`, `of`, `on`, `cooldowns`, `top`, ...), no equally well matched candidate of the same kind names a
different spec or encounter, and it is a high-confidence match. A query that named a row only by some of
its words (`storm` for Raszageth the Storm-Eater) is unrivalled but thin: it comes back with
`resolved: false`, `confidence: "medium"`, the candidate in `match` (see `match.ranking.match_level` and
`match.follow_up.command`), and `next_command: null`. Like every provider, Lorrgs only resolves at
`confidence: "high"`. Under the shared one-word rule, a one-word query keeps its high answer when it
is a spec word only one class has (`shadow` for Shadow Priest), or when it names the boss whole or by
its head before a `,` or an epithet (`dimensius`, `chimaerus` for Chimaerus the Undreamt God). Any
other boss short name (`ansurek` for Queen Ansurek, `jailer` for The Jailer, Zovaal, `anduin` for
Anduin Wrynn) comes back `medium` with `confidence_cap: {"rule": "single_word_query", "from": "high"}`.

A word the top candidate ignores blocks the handoff: `fire mage paladin` leaves `paladin` in
`unmatched_terms`, so it returns `resolved: false` rather than answering the narrower Fire Mage
question, and `frost mage guide` leaves `guide` (Lorrgs has no guides). Every row tied for the best score is emitted, so a query that names a spec Lorrgs
has twice (`frost` is Mage and Death Knight) or an encounter short name it has twice (`salhadaar` is
Fallen-King and Nexus-King) comes back with `resolved: false`, `confidence: "low"`,
`next_command: null`, the first tied row in `match`, and every tied candidate in `candidates` — narrow
the query or pick a slug. A query with a word left over is `low` the same way.

A difficulty word (`mythic`, `heroic`, `normal`, `lfr`) is not matched against specs or bosses; it is
carried into the ranking handoff instead: `heroic frost mage chimaerus` resolves to
`lorrgs spec-ranking mage-frost chimaerus-the-undreamt-god --difficulty heroic`. `comp-ranking` takes no
difficulty, so a heroic, normal, or LFR query lists the difficulty in its `unmatched_terms` and does not
resolve to it. Lorrgs ranks no Normal or LFR, so neither `search` nor `resolve` offers a
`spec-ranking` candidate for a normal or LFR query.

A tie between *different* kinds is a preference, not ambiguity, and it is fixed: a bare encounter name
(`chimaerus`) resolves to `comp-ranking`, because the ranking is the useful surface and the `boss`
metadata row scored the same only because it was built from the same match.

`--limit` only trims what is printed: `resolve` judges ambiguity over every candidate. In `search` and
`resolve`, `count` is the rows in `results`/`candidates`, `total_matches` is every ranked candidate, and
`truncated` says rivals were cut. An unresolved answer carries `fallback_search_command`
(`lorrgs search '<query>'`). Spec-ranking and comp-ranking rows carry the Lorrgs page they render in
`url`; a report row built from a `lorrgs.io/user_report/...` URL carries that URL, and spec, boss and
other report rows have `url: null`.

A report reference comes back as the `match` (`follow_up.command` is `lorrgs report-overview <code>`)
at `confidence: "medium"` with a `caveat`, and `resolved: false`: the reference parsed exactly, but
nothing verified that Lorrgs will serve it (Lorrgs loads any public report, but refuses reports Warcraft
Logs keeps private). Run `report-overview` on it directly.

## Examples

```bash
warcraft lorrgs doctor
warcraft lorrgs search "frost mage chimaerus"
warcraft lorrgs resolve "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
warcraft lorrgs report-overview "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
warcraft cooldown-packet "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done" --actor-id 89 --phase 2
warcraft lorrgs current-season
warcraft lorrgs spec-ranking-info mage-frost chimaerus-the-undreamt-god
warcraft lorrgs spec-ranking mage-frost chimaerus-the-undreamt-god
warcraft lorrgs comp-ranking chimaerus-the-undreamt-god --limit 10 --role heal.gte.4
```

Use `spec-ranking-info` when you only need freshness/status metadata, and `spec-ranking` when you need the
cast timeline rows.

## Wrapper registration

- Registered with `status = "partial"` and `auth_required = false`.
- The wrapper routes `doctor`, `search`, and `resolve` in-process; every other Lorrgs command runs as
  direct passthrough: `warcraft lorrgs <command> ...`.
- `lorrgs doctor` lists per-command capabilities. `user_report` and `user_report_fights` are
  `ready_cached_only`: they answer only for reports Lorrgs has already cached, and a `report-overview`
  call does not make a report readable by `user-report` right away.
- `expansion_mode = "fixed"`, `supported_expansions = ["retail"]`, so Lorrgs joins retail wrapper
  search/resolve fanout and is skipped when a fixed non-retail expansion is requested.

## Boundaries

- Queued load and dirty endpoints are not exposed.
- `user-report` and `user-report-fights` read already-cached Lorrgs records; they do not trigger the Lorrgs
  queued load flow, so they fail when Lorrgs has not loaded that report yet.
- `report-overview` calls the overview endpoint only; it does not request per-fight/player timeline
  generation.
- The CLI returns Lorrgs' raw data. It does not synthesize cooldown plans or "best timings"; top-parse
  timings depend on fight duration, composition, externals, and phase timing.

## Live tests

```bash
make test-e2e E2E_PATHS="tests/e2e/test_lorrgs.py"
```

## Source links

- `https://lorrgs.io/`
- `https://api2.lorrgs.io/api/openapi.json`
- `https://github.com/gitarrg/lorrgs`
