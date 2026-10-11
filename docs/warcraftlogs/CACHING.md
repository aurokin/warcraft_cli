# Warcraft Logs caching and derived-output trust

Shipped cache contract for the typed `warcraftlogs` surface (AUR-388). This is the source
of truth; the README's "Caching Policy" / "Caching and Freshness" sections point here.

## Cache key

Every cached GraphQL response is keyed by:

```
sha256({"site": <site key>, "namespace": <command namespace>, "payload": <payload>})
```

where `payload = {"endpoint": "client"|"user", "operation_name", "query", "variables"}`.
The full query text is part of the key, so two requests that differ only by inlined query
text (for example different leaderboard enums) never collide. Client and user endpoints are
keyed separately via `endpoint`.

## Finished-vs-live report TTL

Report detail is keyed on **finish state**, derived from the report's `endTime`:

| Report state | Signal | Applied TTL |
| --- | --- | --- |
| Finished | `endTime` more than 2 hours ago | `WARCRAFTLOGS_FINISHED_REPORT_CACHE_TTL_SECONDS` (default **86400** = 24h) |
| Live / in-progress | `endTime` within the last 2 hours, `0` or absent | `WARCRAFTLOGS_REPORT_CACHE_TTL_SECONDS` (default **60s**) |

A report that is still being logged has `endTime > 0`: Warcraft Logs sets it to the latest event,
seconds before now. So `endTime > 0` alone does not mean finished; the report must also have been
quiet for two hours, which outlasts a raid break.

The TTL is resolved from the actual response at the cache-write site (both the client and
user GraphQL endpoints), so **a live report is never stored under the finished TTL**. Live
reports are still cached — briefly — and marked `live: true` rather than skipped.

`endTime == 0` (or absent) falls back to the short live TTL: unknown finish state is treated
as live, never as finished. Setting `WARCRAFTLOGS_FINISHED_REPORT_CACHE_TTL_SECONDS=0` disables
finished caching (entries expire immediately).

A response that carries GraphQL partial errors is never cached, under any TTL, so a transient
upstream failure is not replayed after Warcraft Logs recovers.

### Per-family TTL

| Family | Env override | Default |
| --- | --- | --- |
| Guild/character | `WARCRAFTLOGS_GUILD_CACHE_TTL_SECONDS` | 300s |
| Static world/zone/encounter and metadata (regions/expansions/server) | `WARCRAFTLOGS_STATIC_CACHE_TTL_SECONDS` | 21600s |
| Live/report listing baseline | `WARCRAFTLOGS_REPORT_CACHE_TTL_SECONDS` | 60s |
| Finished report detail | `WARCRAFTLOGS_FINISHED_REPORT_CACHE_TTL_SECONDS` | 86400s |

Report **listings** (`reports`, `guild-reports`) keep the short report TTL — the list itself
changes as new reports arrive even when each report appears finished.

**Report rankings** (`report-rankings`) also keep the short report TTL even for a finished
report: rankings are population-relative percentiles that Warcraft Logs keeps recomputing
after the log completes, so they are *not* immutable and must not inherit the 24h finished
TTL. The finished TTL applies to report-detail payloads (fights, events, tables, graphs,
master data, player details, and report metadata) as an explicit **staleness budget**.
Finished does not mean immutable: WCL can re-export a report and increment its `revision`, and
can change events, tables, and graphs. Report metadata and brief report payloads expose `revision`;
it is observed evidence, not an invalidation trigger. Cached revisions are not upstream revalidation.

## Derived-output trust fields

### `cache_provenance`

Report-encounter commands and sampled cross-report commands emit a `cache_provenance` block:

```json
{"finished": true, "live": false, "cache_ttl_seconds": 86400, "source": "report_detail"}
```

- Report-encounter: `source: "report_detail"`, finish state from the resolved report.
- Sampled cross-report commands: `source: "sampled_reports"`. The sampler scans reports that are
  still being logged as well as finished ones, because a kill fight is final once it ends; each kill
  row carries `report_finished`, and the block says `live: true` when any kill came from a report
  still being logged (cached under the short report TTL). `sample.live_report_count` and
  `sample.finished_report_count` split the listed reports.

