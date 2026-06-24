# Expansion Filtering

Reference for wrapper `--expansion` behavior. Remaining policy review is tracked in Linear [AUR-369](https://linear.app/aurokin/issue/AUR-369/warcraft-finish-expansion-filtering-phase-3-4).

## Purpose

When a caller requests a specific game version, the wrapper must not silently mix in providers that do not support that expansion. Expansion ambiguity is a trust problem, not only a ranking problem.

## Provider Modes

| Mode | Meaning |
|------|---------|
| `profiled` | Provider switches behavior by expansion (e.g. `wowhead`) |
| `fixed` | Known scope, usually `retail`; excluded for other expansions |
| `none` | No reliable expansion semantics; excluded from filtered search/resolve |

## Provider Matrix (Current)

| Provider | Mode | Supported expansions | Notes |
| --- | --- | --- | --- |
| `wowhead` | `profiled` | provider profiles | Real expansion routing |
| `method` | `fixed` | `retail` | |
| `icy-veins` | `fixed` | `retail` | |
| `raiderio` | `fixed` | `retail` | |
| `wowprogress` | `fixed` | `retail` | |
| `warcraft-wiki` | `fixed` | `retail` | Reference content; wrapper excludes non-retail until classic routing exists |
| `simc` | `none` | — | Local analysis versioning differs; proxy relaxes to passthrough (see Phase 4) |
| `warcraftlogs` | `profiled` | `retail`, classic family, `fresh` | Site-profile routing: retail -> `www`, classic-family -> `classic`, `fresh` -> `fresh` |
| `raidbots` | `fixed` | `retail` | Report consumption; retail SimC runs |
| `blizzard-api` | `none` | — | Region/namespace routing deferred; proxy relaxes to passthrough (see Phase 4) |

## Wrapper Behavior (Shipped)

- `warcraft --expansion <key> search|resolve|doctor` filters providers by mode
- Responses include requested expansion, included/excluded providers, and exclusion reasons
- `--expansion-debug` surfaces filtering decisions in compact/debug output
- `doctor` reports per-provider expansion mode and support
- `warcraft --expansion <key> <none-provider> ...` (e.g. `simc`, `blizzard-api`) relaxes to passthrough with an advisory note instead of erroring — see the Phase 4 none-expansion passthrough rule

Without `--expansion`, cross-provider fanout behaves as before.

## Exclusion Reasons

- `provider_fixed_to_other_expansion`
- `provider_has_no_expansion_support`
- `provider_does_not_support_requested_expansion`

## Phase 4 Expansion Policy

### (i) Provider promotion criteria (`none`/`fixed` → `profiled`)

A provider is promoted toward `profiled` only when all of the following hold; until then it stays `none` or `fixed`:

- **Source distinguishes expansions.** The provider's data source exposes a stable, first-class way to select an expansion (distinct sites/namespaces/endpoints or a documented version parameter) — not a heuristic inferred from titles or timestamps.
- **Wrapper vocabulary maps cleanly.** Each wrapper expansion key the provider will accept maps to exactly one source scope (see the mapping table below). Keys without a clean mapping are rejected, not coerced.
- **Routing is testable offline.** The expansion → source-scope decision is covered by fixture-backed contract tests, with a `live`-gated check per supported scope.
- **No provenance loss.** Promotion is additive: payloads still carry the raw source ids/URLs for the selected scope (per `SAFE_ANALYTICS_RULES.md`).

`none` → `fixed` is the intermediate step: declare a single honest supported expansion (usually `retail`) once the provider's scope is confirmed, before any multi-expansion (`profiled`) routing is attempted.

### (ii) Wrapper-key -> Warcraft Logs site-profile mapping

`warcraftlogs` is expansion-profiled through a site-profile selector. The wrapper maps supported expansion keys to `warcraftlogs --site <profile>` before invoking the provider.

| Wrapper key | WCL site profile | Host |
| --- | --- | --- |
| `retail` | retail | `www.warcraftlogs.com` |
| `classic` | classic | `classic.warcraftlogs.com` |
| `tbc` | classic (TBC) | `classic.warcraftlogs.com` |
| `wotlk` | classic (WotLK) | `classic.warcraftlogs.com` |
| `cata` | classic (Cataclysm) | `classic.warcraftlogs.com` |
| `mop-classic` | classic (MoP) | `classic.warcraftlogs.com` |
| `fresh` | fresh (Classic Fresh / Anniversary) | `fresh.warcraftlogs.com` |
| `ptr` / `beta` / `classic-ptr` | — (unmapped) | rejected, not coerced |

### (iii) none-expansion passthrough rule

A provider whose mode is `none` has no expansion semantics to honor. For the `warcraft <provider> ...` proxy path, a wrapper `--expansion <key>` is therefore **relaxed to passthrough with an advisory note** rather than rejected:

- The provider command runs unchanged (the `--expansion` flag is not injected).
- The provider payload is preserved verbatim and annotated with an additive advisory: `expansion_filter: "passthrough_no_expansion_semantics"` plus an `expansion_advisory` block (`requested_expansion`, `provider_expansion_mode: "none"`, human note).
- Exit code follows the provider (e.g. `warcraft --expansion wotlk simc version` and `warcraft --expansion wotlk blizzard doctor` both succeed).

This applies only to `none` providers. `fixed`/`profiled` providers asked for an unsupported expansion are a genuine mismatch and still hard-fail with `unsupported_provider_expansion` (exit 1). The relaxed behavior is scoped to the proxy path; `warcraft --expansion <key> search|resolve` continues to exclude `none`/non-matching providers from the filtered fanout (surface/expansion filtering is unchanged).

## Non-Goals

- No fake universal expansion model across all providers
- No silent coercion of unsupported providers to `retail`
- No weakening the Wowhead expansion contract

## Open Work

- Promote `simc` when upstream exposes a real non-retail game-version surface
- Optional: per-expansion wiki routing when classic/fresh article policy is defined

## Related

- [../foundation/WRAPPER_PROVIDER_CONTRACT.md](../foundation/WRAPPER_PROVIDER_CONTRACT.md)
- [../wowhead/EXPANSION_RESEARCH.md](../wowhead/EXPANSION_RESEARCH.md)
- [../ROADMAP.md](../ROADMAP.md)
