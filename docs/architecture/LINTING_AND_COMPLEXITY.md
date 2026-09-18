# Linting And Complexity

Static quality tooling, what blocks a merge, and what is advisory.

## Commands

| Target | Purpose | Blocking |
|--------|---------|----------|
| `make check` | `lint` + `typecheck` + `lint-boundaries` + `complexity-gate` + `deadcode` + `test-fast` — local CI parity | yes |
| `make install` | `uv sync --all-extras` (editable dev environment) | — |
| `make lint` | Ruff over `packages/`, `tests/`, `scripts/` | yes |
| `make lint-all` | Alias of `make lint` | yes |
| `make typecheck` | Mypy over all 16 packages (file list in root `pyproject.toml`) | yes |
| `make lint-boundaries` | `import-linter` package boundaries (`.importlinter`) | yes |
| `make complexity-gate` | `xenon --max-absolute C packages` — fails on any function graded D or worse | yes |
| `make deadcode` | `vulture packages scripts tests scripts/vulture_allowlist.py --min-confidence 80` | yes |
| `make test-fast` | `pytest -q -m "not live"` | yes |
| `make complexity` | Radon CC + maintainability index report (no threshold) | no |
| `make coverage` | Coverage over `packages/` (`pytest-cov`, or stdlib `trace` fallback) | no |
| `make skills` | Regenerate provider subskills under `.generated-skills/` | — |
| `make reference` | Regenerate `docs/reference/<cli>.md` from the Typer apps | — |
| `make schema` | Regenerate `schemas/envelope.schema.json` from the envelope TypedDicts | — |
| `make build` | `uv build --wheel` (the release artifact) | — |
| `make test-live` | Live provider contract tests (network + credentials) | — |
| `make pre-commit-install` | Install the local hooks in `.pre-commit-config.yaml` | — |
| `make benchmark-cache` | Cold vs warm Wowhead search timing | no |
| `make fixture-refresh-hints` | Prints URLs for refreshing Wowhead fixtures | no |

`make reference` and `make skills` write generated files. Never hand-edit their output; staleness
tests in `tests/` fail when the checked-in copies drift from the CLIs.

## Ruff Configuration

`[tool.ruff.lint] select` in the root `pyproject.toml`:

| Family | What it catches |
|--------|-----------------|
| `E`, `W`, `F` | pycodestyle errors/warnings and pyflakes |
| `B` | bugbear (mutable defaults, silent surprises) |
| `I` | import sorting |
| `SIM` | simplifiable constructs |
| `UP` | outdated syntax for the 3.12 target |
| `S102`, `S103`, `S105`, `S106`, `S107`, `S108` | `exec`, loose file permissions, hardcoded secrets, insecure temp paths |
| `S324`, `S501` | weak hashes, disabled TLS verification |
| `S603`, `S607` | untrusted subprocess input and partial executable paths (`simc` runs a local binary) |

The `BLE` (blind-except) family is deliberately **not** enabled: the repo catches broad exceptions
on purpose at transport and process seams, and `warcraft_core.cli.guarded_run` turns whatever
escapes into an error envelope. Do not add `# noqa: BLE001` markers; they are inert.

Per-file ignores relax `E501` and secret/subprocess rules inside `tests/` and `scripts/`, and
`B008` inside every `main.py` because Typer option defaults are function calls by design.

## Complexity Status

`xenon --max-absolute C packages` is a blocking step in `make check` and in CI. It fails the build
on any function radon grades D, E, or F.

At the time of writing `make complexity-gate` passes: `radon cc packages -n D` reports nothing and
the average grade over 2,253 blocks is A. The threshold is the contract — when a change pushes a
function to D, decompose the function rather than excluding the file or lowering the gate.

Extraction rules when a function trips the gate:

- move payload assembly out of the Typer command body into a plain function that takes a small
  dataclass of options, so it is unit-testable without a CLI runner
- keep one provider family per change
- extract into a shared package only when repetition is proven, not in anticipation

Large `main.py` entry modules (`wowhead`, `warcraftlogs`, `simc`, the `warcraft` wrapper,
`raiderio`) are where the gate bites first.

## Dead Code

`make deadcode` runs vulture at confidence 80 with `scripts/vulture_allowlist.py` as the allowlist.
The allowlist exists for names vulture cannot see through (protocol members, `__exit__` signatures,
test doubles). When vulture flags something new, delete the code or add an allowlist entry with a
reason — do not lower the confidence threshold.

## Pre-commit

`.pre-commit-config.yaml` holds optional local hooks that mirror the blocking `make check` steps
plus a secret scan. Install with `make pre-commit-install`. CI enforces the same checks, so the
hooks are a convenience, not the source of truth.

## Success Criteria

- lint and typecheck stay fast enough to run on every change
- the complexity gate stays at `--max-absolute C` with no per-file exclusions
- shared packages stay typed and bounded
- new shared utilities come from repeated patterns, not speculation

## Local Install

```bash
make install
```

`make dev-deploy-no-link` builds a branch-local deploy; use `make dev-deploy` only to relink
`~/.local/bin` to the current checkout.

## Related

- [CONTRACT_TEST_CATALOG.md](CONTRACT_TEST_CATALOG.md)
- [FIXTURE_MAINTENANCE.md](FIXTURE_MAINTENANCE.md)
- [REPO_STRUCTURE_AND_PACKAGING.md](REPO_STRUCTURE_AND_PACKAGING.md)
