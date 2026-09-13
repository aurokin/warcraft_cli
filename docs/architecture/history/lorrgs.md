# Lorrgs Provider Adoption (history)

Design record for why the Lorrgs provider exists. Current behavior lives in
[docs/lorrgs/README.md](../../lorrgs/README.md).

## Decision

**GO.**

Lorrgs has a public FastAPI JSON surface at `https://api2.lorrgs.io/api/*`, an OpenAPI document, and an
open-source implementation. It complements `warcraftlogs`: Warcraft Logs gives scoped report and ranking
primitives, while Lorrgs gives a ready cooldown-timeline view over top parses by spec/boss and a
composition-ranking view for encounters.

## Scope boundaries set at adoption time

- Read-only surface: queued load and dirty endpoints are not exposed.
- `user-report*` commands read already-cached Lorrgs records only; they never trigger the queued load flow.
- The CLI does not synthesize cooldown plans, "best timings", or normalized strategy answers. It returns
  Lorrgs' raw timeline data with provenance so agents can analyze it explicitly.
- Lorrgs data is an aggregation over top parses, not a universal recommendation. Fight duration,
  composition, externals, phase timing, and kill strategy can make top-parse timings unsuitable for a
  specific group.
- Retail-only: Lorrgs uses current Warcraft Logs-derived raid ranking data and exposes no classic/fresh
  selector, so the provider registers with `expansion_mode = "fixed"` and
  `supported_expansions = ["retail"]`.

## Source links

- `https://lorrgs.io/`
- `https://api2.lorrgs.io/api/openapi.json`
- `https://github.com/gitarrg/lorrgs`
