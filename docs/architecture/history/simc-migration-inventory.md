# SimulationCraft Migration Inventory

> **Status: Completed.** All phases have been migrated into `packages/simc-cli/`. This document is preserved for rationale and migration history.

## Purpose

This document translates the original `simc_exp` exploration project into a concrete migration inventory for the `simc` provider.

It answers:
- what should move directly into `simc`
- what should be reshaped for the monorepo command surface
- what should stay out of phase 1

Use alongside:
- [SimulationCraft CLI doc](README.md)
- [Roadmap](../../ROADMAP.md)
- [Package layout](../PACKAGE_LAYOUT.md)

## Original Source

The original exploration code lived in a separate `simc_exp` project. All migrated code is in `packages/simc-cli/`.

Original source modules (for historical reference):
- `simc_exp/repo.py`
- `simc_exp/build_input.py`
- `simc_exp/apl.py`
- `simc_exp/search.py`
- `simc_exp/prune.py`
- `simc_exp/branch.py`
- `simc_exp/sim.py`
- `simc_exp/packet.py`
- `simc_exp/cli_support.py`
- `simc_exp/cli.py`

## Key Conclusion

`simc_exp` is not just a prototype for `simc run`.

It already proves three distinct layers:
1. local repo and binary discovery
2. readonly source-tree analysis
3. runtime escalation through short simulations and log inspection

That means the future `simc` provider should absorb the useful analysis surface, not only the repo/build/run plumbing.

## Move As-Is Or Nearly As-Is

These look structurally sound and map cleanly into `simc` with mostly packaging and naming changes.

### Repo discovery and validation

Source:
- `simc_exp/repo.py`

Use for:
- `simc doctor`
- repo-path resolution
- binary-path resolution

Expected changes:
- remove hardcoded default assumptions where needed
- move to XDG-aligned shared config/data rules

### APL parsing and grouping

Source:
- `simc_exp/apl.py`

Use for:
- `simc apl-lists`
- `simc apl-graph`
- `simc apl-talents`

Expected changes:
- package move
- output shaping through `warcraft-core`

### Search helpers for local source

Source:
- `simc_exp/search.py`

Use for:
- `simc spec-files`
- `simc find-action`
- `simc trace-action`

Expected changes:
- integrate with shared output/error shape
- tighten path handling and rg failure handling if needed

### Sim execution helpers

Source:
- `simc_exp/sim.py`

Use for:
- `simc first-cast`
- `simc log-actions`
- future runtime validation inside `analysis-packet`

Expected changes:
- route binary and temp-path handling through provider config
- make result storage fit the monorepo path rules

## Move But Reshape

These should migrate, but the command surface or internal boundaries should be cleaned up as they move.

### Build input decoding

Source:
- `simc_exp/build_input.py`

Why it should move:
- central to build-aware APL analysis
- already tested

Why it should be reshaped:
- current code assumes the decode path is tightly coupled to the old CLI
- the future `simc` package should separate:
  - build-input parsing
  - binary-backed talent decoding
  - provider-facing command shaping

Target shape:
- provider-local module for build decoding
- reusable helpers for build normalization
- commands like `simc decode-build`

### Prune and branch reasoning

Source:
- `simc_exp/prune.py`
- `simc_exp/branch.py`

Why it should move:
- this is the core of the readonly analysis value
- it is exactly the kind of agent-facing reasoning we want from `simc`

Why it should be reshaped:
- these modules are currently optimized for the old CLI’s text output
- the future `simc` provider should make the internal reasoning model reusable by:
  - `apl-prune`
  - `apl-branch-trace`
  - `apl-intent`
  - `apl-branch-compare`
  - `analysis-packet`

Target shape:
- keep the reasoning local to `simc`
- expose cleaner normalized payloads
- avoid coupling the logic to one print format

### Analysis packet generation

Source:
- `simc_exp/packet.py`

Why it should move:
- it is one of the clearest agent-facing outputs in `simc_exp`

Why it should be reshaped:
- the current packet is designed around the old CLI helper stack
- the new version should emit a structured provider payload first, then format it via shared output tools

Target shape:
- `simc analysis-packet`
- possible future wrapper-aware surface for deep source reasoning

### CLI support and argument composition

Source:
- `simc_exp/cli_support.py`
- `simc_exp/cli.py`

Why it should not move directly:
- this is mostly argparse-era command glue
- the monorepo uses Typer and shared output patterns

Target shape:
- reimplement the command surface in the monorepo style
- keep only the reusable analysis logic, not the old CLI shell

## Keep Out Of Phase 1

These are useful, but they should not block the first `simc` provider milestone.

### Full branch-analysis command set

Keep out of phase 1:
- `apl-prune`
- `apl-branch-trace`
- `apl-intent`
- `apl-intent-explain`
- `apl-branch-compare`
- `analysis-packet`

Reason:
- they depend on the lower layers being packaged cleanly first
- they should arrive after `doctor`, `version`, `run`, `spec-files`, and `decode-build`

### Rich first-cast and log workflows

Keep out of phase 1:
- `first-cast`
- `log-actions`

Reason:
- these are valuable, but they depend on a solid local run path and temporary-output strategy

### Any mutation-heavy repo workflow beyond controlled sync/build

Keep out of phase 1:
- automatic branch switching
- repo rewriting
- aggressive cleanup/build-cache mutation

Reason:
- the first milestone should be safe and mostly readonly, with explicit build/run escalation

## Proposed `simc` Phase Mapping

### Phase 1

Ship first:
- `doctor`
- `version`
- `sync`
- `build`
- `run`
- `inspect`
- `spec-files`
- `decode-build`

Code likely needed:
- `repo.py`
- selected `build_input.py`
- selected `search.py`
- new Typer CLI surface

### Phase 2

Add readonly source analysis:
- `apl-lists`
- `apl-graph`
- `apl-talents`
- `find-action`
- `trace-action`

Code likely needed:
- `apl.py`
- remaining `search.py`

### Phase 3

Add reasoning and runtime escalation:
- `apl-prune`
- `apl-branch-trace`
- `apl-intent`
- `apl-intent-explain`
- `apl-branch-compare`
- `analysis-packet`
- `first-cast`
- `log-actions`

Code likely needed:
- `prune.py`
- `branch.py`
- `packet.py`
- `sim.py`

## What Should Stay Provider-Specific

Do not force these into shared packages yet:
- APL parsing
- talent/build decoding
- SimulationCraft repo layout assumptions
- branch/prune semantics
- first-cast timing helpers
- combat-log extraction
- analysis-packet composition

These are shared within `simc`, not across the monorepo.

## Testing Signal From `simc_exp`

Existing tests give strong confidence in the migration direction:
- `simc_exp/tests/test_build_input.py`
- `simc_exp/tests/test_cli_support.py`
- `simc_exp/tests/test_packet.py`
- `simc_exp/tests/test_prune.py`
- `simc_exp/tests/test_simc_integration.py`

That means the migration should preserve:
- build decoding coverage
- prune/branch reasoning coverage
- packet coverage
- at least one binary-backed integration check when a local `simc` binary exists

## Migration Rules

- Prefer porting logic modules, not the old argparse command layer.
- Keep the first `simc` milestone small and safe.
- Treat readonly analysis as a first-class feature, not an afterthought.
- Do not move `simc`-specific reasoning into shared monorepo packages.
- Keep docs aligned as command phases are promoted from inventory to implementation.
