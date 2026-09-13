# Method.gg Provider History

Design record for the second provider CLI. Kept for provenance; the current behavior of `method`
lives in [docs/method/README.md](../../method/README.md).

## Why Method Was The Second CLI

Method was close enough to Wowhead's guide workflows to reuse the article and bundle
infrastructure, but different enough to prove that the shared layer was not secretly Wowhead-only.
Building it is what produced `warcraft_content.article_bundle`, `article_discovery`, and the shared
article search/resolve payload shaping.

## Original Research (v0.1 era, `https://www.method.gg/guides/mistweaver-monk`)

- direct HTML fetch works without browser automation for the sampled pages
- guide metadata, section navigation, patch, and update dates are in the server-rendered HTML
- section navigation is explicit in page links such as `/guides/mistweaver-monk/talents`
- the page also includes `application/ld+json`, which the parser does not currently use
- premium and login are not needed for the supported guide content

The access model that followed: fetch HTML, extract metadata / section nav / author and update
information / section bodies, and export local guide bundles for repeated querying.

## Sharing Decisions

Proven shared and now living in `warcraft-content` / `warcraft-api` / `warcraft-core`:

- article bundle export, load, and query
- article discovery payloads, follow-up guidance, and multi-page linked-entity merge
- cache backends and TTL handling, HTTP transport and retry, output shaping
- query normalization and article title scoring (`warcraft_content.search`)

Deliberately kept Method-specific:

- HTML selectors and parsing rules, guide slug resolution, section and nav extraction
- sitemap interpretation rules and supported-content-family decisions
- the content-family ranking boost

## Scope Decisions

The target was never "support every page on Method.gg". It was: make the supported guide families
explicit, keep search and resolve trustworthy inside that scope, fail clearly outside it, and prove
the contract with live and synthetic tests. Index-style roots (`tier-list`, `world-of-warcraft`) were
excluded because they use templates the guide parser cannot represent, and premium, news, account,
and personalization surfaces were excluded for lack of a data reason.

## Known Remaining Gaps

- discovery is sitemap-rooted; ranking understands content families only through a keyword boost
- parsing depends on a small set of selectors with limited fallbacks, so template drift is a real risk
- non-live coverage is parser- and contract-level; live coverage is a small stable page set
- more Method content families still need review before they can be marked supported
