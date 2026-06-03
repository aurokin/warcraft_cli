# Warcraft Logs payload keys

Canonical top-level envelope keys for typed `warcraftlogs` commands (v0.4.0).

During the v0.4.x minor release, legacy keys listed below are still dual-emitted at the
top level. Prefer the canonical key; `deprecated_keys` lists legacy aliases present.

| Command | Canonical key | Legacy primary (deprecated) |
| --- | --- | --- |
| `search` | `search` | `results` |
| `resolve` | `resolve` | `match` |
| `doctor` | `doctor` | — |
| `rate-limit` | `rate_limit` | — |
| `regions` | `regions` | — |
| `expansions` | `expansions` | — |
| `server` | `server` | — |
| `zones` | `zones` | — |
| `zone` | `zone` | — |
| `encounter` | `encounter` | — |
| `encounter-rankings` | `encounter_rankings` | `rankings` |
| `guild` | `guild` | — |
| `guild-rankings` | `guild_rankings` | — |
| `guild-members` | `guild_members` | — |
| `guild-attendance` | `guild_attendance` | — |
| `character` | `character` | — |
| `character-rankings` | `character_rankings` | — |
| `report` | `report` | — |
| `reports` | `reports` | — |
| `guild-reports` | `guild_reports` | `reports` |
| `boss-kills` | `boss_kills` | `kills` |
| `top-kills` | `top_kills` | `kills` |
| `spec-kill-samples` | `spec_kill_samples` | `kills` |
| `boss-spec-usage` | `boss_spec_usage` | `spec_usage` |
| `ability-usage-summary` | `ability_usage_summary` | `usage` |
| `comp-samples` | `comp_samples` | `kills` |
| `report-encounter` | `report_encounter` | — |
| `report-encounter-players` | `report_encounter_players` | `player_details` |
| `report-player-talents` | `report_player_talents` | `talent_transport_packet` |
| `report-encounter-casts` | `report_encounter_casts` | `casts` |
| `report-encounter-buffs` | `report_encounter_buffs` | `buffs` |
| `report-encounter-aura-summary` | `report_encounter_aura_summary` | `aura_summary` |
| `report-encounter-aura-compare` | `report_encounter_aura_compare` | `comparison` |
| `report-encounter-damage-source-summary` | `report_encounter_damage_source_summary` | `damage_summary` |
| `report-encounter-damage-target-summary` | `report_encounter_damage_target_summary` | `damage_summary` |
| `report-encounter-damage-breakdown` | `report_encounter_damage_breakdown` | `table` |
| `kill-time-distribution` | `kill_time_distribution` | `distribution` |
| `report-fights` | `report_fights` | — |
| `graphql` | `graphql` | `data` |
| `report-events` | `report_events` | `events` |
| `report-table` | `report_table` | `table` |
| `report-graph` | `report_graph` | `graph` |
| `report-master-data` | `report_master_data` | `master_data` |
| `report-player-details` | `report_player_details` | `player_details` |
| `report-rankings` | `report_rankings` | `rankings` |

See `packages/warcraftlogs-cli/src/warcraftlogs_cli/payload_envelope.py` for the source of truth.
