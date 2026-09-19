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
- use `reference` metadata on article responses instead of parsing the full body first

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
