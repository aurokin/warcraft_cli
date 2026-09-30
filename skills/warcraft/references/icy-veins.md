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
- a spec query such as `frost mage` or `survival hunter` resolves to that spec's PvE guide; healer specs resolve to their healing guide; a spec name several classes share (`frost`, `holy`) stays unresolved, so name the class; a query that is a page's exact title (`player housing`) resolves to that page when its only close rivals are its own sub-pages
- every result carries `metadata.sitemap_lastmod`, the sitemap's date for the page (not the page's own `guide.last_updated`); a page a year behind the newest one is marked `penalty_stale_page` and ranked lower
- `resolve --limit` only trims the candidates shown; confidence is judged on every match
- for narrow subpage questions, search terms like `easy mode`, `rotation`, `stat priority`, or `mythic+ tips` work well
- `build_references` holds explicit build evidence from the page: embedded Wowhead talent-calc links (`reference_type: wowhead_talent_calc_url`) and published WoW loadout import strings (`reference_type: wow_talent_export`, where `url` is the import string). Guide slugs and titles are never treated as build evidence
- current spec builds/talents pages publish import strings, so `guide-full` on a spec guide is what feeds `guide-builds-simc`
- `wowhead_talent_calc_url` rows always decode unaided, because the URL path names the class and spec
- an import string names neither, so SimC identifies it by probing every spec it knows: builds of every role, healers included, decode without `--actor-class` or `--spec`
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
- a page whose article container no longer matches fails with `parse_failed`; an empty article is never reported as success. So do a sitemap that lists no guides and a spec page whose page switcher no longer parses in `guide-full`/`guide-export`
- discovery reads the Icy Veins sitemap, which has stopped being updated: when `provenance.sitemap_warning` is set, recent guides (the current season's raid and Mythic+ pages) are missing from `search` and `resolve`; open a known page directly with `icy-veins guide <slug-or-url>`, or use Method or Wowhead for current-season content
- `guide-full` and `guide-export` skip a family page they cannot fetch or parse and list it in `data.failed_pages`
- when Icy Veins serves another guide than the one asked for (a retired page redirects, e.g. `mistweaver-monk-legion-remix-guide` now serves the healing guide), `guide`, `guide-full` and `guide-export` set `data.redirect` to `{requested, served, message}` and everything else describes the served guide; check it before treating the content as the page you asked for. It is `null` otherwise
