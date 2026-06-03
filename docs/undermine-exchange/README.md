# Undermine Exchange CLI

## Decision

**DEFER.**

The [AUR-395](https://linear.app/aurokin/issue/AUR-395) gate is met, but Undermine Exchange does
not clear go/no-go on its own merits: at decision time the public site is under maintenance and
there is no confirmed stable public page or documented data endpoint to build against. Market data
is also highly time-sensitive, which makes cache behavior a real design cost we should not take on
speculatively. The market-data gap it would fill is genuine, but committing now would mean building
on an unstable surface.

### Un-gate condition

Revisit when **both** are true and recorded here:
1. the public Undermine Exchange surface is stable again (out of maintenance), and
2. a stable public page or documented data endpoint is confirmed for at least one market lookup
   (item or commodity pricing) plus one history/summary surface.

When those hold, the next revisit is itself AFK-able: flip this to GO, define the first command
slice (`doctor` + one item/commodity lookup + one history surface), and open a scaffold follow-up
issue mirroring [AUR-499](https://linear.app/aurokin/issue/AUR-499).

## Why It's Worth Revisiting

Undermine Exchange fills a market-data gap none of the current providers cover:
- auction house pricing
- commodity and item market history
- realm and region market context
- trade-good and profession-material discovery

It complements a future official Blizzard auction surface rather than replacing it: official
auction APIs are authoritative for raw data, while Undermine Exchange presents market-oriented
views and history that agents can reason over directly.

## Posture When Revisited

If un-gated, treat it as a cautious public-web-first market-data provider, following
[SAFE_ANALYTICS_RULES.md](../foundation/SAFE_ANALYTICS_RULES.md):
- prefer stable public pages or documented data endpoints; do not assume a stable public API until
  confirmed
- model realm, region, faction, commodity/item, and time-range explicitly
- preserve raw source identifiers/URLs; treat market-summary normalization as additive
- cache carefully because market data is time-sensitive

## Source Links

- `https://undermine.exchange/`
- [Roadmap](../ROADMAP.md)
