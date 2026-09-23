# Agent Instructions

## Toolchain
- Package manager is `uv`. Install the dev environment: `uv sync --all-extras` (or `make install`).
- `uv.lock` is committed. Refresh it with `uv lock` whenever a dependency changes, and commit it.
- `pip install -e '.[dev]'` still works if you cannot use uv; add `,redis` for the Redis extra.
- Fast tests: `make test-fast` (`pytest -q -m "not live"`). Non-live tests run under a network guard
  in `tests/conftest.py`, so a test that reaches the network fails.
- Local CI parity: `make check` = lint + typecheck + import boundaries + complexity gate + dead code
  + fast tests.
- End-to-end journeys: `make test-e2e` runs `tests/e2e/` through the installed binaries against
  real providers with the keys in `~/.config/warcraft/providers`. Skips are failures unless
  excluded via `WARCRAFT_E2E_SKIP`. See `docs/architecture/E2E_TESTING.md`.
- Lint: `make lint` (ruff over `packages/`, `tests/`, `scripts/`; `make lint-all` is an alias).
- Type check: `make typecheck` (mypy over all 16 packages).
- Complexity: `make complexity-gate` (`xenon --max-absolute C packages`, blocking). `make complexity`
  is the advisory radon report.
- Dead code: `make deadcode` (vulture with `scripts/vulture_allowlist.py`, blocking).
- Coverage: `make coverage` over `packages/` (`pytest-cov`, stdlib `trace` fallback).
- Generated docs: `make reference` writes `docs/reference/<cli>.md` from the Typer apps and
  `make skills` writes the provider subskills. Never hand-edit either output; staleness tests fail
  when they drift.
- Build the release artifact: `make build` (`uv build --wheel`).
- Live canary: `make test-canary` (`tests/test_wowhead_parser_canaries.py`, gated by
  `WOWHEAD_LIVE_TESTS=1`) is the only live test outside `tests/e2e/`.
  `.github/workflows/live-contracts.yml` runs it and the keyless e2e files weekly.
- Branch-local deploy: `make dev-deploy-no-link`. Relink `~/.local/bin` to this checkout with
  `make dev-deploy`.
- Optional pre-commit: `make pre-commit-install`.

## Commit Attribution
- AI commits MUST include:
```text
Co-Authored-By: OpenAI Codex <noreply@openai.com>
```

## Key Conventions
- Use `wowhead`, not `wowhead-cli`, in commands and examples.
- Put global flags before the subcommand.
- Every binary emits the envelope in `docs/foundation/ERROR_CONTRACT.md`: `ok`, `provider`,
  `command`, `kind`, `schema_version`, `query`, `provenance`, `data`, and `error` on failure.
  Failures use the shared exit codes (1 generic, 2 usage, 3 auth, 4 not found, 5 network).
- Each provider package exports `PROVIDER` from `<pkg>/provider.py` satisfying
  `warcraft_core.provider.ProviderSurface`. Surface functions stay pure: no printing, no
  `typer.Exit`. Typer commands are thin wrappers, and the `warcraft` wrapper calls `PROVIDER`
  objects directly instead of spawning provider binaries.
- Shared packages never import a provider package and never run a provider binary.
  `warcraft_core.talent_transport` stays pure and takes an injectable executor; `simc_cli` supplies
  the SimulationCraft-backed one.
- Providers are tiered (core / supported / experimental) on `ProviderRegistration` in
  `packages/warcraft-cli/src/warcraft_cli/providers.py`. Keep README, skill, and docs tiers in sync
  with the registry.
- Hand-written fixtures are called synthetic, not recorded. Captured provider pages are trimmed per
  `docs/architecture/FIXTURE_MAINTENANCE.md`.
- Keep `README.md` short.
- Keep flag and command enumerations in the generated `docs/reference/`; `docs/USAGE.md` carries
  workflows and conventions.
- Keep roadmap sequencing and current priority in `docs/ROADMAP.md`.
- Keep provider-specific docs in `docs/<cli>/README.md`.
- Keep the docs index in `docs/README.md`.
- Keep repo-wide product philosophy in `docs/foundation/PRODUCT_PRINCIPLES.md`.
- Keep repo-wide analytics rules in `docs/foundation/SAFE_ANALYTICS_RULES.md`.
- Keep shared identity semantics aligned with `docs/foundation/IDENTITY_CONTRACT.md` and `packages/warcraft-core/src/warcraft_core/identity.py`.
- Keep docs aligned with actual CLI behavior.
- Prefer updating docs when command contracts or output shapes change.
- Do not bolt on “smart answers” for analytics-heavy questions.
- Build reliable, sample-backed analytics primitives that agents can trust and compose.
- Treat normalization as an additive analysis layer, not a replacement for raw source content.
- Keep raw guide/article/log content and provenance accessible alongside normalized outputs.
- Follow `docs/foundation/SAFE_ANALYTICS_RULES.md` when adding sampled, compared, or derived analytics surfaces.
- Skills are consumer-facing workflow docs, not internal maintenance docs.
- Keep repo maintenance, roadmap, packaging, architecture, and generation workflow out of skill docs.
- `skills/warcraft/SKILL.md` and `skills/warcraft/references/*.md` are the source of truth for consumer skill content.
- In skill files, reference bundled docs with portable relative paths like `references/simc.md`, not absolute filesystem paths.
- Generated provider subskills belong under `.generated-skills/` and must not be edited manually.
- Do not mention generation or internal maintenance workflow inside consumer skill files.

## Local Skills
- Use the `warcraft` skill for root wrapper and provider-routing work. See `skills/warcraft/SKILL.md`.

## Releases
- `CHANGELOG.md` at the repo root is the source of truth for shipped changes.
- Add user-visible changes (new flags, output shape changes, fixed bugs, removed commands) to `## [Unreleased]` in the same PR that ships them.
- Release flow lives in `docs/RELEASE.md`. Use `make release VERSION=X.Y.Z` to bump every `pyproject.toml` together.
- Refresh `uv.lock` in the release commit when dependencies changed.
- Pushing the `v<version>` tag triggers `.github/workflows/release.yml`, which builds the wheel and attaches it to the GitHub release.
