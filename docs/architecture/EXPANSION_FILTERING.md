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
| `simc` | `none` | — | Local analysis versioning differs |
| `warcraftlogs` | retail-first | phase 1 `retail` | Classic/fresh site mapping deferred |

## Wrapper Behavior (Shipped)

- `warcraft --expansion <key> search|resolve|doctor` filters providers by mode
- Responses include requested expansion, included/excluded providers, and exclusion reasons
- `--expansion-debug` surfaces filtering decisions in compact/debug output
- `doctor` reports per-provider expansion mode and support

Without `--expansion`, cross-provider fanout behaves as before.

## Exclusion Reasons

- `provider_fixed_to_other_expansion`
- `provider_has_no_expansion_support`
- `provider_does_not_support_requested_expansion`

## Non-Goals

- No fake universal expansion model across all providers
- No silent coercion of unsupported providers to `retail`
- No weakening the Wowhead expansion contract

## Open Work

- Promote `simc` when wrapper search/resolve can version local analysis inputs safely
- Warcraft Logs classic/fresh site-profile mapping when wrapper vocabulary is ready
- Optional: per-expansion wiki routing when classic/fresh article policy is defined

## Related

- [../foundation/WRAPPER_PROVIDER_CONTRACT.md](../foundation/WRAPPER_PROVIDER_CONTRACT.md)
- [../wowhead/EXPANSION_RESEARCH.md](../wowhead/EXPANSION_RESEARCH.md)
- [../ROADMAP.md](../ROADMAP.md)
