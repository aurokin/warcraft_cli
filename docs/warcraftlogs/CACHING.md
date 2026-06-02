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
| Finished | `endTime > 0` | `WARCRAFTLOGS_FINISHED_REPORT_CACHE_TTL_SECONDS` (default **86400** = 24h) |
| Live / in-progress | `endTime` is `0`/absent | `WARCRAFTLOGS_REPORT_CACHE_TTL_SECONDS` (default **60s**) |

The TTL is resolved from the actual response at the cache-write site (both the client and
user GraphQL endpoints), so **a live report is never stored under the finished TTL**. Live
reports are still cached — briefly — and marked `live: true` rather than skipped.

`endTime == 0` (or absent) falls back to the short live TTL: unknown finish state is treated
as live, never as finished. Setting `WARCRAFTLOGS_FINISHED_REPORT_CACHE_TTL_SECONDS=0`
disables finished caching (entries expire immediately).

### Per-family TTL

| Family | Env override | Default |
| --- | --- | --- |
| Metadata (regions/expansions/server) | `WARCRAFTLOGS_METADATA_CACHE_TTL_SECONDS` | 900s |
| Guild/character | `WARCRAFTLOGS_GUILD_CACHE_TTL_SECONDS` | 300s |
| Static world/zone/encounter | `WARCRAFTLOGS_STATIC_CACHE_TTL_SECONDS` | 21600s |
| Live/report listing baseline | `WARCRAFTLOGS_REPORT_CACHE_TTL_SECONDS` | 60s |
| Finished report detail | `WARCRAFTLOGS_FINISHED_REPORT_CACHE_TTL_SECONDS` | 86400s |

Report **listings** (`reports`, `guild-reports`) keep the short report TTL — the list itself
changes as new reports arrive even though each finished report is stable.

**Report rankings** (`report-rankings`) also keep the short report TTL even for a finished
report: rankings are population-relative percentiles that Warcraft Logs keeps recomputing
after the log completes, so they are *not* immutable and must not inherit the 24h finished
TTL. The finished TTL applies only to the immutable report-detail payloads (fights, events,
tables, graphs, master data, player details, and the report metadata lookup).

## Derived-output trust fields

### `cache_provenance`

Report-encounter commands and sampled cross-report commands emit a `cache_provenance` block:

```json
{"finished": true, "live": false, "cache_ttl_seconds": 86400, "source": "report_detail"}
```

- Report-encounter: `source: "report_detail"`, finish state from the resolved report.
- Sampled cross-report commands: `source: "sampled_finished_reports"`, always
  `finished: true` — the sampler excludes live reports before scanning, so the cohort is
  finished reports cached under the finished TTL.

### `freshness`

Sampled cross-report commands emit `freshness.cache_ttl_seconds` populated with the real
applied finished-report TTL (was previously `null`), alongside `sampled_at`.

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

File and Redis caches expire by TTL; there is no manual per-report invalidation. Use the
shared cache-admin commands (`cache inspect` / `cache clear`) to drop namespaces. Because the
finished TTL is long, re-fetching a report that has since been edited waits out the TTL or a
manual clear; finished WoW logs are effectively immutable, so this is acceptable.

### Live → finished staleness window

The cache key is finish-state-agnostic (it does not include `endTime`), so a report fetched
while **live** is stored under the short report TTL and can still be served from that entry
for up to that TTL (default 60s) after the report finishes — even by a finished-only workflow
such as sampled boss analytics. This is the accepted consequence of caching live reports
(rather than no-caching them, for rate-limit relief): the short live TTL bounds the window,
and once it expires the next fetch sees `endTime > 0` and re-caches under the finished TTL.
Finished WoW logs are immutable thereafter. To eliminate the window for a specific report,
`cache clear` the report namespace before sampling.

A coarse `cache_hits` diagnostics counter exists in `warcraft_core.output`; `cache_provenance`
is a finer-grained, per-payload surface and does not replace it.

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
- Classic/fresh cache isolation is deferred (AUR-389).
