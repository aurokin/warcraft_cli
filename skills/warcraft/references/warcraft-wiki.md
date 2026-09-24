# Warcraft Wiki

## Best For

- addon and API documentation
- UI events and framework pages
- systems, lore, and reference pages

## Start With

- programming lookup: `warcraft-wiki api <name>`
- game event lookup: `warcraft-wiki event PLAYER_LOGIN`
- general page: `warcraft-wiki article "<title>"`
- discovery: `warcraft-wiki search "<query>"`, `warcraft-wiki resolve "<query>"`

## Effective Use

- prefer `api` for function, framework, XML schema, CVar, and API-change pages
- prefer `event` for game events (`PLAYER_LOGIN`, `ENCOUNTER_START`) and UI handlers (`OnKeyDown`)
- `api` and `event` fail with `not_found` (exit 4) instead of returning an unrelated page
- use `article` when the query is broader than programming
- use `reference` metadata on article responses instead of parsing the full body first; on `api`/`event` pages `reference.arguments` holds the arguments (an event's "Payload"), and `reference.signature` is the introduction's code block or `null`
- `reference.summary` is the page's opening text, which can start with a "For the ..., see ..." hatnote or infobox text on lore and API pages; read `content.text` when the summary looks like navigation

## Strong Families

- API functions
- game events
- UI handlers
- framework pages
- XML schema
- API changes
- programming howtos
- systems pages
- expansion, class, profession, faction, lore, zone, and guide-style reference pages
