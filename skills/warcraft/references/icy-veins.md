# Icy Veins

## Best For

- spec guides and broad guide-family navigation
- class hubs, role guides, and spec subpages

## Start With

- discovery: `icy-veins search "<query>"`
- conservative match: `icy-veins resolve "<query>"`
- fetch: `icy-veins guide <slug>`
- deeper content: `icy-veins guide-full <slug>`
- local export/query: `icy-veins guide-export ...`, `icy-veins guide-query ...`

## Effective Use

- use Icy Veins when the caller needs structured guide families rather than a single direct page
- for broad class or role queries, let `resolve` pick the hub first
- for narrow subpage questions, search terms like `easy mode`, `rotation`, `stat priority`, or `mythic+ tips` work well
- `build_references` holds explicit build evidence from the page: embedded Wowhead talent-calc links (`reference_type: wowhead_talent_calc_url`) and published WoW loadout import strings (`reference_type: wow_talent_export`, where `url` is the import string). Guide slugs and titles are never treated as build evidence
- current spec builds/talents pages publish import strings, so `guide-full` on a spec guide is what feeds `guide-builds-simc`
- an import string does not name its class or spec, so `guide-builds-simc --decode` cannot decode those rows; decode one with `simc decode-build --talents <build_code> --actor-class <class> --spec <spec>`
- additive `analysis_surfaces` highlight comparison-relevant guide topics without replacing raw guide content

## Validated Families

- class hubs
- role guides
- spec guides
- easy mode
- leveling
- PvP
- builds/talents
- rotation
- stat priority
- gems/enchants/consumables
- spell summary
- resources
- macros/addons
- Mythic+ tips
- simulations
- raid guides
- expansion guides
- special-event guides

## Boundaries

- patch notes, hotfixes, and news-like queries return `scope_hint` rather than guide results
- a page whose article container no longer matches fails with `parse_failed`; an empty article is never reported as success
- `guide-full` and `guide-export` skip a family page they cannot fetch or parse and list it in `data.failed_pages`
