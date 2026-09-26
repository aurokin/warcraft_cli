# Product Principles

This is the canonical home for repo-wide product philosophy and representative workflow direction.

Documentation ownership:
- Put sequencing and current priority in [ROADMAP.md](../ROADMAP.md).
- Put provider-specific behavior, boundaries, and current gaps in `docs/<cli>/README.md`.
- Put analytics/comparison safety rules in [SAFE_ANALYTICS_RULES.md](SAFE_ANALYTICS_RULES.md).

## Product Principles

- These CLIs exist to give agents a comprehensive World of Warcraft toolset across guides, reference content, rankings, logs, and local simulation workflows.
- The repo should make complex WoW questions easier for agents by returning accurate, structured, well-formatted data with explicit provenance and boundaries.
- Prefer trustworthy building blocks over thin answer-only features:
  - normalization
  - sampling
  - aggregation
  - provenance
  - freshness
  - explicit scope and query metadata
- Normalization is additive. Raw source content should remain available so agents can still inspect the full guide, article, log slice, or profile detail when needed.
- If a provider cannot honestly support a workflow, the CLI should fail clearly or narrow the contract instead of faking coverage.
- Wrapper and provider surfaces should help agents compose cross-provider workflows without erasing source identity.
- Every binary speaks one JSON envelope, one error/exit-code vocabulary, and one set of global output flags ([ERROR_CONTRACT.md](ERROR_CONTRACT.md)), and every provider declares a support tier (core, supported, experimental) so an agent can tell the product from the experiments before trusting a surface.
- "Smart answers" are only acceptable when they stay grounded in what the provider contract, local source tree, or sampled data actually proves.

## Representative Workflows

Broad requests the repo should support cleanly:
- "Tell me about this class, quest, item, zone, or spec."
- "What changed for this spec?"
- "Show me the best guide or reference source for this topic."

Cross-provider requests the repo should make composable:
- compare guide recommendations from `wowhead`, `method`, and `icy-veins`
- compare guide-derived priorities against the local SimulationCraft APL for a specific build or talent export
- connect profile, ranking, guide, reference, and simulation data without making the agent hand-normalize every identifier

Deep analytics requests the repo should support through explicit building blocks:
- inspect one Warcraft Logs report, fight, or time window safely
- analyze casts, buffs, damage, and scoped event slices with stable actor/ability/window identity
- compare one player's usage, one aura window, or one target/add window against the cast timeline and other scoped slices
- aggregate across multiple reports only when sample boundaries, ranking basis, and truncation are explicit

The important product rule is not that every example above must already be a one-command workflow.
The important rule is that the repo should give agents the smallest trustworthy primitives needed to compute those workflows safely.
