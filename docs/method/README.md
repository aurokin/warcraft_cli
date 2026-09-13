# Method.gg CLI

`method` reads Method.gg guide pages: it discovers guides from the sitemap, fetches and parses a
guide page (or every page of a multi-page guide), exports a local bundle, and queries that bundle
offline. It never signs in; premium and account surfaces are out of scope.

## Commands

Global flags go before the subcommand: `--pretty`, `--compact`, `--compact-max-chars N`,
`--fields a.b`, `--fields-strict`, `--profile agent|human|debug`. They behave as described in
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

| Command | Flags | What it returns |
|---|---|---|
| `method doctor` | | readiness, capabilities, supported scope, cache configuration |
| `method search "<query>"` | `--limit` (1-50, default 5) | ranked guide candidates from the sitemap |
| `method resolve "<query>"` | `--limit` (1-50, default 5) | the single best guide plus the follow-up command |
| `method guide <slug-or-url>` | | one guide page, with a 10-item preview of linked entities, build references, and analysis surfaces |
| `method guide-full <slug-or-url>` | | every navigation page of the guide, with merged linked entities, build references, and analysis surfaces |
| `method guide-export <slug-or-url>` | `--out DIR` (default `./method_exports/guide-<slug>`) | writes a bundle to disk and returns its manifest |
| `method guide-query <bundle> "<query>"` | `--limit` (1-50, default 5), `--kind` (repeatable/comma-separated), `--section-title` | matches inside an exported bundle; no network access |

`--kind` accepts `sections`, `navigation`, `linked_entities`, `build_references`, and
`analysis_surfaces`; anything else fails with `invalid_kind`.

```bash
method resolve "mistweaver monk"
method --pretty guide mistweaver-monk
method guide-export mistweaver-monk --out ./tmp/mistweaver
method guide-query ./tmp/mistweaver "tea of serenity" --kind linked_entities
```

## Output

Every command emits one shared envelope (`ok`, `provider`, `command`, `kind`, `schema_version`,
`query`, `provenance`, `data`, and `error` on failure). The payload lives in `data`; the historical
top-level keys (`results`, `count`, `guide`, `capabilities`, ...) are still emitted next to the
envelope keys and are deprecated. Read `data`.

Error codes and their exit codes: `network_error`/`timeout`/`upstream_error` exit 5, `not_found`
exits 4, `auth_failed` exits 3, and the Method-specific input errors `invalid_guide_ref`,
`unsupported_guide_surface`, `invalid_bundle`, `invalid_kind`, and `invalid_cache_config` exit 1.

`method_cli.provider.PROVIDER` exposes the same `search`, `resolve`, and `doctor` surfaces in
process, without Typer.

## Supported scope

- root guide pages under `/guides/<slug>` and their section pages under `/guides/<slug>/<section>`
- content families `class_guide`, `profession_guide`, `delve_guide`, `reputation_guide`, `article_guide`
- index-style roots (`tier-list`, `world-of-warcraft`) are excluded from discovery, and requesting
  one directly returns `unsupported_guide_surface`
- queries whose terms match an excluded root return an empty result set with a `scope_hint`
- premium, login, account, and non-guide Method surfaces are not supported

Ranking uses the shared article scorer (`warcraft_content.search`) plus a Method-specific boost when
the query names the content family (professions, delves, renown/reputation).

## Caching

HTTP responses are cached through `warcraft_api.cache` under the `METHOD` prefix:
`METHOD_CACHE_BACKEND` (`file`, `redis`, or `none`), `METHOD_CACHE_DIR`, `METHOD_REDIS_URL`,
`METHOD_REDIS_PREFIX`, `METHOD_SITEMAP_CACHE_TTL_SECONDS` (default 86400), and
`METHOD_PAGE_CACHE_TTL_SECONDS` (default 3600). `method doctor` reports the resolved values.

## Tests

- `tests/test_method_cli.py`: parser behavior, command contracts, envelope conformance, transport failures
- `tests/test_method_synthetic_fixtures.py`: hand-written HTML fixtures in `tests/fixtures/method/`, one per content family
- `tests/test_method_live.py`: live contracts, run with `METHOD_LIVE_TESTS=1 pytest -q -m live tests/test_method_live.py`

Design history and the original research notes are in
[../architecture/history/method.md](../architecture/history/method.md).

## Source Links

- `https://www.method.gg/guides/mistweaver-monk`
- [Roadmap](../ROADMAP.md)
