# Encounter Rankings (Completed)

Historical design record for splitting official Warcraft Logs encounter leaderboards from sampled boss analytics.

## Problem

Boss-level commands such as `boss-kills` and `top-kills` accepted `--spec-name` as participant filtering on sampled kills, not as spec-filtered parse rankings. Queries like “top Balance Druid parses on Mythic X” needed a different API surface.

## Solution (Shipped)

- Added `warcraftlogs encounter-rankings` using `Encounter.characterRankings(...)`
- Kept sampled commands; clarified in help and output that `--spec-name` on sampled analytics is participant filtering, not a spec leaderboard
- Documented routing in [../../warcraftlogs/README.md](../../warcraftlogs/README.md)

## Command Contract (Summary)

`encounter-rankings` exposes zone/boss, difficulty, class/spec, metric, pagination, partition, server, leaderboard, and related filters. Normalized output uses an `encounter_rankings` envelope with stable row summaries and preserved raw provider data.

## Rankings vs Sampled Analytics

| Surface | Use for |
|---------|---------|
| `encounter-rankings` | Official boss/class/spec leaderboards |
| `character-rankings` | Character-centric rankings |
| `boss-kills`, `top-kills`, `kill-time-distribution`, etc. | Sampled cross-report analytics (cohort metadata, citations) |

Do not reinterpret sampled commands as ranking leaderboards.

## Related

- [../../warcraftlogs/README.md](../../warcraftlogs/README.md) — current command reference
- [../../ROADMAP.md](../../ROADMAP.md) — ongoing Warcraft Logs work
