# Method

## Best For

- supported Method.gg guide/article families with simple article structure
- article fetch, export, and local query

## Start With

- discovery: `method search "<query>"`
- conservative match: `method resolve "<query>"`
- fetch: `method guide <slug>`
- deeper content: `method guide-full <slug>`
- local export/query: `method guide-export ...`, `method guide-query ...`

## Effective Use

- use Method when an article-style guide is easier to traverse than a Wowhead guide page
- prefer `guide` before `guide-full`
- expect explicit support boundaries; unsupported families return structured failures or `scope_hint`
- `build_references` holds explicit build evidence from the page: embedded Wowhead talent-calc links (`reference_type: wowhead_talent_calc_url`) and published WoW loadout import strings (`reference_type: wow_talent_export`, where `url` is the import string). There is no slug/title-based guide hardlinking
- current class guides publish import strings on their `/talents` section, so `guide-full` on a class guide is what feeds `guide-builds-simc`
- `wowhead_talent_calc_url` rows always decode unaided, because the URL path names the class and spec
- an import string names neither, so SimC identifies it by probing every spec it knows: builds of every role, healers included, decode without `--actor-class` or `--spec`
- additive `analysis_surfaces` highlight comparison-relevant guide topics without replacing raw guide content

## Validated Families

- class guides
- profession guides
- delve guides
- reputation guides
- article guides

## Boundaries

- not all Method.gg content is intentionally supported
- tier-list and index-style roots are intentionally excluded
- a page whose article container no longer matches fails with `parse_failed`; an empty article is never reported as success
- `guide-full` and `guide-export` skip a navigation page they cannot fetch or parse and list it in `data.failed_pages`
