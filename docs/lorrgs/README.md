# Lorrgs CLI

**Tier: supported.** Lorrgs is a thin, no-auth public-API provider with a narrow surface — check
`lorrgs doctor` before building a workflow on it.

`lorrgs` reads the public Lorrgs API (`https://api2.lorrgs.io/api/*`) and returns raw Lorrgs JSON with
provenance. Lorrgs renders Warcraft Logs-derived cooldown timelines for top parses by spec and boss plus
composition rankings per encounter. No auth is required.

Design record: [docs/architecture/history/lorrgs.md](../architecture/history/lorrgs.md).

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
| `doctor` | Auth posture (none required), endpoints, and per-surface capability state. |
| `search <query> [--limit N]` | Ranked Lorrgs candidates with `follow_up.command` values. `--limit` defaults to 5, max 50. |
| `resolve <query> [--limit N]` | Conservative single-command handoff: `resolved`, `confidence`, `match`, `next_command`. |
| `roles`, `classes`, `specs`, `zones`, `bosses`, `trinkets` | Static Lorrgs metadata collections. |
| `spec <spec-slug>` | Metadata for one spec. |
| `spec-spells <spec-slug>` | Tracked cooldown spells for one spec. |
| `zone <zone-id>`, `zone-bosses <zone-id>` | Raid zone metadata and its bosses. |
| `boss <boss-slug>`, `boss-spells <boss-slug>` | Encounter metadata and tracked boss abilities. |
| `spell <spell-id>` | Lorrgs metadata for one spell id. |
| `season <season-slug>`, `current-season` | Season-to-raid partition metadata. `season` defaults to `current`. |
| `spec-ranking <spec-slug> <boss-slug> [--difficulty mythic] [--metric dps]` | Top-parse cooldown timelines: reports, fights, players, boss casts, phases, cast timestamps. |
| `spec-ranking-info <spec-slug> <boss-slug> [--difficulty mythic] [--metric dps]` | Ranking metadata without the large report list. |
| `comp-ranking <boss-slug> [--limit N] [--role EXPR]... [--spec EXPR]... [--killtime-min S] [--killtime-max S]` | Top composition rows for an encounter. `--role`/`--spec` are repeatable filter expressions such as `heal>=4`. When Lorrgs returns `reports: []`, `data.notes` says the upstream ranking is empty for that boss and those filters. |
| `report-overview <report-ref> [--refresh/--no-refresh]` | Lorrgs report overview metadata for any public Warcraft Logs report; Lorrgs loads one it has not seen on demand, and `--refresh` asks it to reload one it has. Does not queue per-fight timeline work. |
| `user-report <report-ref>` | Already-cached Lorrgs user report overview. |
| `user-report-fights <report-ref> [--fight IDS] [--player IDS] [--type TYPE]` | Selected cached fights. `--fight` and `--type` default to the values parsed from a report URL. |

`<report-ref>` is a Warcraft Logs report URL, a Lorrgs `user_report(s)` URL, or a bare report code.
`--fight` and `--player` take dot-separated id lists (`2.4.15`).

The wrapper adds `warcraft cooldown-packet <report-url> --actor-id <source-id> --phase <n>`, which joins
cached Lorrgs phase/spell/top-parse context with Warcraft Logs actor cast events. Lorrgs only serves
reports it has already cached; for any other report add `--spec-slug <lorrgs-spec-slug>` and the command
degrades to the Warcraft Logs half with `data.lorrgs.status: "unavailable"` and no phase windows. Without
both flags it fails and names them.

## Output contract

Every command emits one JSON envelope: `ok`, `provider`, `command`, `kind`, `schema_version`, `query`,
`provenance`, `data`, and `error` when `ok` is false, and no other top-level key. The payload is in `data`.

`provenance` carries the exact API `source_url`, `api_host`, the site URL, and upstream source posture
(`warcraftlogs` data, Wowhead tooltips).

Failures write the envelope to stderr and exit with the shared codes from
[docs/foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md): 1 generic (including an
unparseable report reference or a missing `--fight`), 2 usage (bad flags or a Lorrgs 422), 4 not found
(Lorrgs 404, and Lorrgs 401/403 — it takes no credentials, so a refusal means the report is private or
not loaded, never an auth problem), 5 network, timeout, rate limit, or other upstream failure. No Lorrgs
command exits 3.

## How search and resolve rank

`search` ranks spec/boss candidates. `resolve` promotes the top one when two things hold: it accounts
for every query word Lorrgs recognised, and no equally well matched candidate of the same kind names a
different spec or encounter. There is no minimum strength — a query that matched only partially still
resolves if it is unrivalled, and says so with `confidence: "medium"` and `match.ranking.match_level`.

A word Lorrgs recognises that the top candidate ignores blocks the handoff: `fire mage paladin` leaves
`paladin` in `unmatched_terms`, so it returns `resolved: false` rather than answering the narrower
Fire Mage question. Every row tied for the best score is emitted, so a query that names a spec Lorrgs
has twice (`frost` is Mage and Death Knight) or an encounter short name it has twice (`salhadaar` is
Fallen-King and Nexus-King) comes back with `resolved: false`, `confidence: "none"`,
`next_command: null`, and every tied candidate in `results` — narrow the query or pick a slug.

A tie between *different* kinds is a preference, not ambiguity, and it is fixed: a bare encounter name
(`chimaerus`) resolves to `comp-ranking`, because the ranking is the useful surface and the `boss`
metadata row scored the same only because it was built from the same match.

`--limit` only trims what is printed: `resolve` judges ambiguity over every candidate, and its payload
carries `count` plus `truncated` so a caller can tell that rivals were cut from `results`.

A report reference resolves to `lorrgs report-overview <code>` at `confidence: "medium"` with a
`caveat`: the reference parsed exactly, but nothing verified that Lorrgs will serve it (Lorrgs loads
any public report, but refuses reports Warcraft Logs keeps private).

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
warcraft lorrgs comp-ranking chimaerus-the-undreamt-god --limit 10 --role "heal>=4"
```

Use `spec-ranking-info` when you only need freshness/status metadata, and `spec-ranking` when you need the
cast timeline rows.

## Wrapper registration

- Registered with `status = "partial"` and `auth_required = false`.
- Wrapper capabilities marked ready: `doctor`, `search`, `resolve`, `spec_ranking`, `comp_ranking`,
  `season`, `current_season`, `metadata`, `report_overview`. `user_report` and `user_report_fights` are
  `ready_cached_only`: they answer only for reports Lorrgs has already cached, and a `report-overview`
  call does not make a report readable by `user-report` right away.
- Every other Lorrgs command runs as direct passthrough: `warcraft lorrgs <command> ...`.
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
make test-e2e E2E_ARGS="tests/e2e/test_lorrgs.py"
```

## Source links

- `https://lorrgs.io/`
- `https://api2.lorrgs.io/api/openapi.json`
- `https://github.com/gitarrg/lorrgs`
