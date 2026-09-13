# Raidbots CLI

`raidbots` reads public Raidbots reports and hands their SimC input off to the local `simc` CLI. It
never runs SimulationCraft itself, never imports `simc_cli`, and does not submit sims — Raidbots has
no sanctioned submission API. Tier: experimental.

Run it directly (`raidbots …`) or through the wrapper (`warcraft raidbots …`). `<url-or-id>` accepts
a bare report ID or any URL containing `/report/{ID}`.

## Commands

| Command | Purpose |
|---|---|
| `raidbots doctor` | Capabilities, cache configuration, and the resolved report URL templates. |
| `raidbots inspect-report <url-or-id>` | Fetch and parse a report's `data.json` into a kind-aware summary (quick-sim actor plus metrics, or ranked profilesets for Top Gear/Droptimizer) with freshness, citations, and scope. |
| `raidbots input <url-or-id>` | Fetch the report's SimC input and emit it with a handoff: classification plus suggested local `simc` commands. |
| `raidbots explain-input` | Classify SimC addon/profile text locally and explain the handoff. No network. |
| `raidbots search <query>` | Structured `not_supported` stub (exit 0): Raidbots publishes no report index. |
| `raidbots resolve <target>` | Structured `not_supported` stub (exit 0): open a known report with `inspect-report`. |

### Flags

Global flags go before the subcommand: `--pretty`, `--compact`, `--compact-max-chars N`,
`--fields a.b,c`, `--fields-strict`, `--profile agent|human|debug`. They behave as described in
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

| Command | Flag | Effect |
|---|---|---|
| `inspect-report` | `--no-raw` | Omit the raw `data.json` payload. Recommended for large Top Gear/Droptimizer reports. |
| `input` | — | No command flags. |
| `explain-input` | `--text TEXT` | Read inline SimC addon/profile text. |
| `explain-input` | `--file PATH` | Read SimC text from a file. With neither flag, the text is read from stdin. |
| `search` | `--limit N` | Accepted for cross-provider parity; the stub always returns zero results. |

```bash
raidbots --pretty inspect-report https://www.raidbots.com/simbot/report/abc123 --no-raw
raidbots --fields data.handoff input abc123
simc sim - < profile.simc   # what `input` suggests you run locally
```

## Output

Every command emits the shared envelope (`ok`, `provider`, `command`, `kind`, `schema_version`,
`query`, `provenance`, `data`, `error`). The payload lives in `data`; the same keys are also copied
to the top level for existing agents and are deprecated — read `data`.

| Command | `kind` | `data` keys |
|---|---|---|
| `doctor` | `doctor` | `status`, `installed`, `language`, `auth`, `capabilities`, `url_templates`, `cache`, `notes` |
| `inspect-report` | `report` | `report`, `scope`, `freshness`, `citations`, `raw` (unless `--no-raw`) |
| `input` | `simc_input` | `report_id`, `input`, `handoff`, `scope`, `freshness`, `citations` |
| `explain-input` | `simc_input` | `scope`, `handoff` |
| `search` / `resolve` | `search_results` / `resolve_match` | `results`, `count`, `not_supported`, `message`, `suggested_command` |

`freshness.from_cache` marks a payload that may be up to `cache_ttl_seconds` old; `retrieved_at` is
always when this CLI produced the response.

## Errors and exit codes

Failures write the error envelope to stderr. Codes follow
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md):

| Code | Exit | When |
|---|---|---|
| `invalid_report` | 1 | The reference is not a report URL or ID, or the payload is not SimC json2. |
| `invalid_cache_config` | 1 | `RAIDBOTS_CACHE_*` environment values are unusable. |
| `invalid_query` | 2 | Bad `explain-input` flags, empty SimC text, or upstream HTTP 400. |
| `not_found` | 4 | Upstream HTTP 404 (no such report). |
| `network_error`, `timeout`, `rate_limited`, `upstream_error` | 5 | Transport failure, HTTP 429, or any other upstream status. |

## Configuration

Report URLs are env-overridable so a live URL change needs no code change (each is `{id}`-templated):
`RAIDBOTS_BASE_URL`, `RAIDBOTS_REPORT_PATH_TEMPLATE`, `RAIDBOTS_DATA_PATH_TEMPLATE`,
`RAIDBOTS_INPUT_PATH_TEMPLATE`. The URL host of an input reference is ignored — fetches are always
rebuilt from the configured base.

Caching uses the shared `RAIDBOTS_CACHE_*` settings; `RAIDBOTS_REPORT_CACHE_TTL_SECONDS` defaults to
24 hours because completed reports are immutable.

## Handoff posture

To analyze a report locally, run the suggested `simc sim -` / `simc decode-build` /
`simc describe-build` commands. To run on the Raidbots cloud, paste the emitted input into
raidbots.com. Submission is deferred; see
[architecture/history/raidbots.md](../architecture/history/raidbots.md) for the research behind that
decision, the report structure, and the access model.
