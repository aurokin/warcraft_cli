# SimulationCraft CLI (`simc`)

`simc` is the local-tool provider in this repo. It reads a local [SimulationCraft](https://github.com/simulationcraft/simc)
checkout, decodes talent builds and APLs from it, and — when the binary is built — runs short sims and
parses their JSON reports. It never talks to a web API.

Design history lives in [../architecture/history/simc.md](../architecture/history/simc.md).

## Requirements

- a SimulationCraft source checkout for read-only analysis
- a built `simc` binary inside that checkout (`build/simc`) for `version`, `sim`, `run`, `decode-build`,
  `modify-build`, `validate-talent-transport`, and the comparison commands
- `rg` (ripgrep) on `PATH` for `find-action` and `trace-action`
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
| `--profile agent\|human\|debug` | Output presets: compact JSON, pretty JSON, pretty JSON plus diagnostics |

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

Raw-only transport packets are not accepted as direct build input: upgrade them with
`simc validate-talent-transport --build-packet <path> --out <path>` first. Malformed packets fail with
`invalid_build_packet` on every command that reads one.

Validation resolves every raw row against the local SimulationCraft trait data (class, spec, hero, and
the hero-tree selection node, which is reported under tree `selection` and named after the hero tree),
re-encodes the build through the SimC binary, and decodes it back. Two SimC decode behaviours are
accounted for and surfaced in `validation.round_trip`: tiered nodes (one node whose ranks are spread
over several entries) are compared by node presence and listed under `tiered_nodes`, and the keystone
SimC grants for the hero tree the build did not pick is listed under `ignored_granted_hero_entries`.
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

## Analysis boundary

The analysis commands stay inside what the local source tree and runtime samples prove. They summarize
structure, branches, and sampled timings, and they name the next command worth running. They do not
synthesize authoritative build advice beyond that evidence.

## Source links

- `https://github.com/simulationcraft/simc`
- [Design record](../architecture/history/simc.md)
- [Error and envelope contract](../foundation/ERROR_CONTRACT.md)
- [Roadmap](../ROADMAP.md)