### `freshness`

Sampled cross-report commands emit `freshness.cache_ttl_seconds` populated with the real
applied finished-report TTL, alongside `sampled_at`.

They also emit the transport tally for the run — `freshness.cache_hit_count`,
`freshness.upstream_request_count`, and `freshness.served_entirely_from_cache` (true when the
run made no upstream request). `sampled_at` is only when the command ran, so the tally is what
distinguishes a live scan from a warm-cache replay of an older cohort.

### `sample_scope`

Sampled cross-report commands emit a consolidated scope object:

```json
{"ranking_basis": "...", "filters": {...}, "returned": N, "excluded": N, "truncated": false}
```

`filters` is projected from the same `query` block already on the payload, so the two
representations cannot drift.

### Cache-disabled deployments

When caching is turned off (`WARCRAFTLOGS_CACHE_BACKEND=none|off|disabled`), no response is
stored, so the emitted `cache_provenance.cache_ttl_seconds` and `freshness.cache_ttl_seconds`
are `null` rather than a TTL the deployment never applies. The `finished`/`live` flags and
`source` still describe the underlying report cohort (which is independent of caching).

## Invalidation

File and Redis caches expire by TTL. Use `warcraftlogs --refresh report <code>` (or any
other read command) to bypass existing responses and replace only the queried cache entries.
This is targeted refresh, not a purge of every cached filter for that report. User endpoints remain
account-scoped; site profiles are isolated in every cache key. Raw GraphQL refresh also bypasses
cache reads, but writes only when its existing `--cache-ttl` opt-in is positive.

Use `WARCRAFTLOGS_CACHE_BACKEND=none` to disable both cache reads and writes for a run.
There is no per-report cache-admin purge command. The CLI does not poll revisions or guarantee
a coherent snapshot across independently cached report requests. A long finished TTL deliberately
trades freshness for API cost; use `--refresh` when current evidence matters.

### Live → finished staleness window

The cache key is finish-state-agnostic (it does not include `endTime`), so a report fetched
while **live** is stored under the short report TTL and can still be served from that entry
for up to that TTL (default 60s) after the report finishes, including by
sampled boss analytics. This is the accepted consequence of caching live reports
(rather than no-caching them, for rate-limit relief): the short live TTL bounds the window,
and once it expires the next fetch sees an `endTime` over two hours old and re-caches under the
finished TTL. Finished reports can still change afterward. To bypass cached evidence, run the sampling command
with `--refresh`, or disable caching using `WARCRAFTLOGS_CACHE_BACKEND=none`.

#### Provenance is a report property, not a per-namespace cache audit

For encounter commands, `cache_provenance` describes the **report's** finish state (resolved
from the report metadata lookup). A single encounter command can return data from more than
one cache namespace (`report`, `report_fights`, `report_player_details`, …), each with its own
entry. Within the same bounded live→finished window, those namespaces can momentarily disagree
— e.g. `report` already refetched as finished while `report_player_details` still serves a
≤60s-old live entry — so `cache_provenance` can briefly differ from the exact TTL applied to
one of the returned detail payloads. This is the same window bounded by the short live TTL;
provenance intentionally reports the singular report-level finish state rather than auditing
every namespace per response.

## Out of scope

- `character-rankings` is a single live character lookup, not a sampled-finished cohort, and
  does not carry `cache_provenance`/`sample_scope` (its trust block reuses sampled freshness
  with `cache_ttl_seconds: null`).
- `cache_provenance` is not retrofitted onto every typed report command — only report-encounter
  and sampled cross-report surfaces.
- Classic/fresh cache isolation is shipped: the site key is included in every response key.

## Event evidence exports

`report-events --all-pages` emits an `export` block with explicit completeness, continuation,
page/event bounds, observed revisions, collection time, and transport counts. `collected_at` is
the artifact collection time, not a claim that each page was fetched upstream then. Revision
changes stop collection before merging the new revision; callers must restart the investigation.
Missing event data and GraphQL partial errors cannot be reported as complete.

`stability.cache_policy: ttl_staleness_budget` and `stability.immutable: false` replace the
ambiguous `stability.cache_safe` boolean on encounter payloads.
