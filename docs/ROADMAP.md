# Roadmap

What the repo treats as core, what is supported, what is experimental, and what is next.

- Open engineering work lives in [Linear — Warcraft CLI](https://linear.app/aurokin/project/warcraft-cli-a9a133da0d88), not here.
- Shipped work lives in the versioned sections of [../CHANGELOG.md](../CHANGELOG.md), not here.
- Command behavior lives in `docs/<cli>/README.md` and the generated [reference/](reference/README.md).
- Repo-wide rules live in [foundation/](README.md#foundations) and [architecture/](architecture/README.md).

Tiers are declared in the wrapper registry (`packages/warcraft-cli/src/warcraft_cli/providers.py`) and reported by `warcraft doctor` as `wrapper.tiers`, so this page and the CLI cannot drift.

## Core

The product. These carry the deepest command surface, the most test coverage, and the strongest contracts.

| Provider | Scope |
|----------|-------|
| `wowhead` | entities, guides, comments, talent calc, bundles, expansion profiles |
| `warcraftlogs` | official API: world metadata, guilds, characters, reports, scoped encounter analytics |
| `simc` | local SimulationCraft repo inspection, build decoding, APL analysis, runs |
| `warcraft` (wrapper) | routing, discovery, and cross-provider composition over the provider surfaces |

## Supported

Real providers with narrower surfaces. They are expected to work and stay covered by tests.

| Provider | Scope |
|----------|-------|
| `raiderio` | character/guild profiles plus sample-backed Mythic+ analytics |
| `wowprogress` | guild rankings, history, and sample-backed leaderboard analytics |
| `warcraft-wiki` | MediaWiki reference, typed API/event lookups, article bundles |
| `icy-veins` | guide extraction, export, and local guide query |
| `method` | guide extraction, export, and local guide query |

## Experimental

Thin or unproven surfaces. Do not build a workflow on them without checking `doctor` first.

| Provider | Status |
|----------|--------|
| `lorrgs` | public API, no auth; top-parse cooldown timelines and composition rankings |
| `raidbots` | public report consumption and SimC input handoff; no discovery surface |
| `blizzard` | endpoints, OAuth URLs, and namespaces never confirmed live: every payload carries `provenance.verified: false` |
| `curseforge` | host, endpoints, and response shapes never confirmed live: every payload carries `provenance.verified: false` |

## Next

- Ship the wheel install path end to end: attach the built wheel to each GitHub release and verify `pipx install <wheel-url>` and `uvx --from <wheel-url> warcraft doctor` on a clean machine.
- Run the gated Blizzard and CurseForge live suites once, then either flip `provenance.verified` and promote them, or delete the provider.
- Retire the deprecated dual-emitted top-level payload keys (agents read `data`); that is a major-version change, so it needs a deprecation window first.
- Finish the expansion story for the deferred surfaces: Warcraft Logs classic/fresh cache isolation and `simc` expansion semantics.
- Decompose the remaining radon D-or-worse blocks so `complexity-gate` stays green in `make check`.

## Deferred Candidates

Both were decided under [AUR-395](https://linear.app/aurokin/issue/AUR-395) and remain **DEFER**. Nothing is built; the un-gate conditions below are the whole decision.

**RaidPlan** (`https://raidplan.io/`) — would cover boss strategy planning, mechanic assignments, and shareable encounter plans, which no current provider covers. Deferred because it is unconfirmed whether the valuable workflows (shared plans and their assignment data) are readable without authentication, and the visual assignment data may be materially harder to normalize than guide/wiki content.

Un-gate when **both** are true and recorded here:
1. public shared plans are confirmed readable without auth via a stable plan URL, and
2. plan data is confirmed exportable or extractable without auth into a structured form that fits the existing bundle/query model.

First slice if un-gated: `doctor` + fetch one public plan + export/query a local plan bundle, read-only, following [foundation/SAFE_ANALYTICS_RULES.md](foundation/SAFE_ANALYTICS_RULES.md).

**Undermine Exchange** (`https://undermine.exchange/`) — would cover auction pricing, commodity/item market history, and trade-good discovery, a genuine gap. Deferred because the public site was under maintenance at decision time with no confirmed stable page or documented data endpoint, and market data is time-sensitive enough that cache design is a real cost to take on speculatively.

Un-gate when **both** are true and recorded here:
1. the public surface is stable again (out of maintenance), and
2. a stable public page or documented data endpoint is confirmed for at least one market lookup (item or commodity pricing) plus one history/summary surface.

First slice if un-gated: `doctor` + one item/commodity lookup + one history surface, preserving raw source identifiers and treating market-summary normalization as additive.

## Risks

- over-generalizing too early
- hiding source differences behind fake shared schemas
- pushing too much logic into the root wrapper
- adding broad analytics semantics before the source contract is strong enough
- documentation drift — now partly guarded: `tests/test_command_reference.py` fails when `docs/reference/` is stale and `tests/test_docs_parity.py` fails when a documented command or flag does not exist

## Rules

- Keep tiers, what is next, and deferred candidates here; keep open tasks in Linear; keep shipped work in the changelog.
- Only extract shared code after a second provider proves the abstraction is real.
- Prefer depth, reliability, and trust metadata in the core tier over adding another provider.
- A provider that cannot honestly support a workflow fails clearly instead of faking coverage.
