# Method.gg CLI

`method` reads Method.gg guide pages: it discovers guides from the sitemap, fetches and parses a
guide page (or every page of a multi-page guide), exports a local bundle, and queries that bundle
offline. It never signs in; premium and account surfaces are out of scope.

## Commands

Global flags go before the subcommand: `--pretty`, `--compact`, `--compact-max-chars N`,
`--fields a.b`, `--fields-strict`, `--profile agent|human`. They behave as described in
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

| Command | What it returns |
|---|---|
| `method doctor` | readiness, capabilities, supported scope, cache configuration |
| `method search "<query>"` | ranked guide candidates from the sitemap |
| `method resolve "<query>"` | the single best guide plus the follow-up command |
| `method guide <slug-or-url>` | one guide page, with a 10-item preview of linked entities, build references, and analysis surfaces |
| `method guide-full <slug-or-url>` | every navigation page of the guide, with merged linked entities, build references, and analysis surfaces |
| `method guide-export <slug-or-url>` | writes a bundle to disk and returns its manifest |
| `method guide-query <bundle> "<query>"` | matches inside an exported bundle; no network access |

Flags, defaults, and ranges are in [reference/method.md](../reference/method.md).

`--kind` accepts `sections`, `navigation`, `linked_entities`, `build_references`, and
`analysis_surfaces`; anything else fails with `invalid_argument` (exit 2).

```bash
method resolve "mistweaver monk"
method --pretty guide mistweaver-monk
method guide-export mistweaver-monk --out ./tmp/mistweaver
method guide-query ./tmp/mistweaver "tea of serenity" --kind linked_entities
```

## Output

Every command emits one shared envelope (`ok`, `provider`, `command`, `kind`, `schema_version`,
`query`, `provenance`, `data`, and `error` on failure) and no other top-level key; the payload is in
`data`.

Error codes and their exit codes: `network_error`/`timeout`/`upstream_error` exit 5, `not_found`
exits 4, `auth_failed` exits 3, and the Method-specific errors `unsupported_guide_surface`,
`invalid_bundle`, and `invalid_cache_config` exit 1; an argument that is not a Method guide reference
is `invalid_guide_ref` (exit 2), an unsupported `--kind` is `invalid_argument` (exit 2), and a blank
`search` or `resolve` query is `invalid_query` (exit 2), rejected before any request.
`guide-query` answers a bad bundle path the same way `icy-veins guide-query` does: a path that does
not exist is `not_found` (exit 4), a file is `invalid_argument` (exit 2), and a directory that is not
a readable bundle is `invalid_bundle` (exit 1): no `manifest.json`, a manifest whose `files` lists no
content file (`pages.jsonl`, `sections.jsonl`, `analysis-surfaces.jsonl`, ...), or a listed file that
is missing, corrupt, or holds a row with a wrongly typed nested field (a `build_identity` that is not
an object, `surface_tags` that is not a list). A `wowhead guide-export` bundle is readable; it has sections, navigation,
linked entities and analysis surfaces, but no pages or build references.
`invalid_guide_ref` means the argument was not a Method guide reference; a page that fetched but
whose article container no longer matches fails with `parse_failed` (exit 1) instead of returning
an empty article with `ok:true`.

### Build references

`build_references` carries explicit build evidence from the page, never a guess from the slug or
title. Two reference types are emitted:

| `reference_type` | Source on the page | `url` |
| --- | --- | --- |
| `wowhead_talent_calc_url` | an embedded Wowhead talent-calc link | the talent-calc URL |
| `wow_talent_export` | a published WoW loadout import string (the talent blocks on `/talents` pages) | the import string itself, because the reference has no link |

Both types set `build_code`, so `warcraft guide-builds-simc` collects either one and reports it
under `summary.identify_success_count`. Decoding needs a class and a spec, and the two types supply
them differently:

- `wowhead_talent_calc_url` always decodes unaided: its URL path names the class and spec.
- `wow_talent_export` names neither, so `build_identity` stays unknown on the row and SimC has to
  identify the string itself. It probes every spec in SimC's specialization data, healers included.

So `simc decode-build --talents <build_code>` returns `ok:true` for a build of any role without
`--actor-class` or `--spec`: a captured Mistweaver Monk export decodes as `monk`/`mistweaver`.

The Method `/talents` sections publish import strings rather than talent-calc links, so in practice
the rows you get back are `wow_talent_export`.

### Partial guide bundles

`guide-full` and `guide-export` walk every navigation page. A page that cannot be fetched or parsed
is skipped rather than failing the whole bundle, and it is reported in `data.failed_pages`
(`{count, items: [{url, section_slug, error: {code, message}}]}`). Content from those pages is
missing from the merged sections, entities, build references, and analysis surfaces.

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
the query names the content family (professions, delves, renown/reputation). Method titles never
say "Mythic+" or "Mythic Plus"; its M+ pages talk about "mythic dungeons", so `mythic+`, `m+` and
`mythic plus` are searched as `mythic dungeon`.

## Caching

HTTP responses are cached through `warcraft_api.cache` under the `METHOD` prefix:
`METHOD_CACHE_BACKEND` (`file`, `redis`, or `none`), `METHOD_CACHE_DIR`, `METHOD_REDIS_URL`,
`METHOD_REDIS_PREFIX`, `METHOD_SITEMAP_CACHE_TTL_SECONDS` (default 86400), and
`METHOD_PAGE_CACHE_TTL_SECONDS` (default 3600). `method doctor` reports the resolved values, with
`redis_url` stripped of its credentials and query string (`redis://***@host:6379/0`).

## Tests

- `tests/test_method_cli.py`: parser behavior, command contracts, envelope conformance, transport failures
- `tests/test_method_synthetic_fixtures.py`: hand-written HTML fixtures in `tests/fixtures/method/`, one per content family
- `tests/test_method_captured_fixtures.py`: one captured real guide page (`captured_talents_page.html`), which pins the parser against production markup
- `tests/e2e/test_method.py`: live end-to-end journeys, run with `make test-e2e E2E_ARGS="tests/e2e/test_method.py"`

## Source Links

- `https://www.method.gg/guides/mistweaver-monk`
- [Roadmap](../ROADMAP.md)
