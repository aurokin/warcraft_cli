# Safe Analytics Rules

This is the canonical home for repo-wide analytics and comparison design rules.

These rules apply across providers when the repo returns derived, compared, sampled, or normalized outputs.

## Core Rules

- Prefer explicit-scope primitives first.
  - Require one report, one fight, one ability, one target/source filter, or one explicit window when broader semantics would be misleading.
- Do not infer hidden segmentation from timestamps alone.
  - Wave inference, phase inference, and broad pull matching are out of scope unless the source exposes a trustworthy contract or the command labels the inference explicitly.
- Prefer provider-native stable groupings before derived pairwise semantics.
  - Ship source-native or target-native views before inventing more opinionated `source x target` comparison layers.
- Every comparison surface should publish its matching rule explicitly.
  - Examples: exact normalized section title, same report and same fight and same ability and explicit windows.
- Narrow, composable primitives beat broad, weakly grounded synthesis.
  - If a broad feature risks misleading agents, ship the narrower building block first and let agents compose from there.

## Evidence And Provenance

- Preserve raw source detail alongside any additive analysis layer.
- Keep source ids, source URLs, bundle paths, report codes, fight ids, and other provenance visible in derived outputs.
- If an output is sampled, truncated, filtered, merged, or derived, say so explicitly in the payload.
- Sample-backed analytics should expose:
  - ranking basis
  - sample counts
  - exclusion or truncation counts when relevant
  - freshness
  - citations
- Comparison outputs should remain evidence-oriented rather than recommendation-oriented unless the source contract actually proves the recommendation.

## Normalization Boundaries

- Normalize explicit references and stable structure first.
  - Embedded build refs, stable ids, exact section titles, actor ids within local scope, and explicit ability ids are good normalization targets.
- Do not normalize interpretive prose into canonical gameplay claims unless a strict contract exists.
  - Guide prose, recommendation tone, and loosely phrased priorities should remain raw evidence or additive tags, not fake universal truth.
- Preserve provider-native ids and payloads even when a normalized layer exists.

## Cache And Artifact Rules

- Reuse transport and provider caches freely when the source contract allows it.
- Treat exported bundles, orchestration roots, and derived investigation packets as evidence artifacts, not invisible cache entries.
- Evidence artifacts must have explicit freshness and inspectable provenance.
- Do not hide live-vs-finished differences behind one shared cache path.

## Operational Boundaries

For rate limiting, User-Agent posture, logging redaction, and the failure-mode playbook when endpoints or parsers break, see [OPERATIONAL_BOUNDARIES.md](OPERATIONAL_BOUNDARIES.md).

## Escalation Rule

- Stop generalizing when the next step would require provider-interpretive semantics instead of source-backed semantics.
- At that point:
  - return a narrower primitive
  - return `unknown` or `ambiguous`
  - or defer the broader workflow until a stricter contract exists
