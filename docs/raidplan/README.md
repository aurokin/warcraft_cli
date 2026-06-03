# RaidPlan CLI

## Decision

**DEFER.**

The [AUR-395](https://linear.app/aurokin/issue/AUR-395) gate is met, but RaidPlan does not clear
go/no-go yet: it is unconfirmed whether the most valuable workflows (shared encounter plans and
their assignment data) are readable and exportable without authentication. The visual
assignment data may also be materially harder to normalize than the guide/wiki content our current
bundle/query model handles. Until read-only public-plan access is confirmed, committing risks
building a provider whose useful surface sits behind auth.

### Un-gate condition

Revisit when **both** are true and recorded here:
1. public shared plans are confirmed readable without auth via a stable plan URL, and
2. plan data is confirmed exportable (or otherwise extractable) without auth into a structured form
   that fits the existing bundle/query model.

When those hold, the next revisit is itself AFK-able: flip this to GO, define the first command
slice (`doctor` + fetch one public plan + export/query a local plan bundle), and open a scaffold
follow-up issue mirroring [AUR-499](https://linear.app/aurokin/issue/AUR-499).

## Why It's Worth Revisiting

RaidPlan covers a planning workflow the current providers do not:
- boss strategy planning
- mechanic assignments
- shareable encounter plans
- structured raid notes that agents can inspect or help generate

It complements guide and log providers: guides explain strategy, logs explain what happened, and
RaidPlan can represent what the group plans to do.

## Posture When Revisited

If un-gated, treat it as a read-only planning/workflow provider first, following
[SAFE_ANALYTICS_RULES.md](../foundation/SAFE_ANALYTICS_RULES.md):
- identify whether public shared plans are readable without auth before any write/edit work
- prefer stable public plan URLs and exported plan data; model raid, boss, difficulty, assignment
  groups, and notes explicitly
- preserve raw plan source identifiers/URLs; treat encounter/assignment normalization as additive
- treat creation/editing and any authenticated flow as a later phase, not the first milestone

## Source Links

- `https://raidplan.io/`
- [Roadmap](../ROADMAP.md)
