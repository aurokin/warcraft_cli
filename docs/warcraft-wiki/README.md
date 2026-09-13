# Warcraft Wiki CLI

`warcraft-wiki` is a family-aware reference CLI over [warcraft.wiki.gg](https://warcraft.wiki.gg). It reads the MediaWiki
search and parse APIs, classifies each page into a content family, and returns JSON envelopes that agents can chain.

Tier: supported.

## Commands

| Command | What it returns |
| --- | --- |
| `warcraft-wiki doctor` | Readiness, per-command capabilities, and cache configuration. |
| `warcraft-wiki search <query>` | Ranked article candidates with match reasons and follow-up commands. |
| `warcraft-wiki resolve <query>` | The single best article plus `resolved`, `confidence`, and `next_command`. |
| `warcraft-wiki article <title-or-url>` | One article: text, headings, section preview, navigation and linked-entity previews. |
| `warcraft-wiki article-full <title-or-url>` | The same article with every section and the complete linked-entity list. |
| `warcraft-wiki api <query>` | The API/framework/CVar/XML reference page a query resolves to, as a summary. |
| `warcraft-wiki api-full <query>` | The same API page with every section. |
| `warcraft-wiki event <query>` | The UI handler or event reference page a query resolves to, as a summary. |
| `warcraft-wiki event-full <query>` | The same handler page with every section. |
| `warcraft-wiki article-export <title-or-url>` | Writes an article bundle to disk and returns the manifest. |
| `warcraft-wiki article-query <bundle> <query>` | Searches an exported bundle offline. |

## Flags

Global flags come before the subcommand: `--pretty`, `--compact`, `--compact-max-chars <n>`, `--fields <path>`,
`--fields-strict`, `--profile agent|human|debug`.

Command flags:

- `search`, `resolve`: `--limit <1-50>` (default 5).
- `article-export`: `--out <dir>` (default `./warcraft-wiki_exports/article-<slug>`).
- `article-query`: `--limit <1-50>`, `--kind sections|navigation|linked_entities` (repeatable or comma-separated,
  defaults to all three), `--section-title <substring>`.

```bash
warcraft-wiki --pretty search "createframe"
warcraft-wiki api "CreateFrame"
warcraft-wiki article-export "API CreateFrame" --out ./tmp/wiki-createframe
warcraft-wiki article-query ./tmp/wiki-createframe "arguments" --kind sections
```

## Output and exit codes

Every payload is a shared envelope: `ok`, `provider`, `command`, `kind`, `schema_version`, `query`, `provenance`,
`data`, and `error` on failure. The historical top-level keys (`results`, `count`, `match`, `article`, `content`, ...)
are still emitted alongside `data` so existing agents keep working.

Exit codes follow `docs/foundation/ERROR_CONTRACT.md`: 1 generic (bad bundle path, unresolvable typed reference,
invalid cache config), 2 usage, 3 auth (upstream 401/403), 4 not found (the wiki has no such page), 5 network or
upstream failure. Failures write
the error envelope to stderr; transport failures never print a traceback.

## Content families

Search ranking, resolution, and extraction all key off a locally classified content family:

- Programming: `api_function`, `ui_handler`, `framework_page`, `xml_schema`, `cvar`, `api_changes`, `howto_programming`.
- Reference: `system_reference`, `expansion_reference`, `class_reference`, `profession_reference`, `faction_reference`,
  `zone_reference`, `patch_reference`, `lore_reference`, `guide_reference`.
- Everything else: `general_article`.

`api` and `api-full` only accept `api_function`, `framework_page`, `xml_schema`, `cvar`, and `api_changes` pages;
`event` and `event-full` only accept `ui_handler` and `framework_page` pages. A query that resolves to another family
fails with `invalid_api_ref` / `invalid_event_ref` rather than returning the wrong page.

Queries that lead with a family word are rewritten before search (`lore Jaina` -> `jaina`, `class druid` -> `druid`);
the dropped words come back as `excluded_terms` with `normalization_hint: "excluded_family_hint_terms"`.

## Caching

HTTP responses are cached under the `WARCRAFT_WIKI_*` cache settings (see `doctor` output): search results for 1800s
(`WARCRAFT_WIKI_SEARCH_CACHE_TTL_SECONDS`) and parsed pages for 3600s (`WARCRAFT_WIKI_PAGE_CACHE_TTL_SECONDS`).

## Known limits

- Page extraction is heuristic, not template-aware; framework pages vary more than function pages.
- Structured data that lives only in templates or cargo tables is not extracted.
- The family classifier is a fixed list; pages that match none of the families fall back to `general_article`.

Design history and the original completion plan live in
[`docs/architecture/history/warcraft-wiki.md`](../architecture/history/warcraft-wiki.md).
