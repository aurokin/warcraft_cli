# SimulationCraft Provider (Design Record)

Pre-implementation research and design intent for the `simc` provider, preserved for provenance.
For what the CLI does today, read [../../simc/README.md](../../simc/README.md).

Companion records:
- [simc-implementation.md](simc-implementation.md): file-level package shape and phase plan
- [simc-migration-inventory.md](simc-migration-inventory.md): what moved over from the `simc_exp` exploration

## Why SimC Was Structurally Important

`simc` was not another web integration. It was the local-tool and local-repository integration, and it
existed partly to prove that the shared monorepo abstractions were not HTTP-only. If a shared layer had
assumed HTTP everywhere, `simc` would have been the provider that exposed it.

## Research Summary (sampled from the official repository and README)

- SimulationCraft is a local simulator written in C++
- the command-line binary is `simc`
- the graphical interface is described upstream as largely unmaintained
- the project expects local builds on Linux rather than packaged Linux releases
- parameter-file and command-line driven execution are first-class workflows

Observed from the original `simc_exp` exploration:

- a read-only local checkout supports a large amount of useful analysis work
- many high-value questions need neither a mutated repo nor a full sim run
- APL structure, build decoding, and search/trace workflows are agent-useful on their own
- short local sims remain a useful escalation path for timing and runtime validation

## Access Model

The intended access model was a local-tool service: read-only repo inspection, optional repo sync, an
optional CLI-managed checkout for users who wanted the CLI to own the repo lifecycle, local build
management, local binary execution, and profile/log/analysis helpers. Guide-derived or user-derived APL
variants were to be compared without touching the upstream checkout.

## Planned Phase Shape

1. **Local tool foundation**: `doctor`, `sync`, `build`, `version`, `sim`, `run`, `inspect`, `spec-files`, `decode-build`.
2. **Read-only source analysis**: `apl-lists`, `apl-graph`, `apl-talents`, `find-action`, `trace-action`.
3. **Runtime-aware reasoning**: `apl-prune`, `apl-branch-trace`, `apl-intent`, `apl-intent-explain`, `priority`, `inactive-actions`, `opener`, `apl-branch-compare`, `analysis-packet`, `first-cast`, `log-actions`.
4. **Comparison workflow**: `build-harness`, `validate-apl`, `compare-apls`, `variant-report`, `verify-clean`.
5. **Talent comparison and modification**: `compare-builds`, `modify-build`.

All five phases shipped; the command list in the provider README is the current source of truth.

## Analysis Boundary

The analysis layer was required to stay grounded in observable local evidence:

- `analysis-packet` and the intent helpers summarize structure, branches, and runtime samples
- exact-build views (`priority`, `inactive-actions`, `opener`) strip inactive talent branches instead of
  summarizing shared APL text blindly
- recommending a next command is fine; authoritative "smart answers" beyond what the source tree or a
  runtime sample proves are not

## Risks Called Out Up Front

- local build requirements vary by platform, so environment diagnostics have to be strong
- result parsing should not be over-generalized too early
- read-only source analysis can sprawl without clear phase boundaries
- the analysis layer must not assume one spec or one APL family

## Source Links

- `https://github.com/simulationcraft/simc`
- [Roadmap](../../ROADMAP.md)
