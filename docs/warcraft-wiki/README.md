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
| `warcraft-wiki event <query>` | The game event or UI handler reference page a query resolves to, as a summary. |
| `warcraft-wiki event-full <query>` | The same event page with every section. |
| `warcraft-wiki article-export <title-or-url>` | Writes an article bundle to disk and returns the manifest. |
| `warcraft-wiki article-query <bundle> <query>` | Searches an exported bundle offline. |

## Flags

Global flags come before the subcommand: `--pretty`, `--compact`, `--compact-max-chars <n>`, `--fields <path>`,
`--fields-strict`, `--profile agent|human`.

Command flags:

- `search`, `resolve`: `--limit <1-50>` (default 5).
- `article-export`: `--out <dir>` (default `./warcraft-wiki_exports/article-<slug>`).
- `article-query`: `--limit <1-50>`, `--kind sections|navigation|linked_entities` (repeatable or comma-separated,
  defaults to all three), `--section-title <substring>`.

```bash
warcraft-wiki --pretty search "createframe"
warcraft-wiki api "CreateFrame"
warcraft-wiki event "PLAYER_LOGIN"
warcraft-wiki article-export "API CreateFrame" --out ./tmp/wiki-createframe
warcraft-wiki article-query ./tmp/wiki-createframe "arguments" --kind sections
```

## Output and exit codes

Every payload is a shared envelope: `ok`, `provider`, `command`, `kind`, `schema_version`, `query`, `provenance`,
`data`, and `error` on failure, and nothing else at the top level: every payload field is under `data`.

Exit codes follow `docs/foundation/ERROR_CONTRACT.md`: 1 generic (unreadable bundle, invalid cache config, a
MediaWiki error code with no shared meaning, passed through verbatim), 2 usage (including `invalid_argument` for an
unsupported `article-query --kind` or a bundle path that is a file, and `invalid_query` for a blank `search`/`resolve`
query, rejected before any request), 3 auth (upstream 401/403), 4 not found (the wiki has no such page, no
`api`/`event` page matches the query, or the bundle directory does not exist), 5 network or upstream failure
(including `rate_limited` for MediaWiki's `ratelimited`, and `upstream_error` for `maxlag`, `readonly`, or a body that
is not JSON). Failures write
the error envelope to stderr; transport failures never print a traceback.

## Content families

Search ranking, resolution, and extraction all key off a locally classified content family:

- Programming: `api_function`, `ui_handler`, `event_reference`, `framework_page`, `xml_schema`, `cvar`, `api_changes`,
  `howto_programming`.
- Reference: `system_reference`, `expansion_reference`, `class_reference`, `profession_reference`, `faction_reference`,
  `zone_reference`, `patch_reference`, `lore_reference`, `guide_reference`.
- Everything else: `general_article`.

`api` and `api-full` only accept `api_function`, `framework_page`, `xml_schema`, `cvar`, and `api_changes` pages;
`event` and `event-full` only accept `event_reference`, `ui_handler`, and `framework_page` pages. Both surfaces fetch
exact titles before they search: `api` tries `API:<query>` then `API <query>`, `event` tries `Event:<query>` then
`UIHANDLER <query>`, and both fall back to the bare title. Only if all three miss does the query go to ranked search,
and a query that matches nothing in the allowed families fails with `not_found` (exit 4) rather than returning the
wrong page. Event names may be written with underscores or spaces (`PLAYER_LOGIN`, `Event:PLAYER LOGIN`).

Queries that lead with a family word are rewritten before search (`lore Jaina` -> `jaina`, `class druid` -> `druid`);
the dropped words come back as `excluded_terms` with `normalization_hint: "excluded_family_hint_terms"`. The query as
typed is searched first, and when a page is titled with all of it (`class hall`, `zone scaling`) nothing is dropped.

Every candidate carries its full `ranking.match_reasons`. MediaWiki's own full-text order contributes at most 10
points and always appears as `upstream_rank_<n>`, so a row that matched only in a page body it never showed us cannot
outscore a real title match. A title is compared with the query word by word, ignoring case and punctuation, so a
disambiguation title's parenthetical counts as words: `xuen tactics` is an `exact_title` match for `Xuen (tactics)`.
Digit groups stay separate words, so `patch 1.12` is not an `exact_title` match for `Patch 1.1.2`. A
title that appears as a whole-word phrase inside a longer query earns `query_contains_title`, up to 30 points scaled
by the share of query words it spells out, so `world boss sha of anger` ranks `Sha of Anger` above pages whose
snippets only mention it. The upstream rank can still reorder two such partial titles. An exact title earns at least
60 title points, more than a partial title of the same content family can collect from its bonus, upstream rank and
snippet points combined, so `sha of anger anniversary` returns `Sha of Anger (Anniversary)` ahead of `Sha of Anger`
even when MediaWiki lists the base page first.

`resolve` reports `resolved: true` only when the top row carries a reason covering the whole query (`exact_title`,
`exact_api_title`, `exact_handler_title`, `exact_event_title`, `title_prefix`, `title_contains_query`,
`normalized_title_match`, `all_terms_match`, `guide_title_terms`, `expansion_alias_match`) and either no other row
covers the query, or the top row scores at least 70, or it leads the best other covering row by at least 18 points.
Upstream rank, family, intent and `query_contains_title` are not covering reasons, so a row that has only those is
never confident. For the 70 and 18-point checks, a top row's score loses 30 points when it carries
`query_contains_title`, so a title that is only part of the query is never confident because of that bonus. Two
covering rows can therefore both score high and still resolve confidently to the first. The top row's title must
also name the query word for word (as for `api`/`event`): `all_terms_match` also fires on the snippet, so a page
that only mentions every word (`Liquid guild us illidan` -> `Team Liquid`) is never confident. Confidence is judged on
every fetched row, and `--limit` only trims `candidates`, so `--limit 1` never hides a fetched rival. The
fetch is MediaWiki's top `max(25, 5 x --limit)` results, so a `--limit` above 5 reads more rows and can
find a rival further down.

The `api`/`event` search fallback adds an absolute floor on top of that: the candidate's own title has to spell the
query out. Every word of the query must match a whole word of the title or a whole camel-case component of one, and
the query must account for at least one title word end to end — case and separators are ignored on both sides, and
`UIHANDLER` counts as the two words it mashes together (`PLAYER_LOGIN` names `Event:PLAYER LOGIN`, `key down handler`
names `UIHANDLER OnKeyDown`). Letters that merely occur inside a longer name are not a match: `UnitHealth` does not
name `API UnitHealthMax`, and `is` does not name `API UnitIsPlayer`. `all_terms_match` also fires on MediaWiki's
snippet, so without the floor a page that merely mentions the query in its body — `UIHANDLER OnEvent` for
`PLAYER_LOGIN` — could be returned as the answer. Rows that fail the floor are reported under
`error.details.candidates` instead, and the command exits 4.

## Caching

HTTP responses are cached under the `WARCRAFT_WIKI_*` cache settings (see `doctor` output): search results for 1800s
(`WARCRAFT_WIKI_SEARCH_CACHE_TTL_SECONDS`) and parsed pages for 3600s (`WARCRAFT_WIKI_PAGE_CACHE_TTL_SECONDS`).

## Known limits

- Page extraction is heuristic, not template-aware; framework pages vary more than function pages.
- Structured data that lives only in templates or cargo tables is not extracted.
- The family classifier is a fixed list; pages that match none of the families fall back to `general_article`.
