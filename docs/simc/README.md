# SimulationCraft CLI (`simc`)

`simc` is the local-tool provider in this repo. It reads a local [SimulationCraft](https://github.com/simulationcraft/simc)
checkout, decodes talent builds and APLs from it, and — when the binary is built — runs short sims and
parses their JSON reports. It never talks to a web API.

Design history lives in [../architecture/history/simc.md](../architecture/history/simc.md).

## Requirements

- a SimulationCraft source checkout for read-only analysis
- a built `simc` binary inside that checkout (`build/simc`) for `version`, `sim`, `run`, `decode-build`,
  `modify-build`, `validate-talent-transport`, and the comparison commands
- `rg` (ripgrep) on `PATH` for `spec-files`, `find-action`, and `trace-action`. Without it those three
  commands fail with `missing_dependency` and `simc doctor` marks them `unavailable`.
- `git` and `cmake` for `sync`, `checkout`, and `build`

`simc doctor` reports which of these are present.

## Repo resolution

The checkout is resolved in this order:

1. `--repo-root` on the command line
2. the `SIMC_REPO_ROOT` environment variable
3. the explicit root saved with `simc repo --set-root <path>`
4. the managed checkout under the provider data root, created by `simc checkout`

`simc repo` prints the resolution, `simc doctor` includes it under `repo_resolution`.

## Global flags

Global flags go before the subcommand.

| Flag | Effect |
|------|--------|
| `--repo-root PATH` | Override the local SimulationCraft checkout for this invocation |
| `--pretty` | Pretty-print JSON. Default output is compact JSON |
| `--compact` | Truncate long string fields |
| `--compact-max-chars N` | Truncation length for `--compact` (40-10000) |
| `--fields a.b,c` | Keep only the listed dot paths (repeatable or comma-separated) |
| `--fields-strict` | Exit 2 with `missing_fields` when a requested path is absent |
| `--profile agent\|human` | Output presets: compact JSON, pretty JSON |

```bash
simc --repo-root ~/src/simc --pretty doctor
simc --fields data.capabilities doctor
```

## Output contract

Every command writes one JSON envelope: `ok`, `provider`, `command`, `kind`, `schema_version`, `query`,
`provenance`, `data`, and `error` on failure. The payload lives in `data`; the same keys are still
mirrored at the top level for existing agents and are deprecated. Failures go to stderr and use the
shared exit codes (1 generic, 2 usage, 3 auth, 4 not found, 5 network/upstream) — see
[../foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

`simc` has no network surface, so exit 3 and exit 5 do not occur. A missing checkout, a missing binary,
or a failed SimC run is a structured error, never a traceback.

These codes are worth knowing:

- `invalid_build` (exit 1) — SimC rejected the talent input. `error.message` is SimC's own error line
  and `error.details` carries `simc_returncode`, a 20-line `simc_output_preview` (each line clipped to
  200 characters), and `simc_binary` with the binary's build revision, the checkout HEAD, and
  `matches_checkout`. A rejected hash is never reported as a partial decode. When the binary is older
  than its checkout the message says so and names the rebuild command, because a stale binary decodes
  against older trait data; `simc doctor` reports the same mismatch.
- `missing_dependency` (exit 1) — ripgrep is not installed.
- `not_found` (exit 4) — `spec-files`, `find-action`, and `trace-action` were pointed at a directory that
  is not a SimulationCraft checkout. They report this instead of returning zero hits as a success.
- `invalid_query` (exit 2) — a build arrived without a class and spec and could not be identified, or a
  build-input option was passed with an empty value. Identification decodes the build once per candidate
  spec, and the candidates are exactly the specs the checkout ships an APL for (34 today, healer specs
  mostly among the ones it does not); `error.details.probed_specs` lists them. Pass `--actor-class` and
  `--spec` for anything outside that list.
- `unsupported_build_reference` (exit 2) — the build input is a link the CLI cannot turn into talents.
  `error.details.reference_type` names what it recognized: `wowhead_talent_calc_url` for a talent-calc
  URL with no build code, `url` for anything else. See "Build references" below for what does decode.
- `unknown_talent` (exit 2) — an `--enable`/`--disable` value, or a `modify-build` `--add`/`--remove`
  value, names no talent of the actor's class. `error.details.unknown_talents` lists them.

## Build input flags

Commands that act on an exact build accept the same build-input group. Pass whichever form you have;
the CLI reports which one it used in `source_kind` and `identity`.

| Flag | Input |
|------|-------|
| `--apl-path PATH` | Infer actor class and spec from an APL file |
| `--profile-path PATH` | A SimC profile that contains build lines |
| `--build-file PATH` | A plain text file with `talents=` / spec lines |
| `--build-packet PATH` | A talent transport packet JSON file |
| `--build-text TEXT` | Inline build text, talent hash, or Wowhead talent-calc URL with a build code |
| `--talents TEXT` | WoW export string, Wowhead talent-calc URL with build code, or `talents=...` line |
| `--class-talents` / `--spec-talents` / `--hero-talents` | Split SimC talent strings |
| `--actor-class` / `--spec` | Explicit class and spec, e.g. `monk` and `mistweaver` |

Analysis commands additionally take `--enable NAME` and `--disable NAME` (repeatable or comma-separated)
to force talents on or off on top of the resolved build.

Passing a build-input option with an empty value is a usage error, not the same as omitting it.

Raw-only transport packets are not accepted as direct build input: upgrade them with
`simc validate-talent-transport --build-packet <path> --out <path>` first. Malformed packets fail with
`invalid_build_packet` on every command that reads one.

## Build references

`--build-text` and `--talents` accept these reference types. Anything else fails with
`unsupported_build_reference` rather than reaching SimC as if it were a talent hash.

| Reference type | Example | Decodes |
|----------------|---------|---------|
| `wow_talent_export` | `C4QAAAAAA...` | Yes, once the class and spec are known. Both Method and Icy Veins publish only this type, and the string names no class or spec, so either pass `--actor-class`/`--spec` or let identification probe the specs the checkout ships an APL for. |
| `wowhead_talent_calc_url` | `https://www.wowhead.com/talent-calc/monk/mistweaver/<code>` | Yes, unaided: the path names the class and spec. |
| Wowhead `/talent-calc/blizzard/<code>` | what `modify-build` publishes as `result.wowhead_url` | Yes, as a `wow_talent_export`: the URL carries the hash but no class or spec. |
| `wowhead_talent_calc_url` with no build code | `https://www.wowhead.com/talent-calc/monk/mistweaver` | No — `unsupported_build_reference`. |
| Any other link (guide page, article, addon export site) | `https://www.icy-veins.com/wow/...` | No — `unsupported_build_reference` with `reference_type: "url"`. |

## Decoded builds

`decode-build` and `describe-build` report what SimC actually gave the player, not every line it printed:

- `hero_tree` names the hero tree SimC activated (`activating sub tree` in its debug output). A talent
  hash grants the keystones of both hero trees and SimC then disables the unselected one, so those
  talents are moved to `inactive_hero_talents` and are absent from `enabled_talents`. Keeping them there
  flips APL branches that dispatch on a hero keystone.
- A tiered node (one node whose ranks are spread over several entries) is reported as one row per entry,
  each with its own rank. SimC's decode prints a single line per tiered node holding the leftover rank,
  always `0`, so the CLI runs the build a second time with those entries set to rank `0` and reads the
  ranks back out of SimC's own overwrite log. Without those ranks the node cannot be re-serialized, and
  every `modify-build` tree swap dropped it.
- Talent rows still carry `rank_known`. It is `false`, with `rank: null`, only when the read-back found
  nothing — for example when the checkout's trait data predates the node. Such a row still counts as
  enabled, and re-serializing it (a tree swap) will fail with `encode_mismatch` rather than lose it.

## Editing a build

`modify-build` routes each `--add`/`--remove` into the tree that owns the talent (SimC resolves talent
names per tree, so a spec talent passed as a class talent is rejected). A name must belong to the actor's
class; an entry id is resolved against the checkout's trait data. Unresolvable values fail with
`unknown_talent`.

After re-encoding, the result is decoded again and compared per tree with the build it was supposed to
come from: the base build, or the `--swap-*-tree-from` source for a tree that was swapped. If anything
changed in the active trees that was not asked for, the command fails with `encode_mismatch` and
`details.unrequested_changes` instead of emitting an export. On success the payload carries
`result.verified: true`.

A tree swap drops the base hash and rebuilds every tree from `entry:rank` pairs. That is lossless for a
tiered node whose per-entry ranks were read back (see "Decoded builds" above); when they were not
(`rank_known: false`), the swap fails with `encode_mismatch` naming the talent that would have been
lost rather than emitting an export without it. `--add`/`--remove` keep the base hash and are
unaffected.

`result.diff_from_base` has a fourth key, `inactive_hero`. SimC regenerates the talent hash whenever it
is handed a split talent string, and its serializer freely grants the keystone of *every* hero tree, so
the export can carry a keystone the input hash did not. Those talents are inert (the sim never activates
that tree) but the export string really does differ, so they are listed under `inactive_hero` and
`result.disclosures` explains why. An empty `disclosures` means the export matches the base build
exactly.

Validation resolves every raw row against the local SimulationCraft trait data (class, spec, hero, and
the hero-tree selection node, which is reported under tree `selection` and named after the hero tree),
re-encodes the build through the SimC binary, and decodes it back. Two SimC decode behaviours are
accounted for and surfaced in `validation.round_trip`: tiered nodes (one node whose ranks are spread
over several entries) are listed under `tiered_nodes` with `compared_by: "node_presence"` and
`per_entry_ranks_verified: false`, because SimC prints only the node's leftover rank; and the keystones
SimC grants for the hero tree the build did not pick are listed under `ignored_unselected_hero_entries`.
A packet stays `raw_only` with `simc_trait_resolution_incomplete` when the local checkout predates a
talent, or `simc_round_trip_mismatch` with `expected_entries_by_tree` / `actual_entries_by_tree` when
the decoded build differs.

## Commands

| Command | Arguments | Flags | What it returns |
|---------|-----------|-------|-----------------|
| `analysis-packet` | APL_PATH | `--targets`, `--list`, `--intent-limit`, `--explain-limit`, `--runtime-scan-limit`, `--sim-profile`, `--first-cast-action`, `--seeds`, `--max-time`, `--fight-style`; build input (no `--apl-path`, `--build-packet`); `--enable`, `--disable` | Bundle branch, intent, and optional first-cast timing analysis into one payload. |
| `apl-branch-compare` | APL_PATH | `--left-targets`, `--right-targets`, `--list`, `--right-profile-path`, `--right-build-file`, `--right-build-text`, `--right-talents`, `--right-class-talents`, `--right-spec-talents`, `--right-hero-talents`, `--right-actor-class`, `--right-spec`, `--right-enable`, `--right-disable`; build input (no `--apl-path`, `--build-packet`); `--enable`, `--disable` | Compare branch dispatch between two builds or target counts on one APL. |
| `apl-branch-trace` | APL_PATH | `--targets`, `--list`, `--max-depth`; build input (no `--apl-path`, `--build-packet`); `--enable`, `--disable` | Trace action-list dispatch for an exact build from a starting list. |
| `apl-graph` | APL_PATH | - | Render the action-list call graph of an APL file as Mermaid text. |
| `apl-intent` | APL_PATH | `--targets`, `--list`, `--limit`; build input (no `--apl-path`, `--build-packet`); `--enable`, `--disable` | Summarize what the focus action list is trying to do for an exact build. |
| `apl-intent-explain` | APL_PATH | `--targets`, `--list`, `--limit`; build input (no `--apl-path`, `--build-packet`); `--enable`, `--disable` | Explain the focus list as setup, helper, burst, and priority buckets. |
| `apl-lists` | APL_PATH | `--list` | List the action lists in an APL file with their entries. |
| `apl-prune` | APL_PATH | `--targets`, `--list`, `--show`; build input (no `--apl-path`, `--build-packet`); `--enable`, `--disable` | Classify APL entries as eligible, dead, or unknown for an exact build. |
| `apl-talents` | APL_PATH | - | List the talents an APL file references and the most common actions. |
| `build` | - | `--target` | Build the local SimulationCraft binary with cmake. |
| `build-harness` | - | `--out`, `--line`; build input (no `--build-packet`) | Write a harness profile for the resolved build with no APL actions. |
| `checkout` | - | - | Clone or update the managed SimulationCraft checkout. |
| `compare-apls` | HARNESS_PATH | `--base-apl`, `--base-label`, `--variant`, `--iterations`, `--threads`, `--out-dir`, `--validate-first/--skip-validate`, `--report-out` | Sim a base APL against labelled variants and rank them by DPS. |
| `compare-builds` | - | `--base`, `--other`, `--tree`; `--actor-class`, `--spec` | Diff a base talent build against one or more other builds, per tree. |
| `decode-build` | - | build input | Decode a talent build into per-tree talents using the local SimC binary. |
| `describe-build` | - | `--targets`, `--aoe-targets`, `--list`, `--priority-limit`, `--inactive-limit`; build input; `--enable`, `--disable` | Describe a build end to end: talents, priority, and single-target versus AoE differences. |
| `doctor` | - | - | Report SimulationCraft repo readiness, binary version, and per-command capabilities. |
| `find-action` | ACTION | `--class`, `--limit` | Find an action, buff, or token across APLs, class modules, and spell dumps. |
| `first-cast` | PROFILE_PATH ACTION | `--seeds`, `--max-time`, `--targets`, `--fight-style` | Time the first cast of an action across several short sims. |
| `identify-build` | - | build input | Resolve class/spec identity for a build without decoding its talents. |
| `inactive-actions` | APL_PATH | `--targets`, `--list`, `--limit`, `--talent-only/--all-dead`; build input (no `--apl-path`, `--build-packet`); `--enable`, `--disable` | List the APL actions an exact build cannot use. |
| `inspect` | [TARGET] | - | Describe the repo, or one file inside it, including any build lines it carries. |
| `log-actions` | LOG_PATH ACTIONS | - | Report when actions were first scheduled and performed in a SimC combat log. |
| `modify-build` | - | `--swap-class-tree-from`, `--swap-spec-tree-from`, `--swap-hero-tree-from`, `--add`, `--remove`; `--talents`, `--actor-class`, `--spec` | Apply talent swaps, additions, and removals to a build and re-encode it. |
| `opener` | APL_PATH | `--targets`, `--list`, `--limit`; build input (no `--apl-path`, `--build-packet`); `--enable`, `--disable` | Preview the early priority for an exact build, flagging runtime-only conditions. |
| `priority` | APL_PATH | `--targets`, `--list`, `--limit`; build input (no `--apl-path`, `--build-packet`); `--enable`, `--disable` | Return the static active priority for an exact build, excluding inactive talent branches. |
| `repo` | - | `--set-root`, `--clear-root` | Show or change which local SimulationCraft checkout the CLI uses. |
| `resolve` | QUERY | `--limit` | Return the structured coming-soon stub for free-text resolution. |
| `run` | PROFILE_PATH | `--arg` | Run a profile through the local SimC binary with raw SimC arguments. |
| `search` | QUERY | `--limit` | Return the structured coming-soon stub for free-text search. |
| `sim` | [PROFILE_PATH] | `--preset`, `--iterations`, `--max-time`, `--fight-style`, `--threads`, `--targets`, `--vary-combat-length`, `--profile-text`, `--json-out` | Run a profile through the local SimC binary and summarize the JSON report. |
| `spec-files` | [QUERY] | `--limit` | List APL and class-module files in the checkout, optionally narrowed by a substring. |
| `sync` | - | `--allow-dirty` | Pull the latest SimulationCraft sources into the local checkout. |
| `trace-action` | APL_PATH ACTION | `--class`, `--limit` | Trace one action through an APL file and the surrounding source. |
| `validate-apl` | HARNESS_PATH APL_PATH | `--label`, `--out-dir` | Append an APL to a harness profile and check that SimC parses the result. |
| `validate-talent-transport` | - | `--talent-row`, `--out`; `--build-packet`, `--actor-class`, `--spec` | Round-trip raw talent rows through SimulationCraft and report the validated transport forms. |
| `variant-report` | REPORT_PATH | - | Summarize a saved compare-apls JSON report. |
| `verify-clean` | - | `--hash-binary` | Report whether the checkout and built binary are unmodified. |
| `version` | - | - | Report the version reported by the local SimC binary. |

`search` and `resolve` are structured `coming_soon` stubs that exit 0. They exist so the `warcraft`
wrapper can route uniformly; use the direct commands above for discovery.

Run `simc <command> --help` for per-flag detail including defaults and value ranges.

## Sim presets

`simc sim` is the preferred consumer run path. It uses fixed presets instead of leaving iteration
counts implicit, and always returns run settings, runtime timing, and core metrics:

- `--preset quick` (default): 1000 iterations
- `--preset high-accuracy`: 5000 iterations

Individual settings (`--iterations`, `--max-time`, `--threads`, `--targets`, `--fight-style`,
`--vary-combat-length`) override the preset. Default to `quick` for consumer work and only reach for
`high-accuracy` when the user asks for it. Do not hard-code thread counts in guidance; inspect the
machine first.

## Comparison workflow

Draft and compare APL variants without touching the upstream checkout:

```bash
simc build-harness --talents "<export>" --out ./tmp/harness.simc
simc validate-apl ./tmp/harness.simc ./tmp/variant.simc
simc compare-apls ./tmp/harness.simc --base-apl ./tmp/base.simc --variant "variant=./tmp/variant.simc" --report-out ./tmp/report.json
simc variant-report ./tmp/report.json
simc verify-clean --hash-binary
```

`dps`, `dps_error`, and `fight_length` are means over every iteration. `action_counts`, `action_cpm`,
and `top_action_deltas` are not: SimulationCraft records an action sequence for a single iteration, so
those describe one fight however many were simulated. The payload says so in `sampling` and repeats
`action_sequence_iterations: 1` on each summary and comparison. Treat a small CPM delta as noise.

## Tests that need the binary

`tests/test_simc_real_binary.py` drives the real SimC binary in the discovered checkout over its own
stock MID1 profiles. It skips — with `REAL-BINARY TEST SKIPPED` in the skip reason — when no built
binary is present, so it proves nothing on CI. The same logic is covered everywhere else by
`tests/test_simc_build_input.py` and `tests/test_simc_cli.py`, which replay captured SimC output.

## Analysis boundary

The analysis commands stay inside what the local source tree and runtime samples prove. They summarize
structure, branches, and sampled timings, and they name the next command worth running. They do not
synthesize authoritative build advice beyond that evidence.

## Source links

- `https://github.com/simulationcraft/simc`
- [Design record](../architecture/history/simc.md)
- [Error and envelope contract](../foundation/ERROR_CONTRACT.md)
- [Roadmap](../ROADMAP.md)
