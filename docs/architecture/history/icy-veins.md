# Icy Veins provider: historical design notes

Archived from `docs/icy-veins/README.md`. This is the pre-implementation design record for the
Icy Veins provider. It is kept for context only; current behavior is documented in
[`docs/icy-veins/README.md`](../../icy-veins/README.md).

## Original goal

Make `icy-veins` a fully functioning, clearly scoped, and well-tested WoW guide/article CLI:
explicit family support, predictable discovery behavior, family-aware `guide-full` traversal,
strong live and recorded-fixture coverage, and docs that say exactly what is supported.

## Research summary

Icy Veins offers a much broader WoW surface than spec guide landing pages. Validated live page
families were class hub guides, role guides, main spec guides, easy mode pages, leveling guides,
spec subpages (builds, rotation, stat priority, gems, gear, spell summary, resources, Mythic+ tips,
macros/addons, simulations), raid-specific spec guides, and expansion/special-mode guides.

Observations from the live WoW sitemap:

- roughly 4,000+ WoW URLs exist under `/wow/`
- the site contains many old and special-purpose pages that still look guide-like
- sitemap filtering must remain WoW-specific and family-aware

The conclusion that drove the implementation: the parser model is broad enough to support more than
one Icy Veins guide family, so the real work was defining supported families and applying the right
traversal and ranking rules per family.

## Phased plan (all phases delivered)

1. **Correctness and scope** - family classification, family metadata on discovery and fetch
   payloads, heading dedupe, structured failures for bad refs, defined `guide-full` behavior per
   family.
2. **Discovery and traversal** - family-aware sitemap discovery, ranking boosts and penalties,
   conservative `resolve`, family-scoped traversal.
3. **Coverage and reliability** - dedicated live tests, recorded fixtures for representative
   supported and intentionally unsupported pages, regression tests for family-aware `guide-full`,
   ranking, and classification.
4. **Documentation and polish** - explicit supported families in the provider docs, wrapper
   discovery alignment, root `warcraft` skill guidance.

## Shared vs local code split

Shared: article bundle export/load/query, article discovery payload shaping, linked-entity merge,
cache, output, and transport infrastructure.

Local to `icy-veins`: sitemap family classification, family-aware ranking, family-aware traversal
rules, Icy Veins page parsing, and Icy Veins-specific invalid/unsupported surface rules.
