# Warcraft Wiki provider: design record

Historical context for the `warcraft-wiki` provider. Current behavior lives in
[`docs/warcraft-wiki/README.md`](../../warcraft-wiki/README.md); this file records why the provider exists and how it
was built out. It is not a backlog.

## Why the provider was added

Guide and ranking sites do not cover broad reference material: lore, systems documentation, addon/API documentation,
patch history, and general gameplay reference. Warcraft Wiki does, and it is especially strong for programming-oriented
workflows because it carries dedicated API and UI documentation (`API_CreateFrame`, `Widget_API`,
`Widget_script_handlers`, `UIHANDLER_OnKeyDown`, `XML_schema`) that no other planned service documents well.

## Research findings that shaped the access model

- The site is MediaWiki-based with stable page URLs; direct HTTP works without browser automation.
- The built-in search API is good enough for discovery, and the parse API returns section metadata alongside HTML.
- Content is heterogeneous: API pages, systems pages (`Expansion`, `Profession`, `Renown`, `Zone_scaling`, `Housing`),
  class/profession/faction/zone pages, patch pages, lore pages, and wiki-native guides all use the same source format
  but need different ranking and extraction treatment.

That last point produced the central decision: classify every page into a content family locally, and let ranking,
resolution, and extraction branch on the family instead of trying to treat the wiki as one uniform corpus.

## Build-out phases (all completed)

1. **Family classification** for both programming and non-programming families.
2. **Programming reference pass**: programming-aware ranking, cleaner extraction for API/event/framework pages, typed
   metadata, and linked-entity filtering that drops wiki chrome and edit-action links.
3. **Non-programming reference pass**: classification and ranking for systems, expansion, patch, profession, class,
   faction, zone, and lore families, plus family-hint query cleanup so `lore Jaina` resolves to the subject page.
4. **Family-aware tests** across API functions, UI handlers, API-changes pages, programming howtos, systems pages,
   expansion pages, class pages, profession pages, faction pages, lore pages, zone pages, and guide-style pages.
5. **Typed surfaces**: `api` / `api-full` and `event` / `event-full` for high-signal programming lookups, which refuse
   to return a page from the wrong family.

## What the provider validated for the shared layers

- The shared article bundle layer (export, load, query) works for reference material, not only class guides.
- The shared article discovery and follow-up layer supports an `article` surface in addition to `guide` surfaces.

## Deliberate service-specific scope

MediaWiki page parsing, title normalization, category/template handling, reference and infobox extraction, wiki-specific
search ranking, programming section extraction, and family classification stay in `warcraft_wiki_cli` and are not pushed
into `warcraft_content`.

## Known risks accepted at the time

- Wiki pages are far more heterogeneous than guide pages, so classification rules must stay conservative and
  test-backed.
- Useful structured data can live in templates or cargo metadata rather than the article body.
- The best query unit differs between lore, systems, and API pages.

## Source pages used during design

`Main_Page`, `Warcraft_Wiki:API`, `API_CreateFrame`, `Widget_script_handlers`, `Widget_API`, `XML_schema`,
`User_interface_customization_guide`, `UI_FAQ/AddOn_Author_Resources`, `Guides`, `Expansion`, `Profession`, `Renown`,
`Zone_scaling`, `Housing`, `Patch_2.2.0`.
